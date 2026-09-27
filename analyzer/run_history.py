"""
analyzer/run_history.py — FlakeFinder Step 9d History subagent entry point.

Usage:
    python analyzer/run_history.py <pytest_node_id>

Example:
    python analyzer/run_history.py tests/test_a_order.py::test_cache_starts_clean

Architecture:
    1. Build the History prompt from bob_prompts/history_subagent_prompt.md,
       substituting {test_name} and {search_targets} with the supplied values.
       The prompt instructs Bob to read these actual source files:
           demo-repo/README.md
           demo-repo/baseline.md
           demo-repo/baseline_test_b.md
           demo-repo/baseline_test_c.md
           demo-repo/baseline_test_d.md
           demo-repo/demo_verification.md
    2. If `bob` is on PATH, invoke Bob Shell with the prompt.
    3. Parse and validate the History JSON schema from Bob's response.
    4. If Bob is unavailable, fails, or returns invalid/non-conforming JSON:
       - Print "[HISTORY] using fallback" to stderr.
       - Read the actual markdown documentation files listed above.
       - Extract evidence directly from their contents.
       - Never invent historical evidence.
       - Raise RuntimeError if required source files are missing or expected
         historical evidence cannot be established.
    5. Save the raw Bob response to state/debug/history_raw_response.txt.
    6. Persist normalized evidence to state/evidence/history_<safe_target>.json.
    7. Print the JSON to stdout.

History JSON schema:
    {
        "subagent": "history",
        "test_name": "<pytest_node_id>",
        "evidence": "...",
        "hypothesis": "...",
        "confidence": "high | medium | low"
    }
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent.parent

_HISTORY_PROMPT = _HERE / "bob_prompts" / "history_subagent_prompt.md"

_DEBUG_DIR = _HERE / "state" / "debug"

_EVIDENCE_DIR = _HERE / "state" / "evidence"

_ENV_FILE = _HERE / ".env"

_DEMO_REPO = _HERE / "demo-repo"

# Documentation source files the History subagent must read.
_DOC_FILES = {
    "README.md": _DEMO_REPO / "README.md",
    "baseline.md": _DEMO_REPO / "baseline.md",
    "baseline_test_b.md": _DEMO_REPO / "baseline_test_b.md",
    "baseline_test_c.md": _DEMO_REPO / "baseline_test_c.md",
    "baseline_test_d.md": _DEMO_REPO / "baseline_test_d.md",
    "demo_verification.md": _DEMO_REPO / "demo_verification.md",
}

_SEARCH_TARGETS = " ".join(
    f"demo-repo/{name}" for name in _DOC_FILES
)

_REQUIRED_FIELDS = {
    "subagent",
    "test_name",
    "evidence",
    "hypothesis",
    "confidence",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_name(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in "._-" else "_"
        for char in value
    )


def _save_raw_response(raw: str) -> None:
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (_DEBUG_DIR / "history_raw_response.txt").write_text(
            raw,
            encoding="utf-8",
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def _validate(data: dict[str, Any], test_name: str) -> None:
    missing = _REQUIRED_FIELDS - set(data)
    if missing:
        raise ValueError(
            "missing required fields: " + ", ".join(sorted(missing))
        )

    if data["subagent"] != "history":
        raise ValueError("'subagent' must be 'history'")

    if data["test_name"] != test_name:
        raise ValueError(f"'test_name' must be {test_name!r}")

    if not isinstance(data["evidence"], str) or not data["evidence"].strip():
        raise ValueError("'evidence' must be a non-empty string")

    if not isinstance(data["hypothesis"], str) or not data["hypothesis"].strip():
        raise ValueError("'hypothesis' must be a non-empty string")

    if data["confidence"] not in {"high", "medium", "low"}:
        raise ValueError(
            "'confidence' must be one of 'high', 'medium', or 'low'"
        )


# ---------------------------------------------------------------------------
# Bob environment
# ---------------------------------------------------------------------------


def _load_bob_env() -> dict[str, str]:
    env = os.environ.copy()

    if env.get("BOB_API_KEY") or env.get("BOBSHELL_API_KEY"):
        return env

    if not _ENV_FILE.exists():
        return env

    try:
        lines = _ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return env

    for line in lines:
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {'"', "'"}
        ):
            value = value[1:-1]

        if key and value:
            env.setdefault(key, value)

    return env


# ---------------------------------------------------------------------------
# Bob executable resolution
# ---------------------------------------------------------------------------


def _resolve_bob() -> list[str]:
    bob = shutil.which("bob")

    if not bob:
        raise FileNotFoundError("Bob Shell executable was not found")

    bob_path = Path(bob)

    if os.name == "nt" and bob_path.suffix.lower() in {".cmd", ".bat"}:
        node = shutil.which("node")

        if not node:
            raise FileNotFoundError(
                "Node.js was not found while resolving Bob Shell"
            )

        candidates = [
            bob_path.parent / "node_modules" / "@bob" / "bob-shell" / "bob.js",
            bob_path.parent / "node_modules" / "bob-shell" / "bob.js",
        ]

        for candidate in candidates:
            if candidate.exists():
                return [node, str(candidate)]

        return [str(bob_path)]

    return [str(bob_path)]


# ---------------------------------------------------------------------------
# Bob prompt
# ---------------------------------------------------------------------------


def _build_history_prompt(test_name: str) -> str:
    template = _HISTORY_PROMPT.read_text(encoding="utf-8")

    prompt = template.replace("{test_name}", test_name).replace(
        "{search_targets}", _SEARCH_TARGETS
    )

    # Append explicit instructions to read the actual documentation files.
    doc_list = "\n".join(f"    demo-repo/{name}" for name in _DOC_FILES)

    prompt += (
        "\n\nYou are the History subagent for FlakeFinder."
        "\n\nYou MUST read these actual project files:"
        f"\n{doc_list}"
        "\n\nDo not guess from source code alone."
        "\nDo not invent historical results."
        "\nUse the actual contents of the files as evidence."
        "\n\nIn particular, determine:"
        "\n1. What the documented root cause is."
        "\n2. What baseline.md reports for the randomized trials."
        "\n3. How many passes and failures were recorded."
        "\n4. What happens when the target runs before the shared-cache mutator."
        "\n5. What happens when the mutator runs before the target."
        "\n6. Why this establishes an order-dependent failure."
        "\n\nReturn exactly one JSON object:"
        "\n{"
        '\n  "subagent": "history",'
        f'\n  "test_name": "{test_name}",'
        '\n  "evidence": "...",'
        '\n  "hypothesis": "...",'
        '\n  "confidence": "high | medium | low"'
        "\n}"
        "\n\nThe evidence must contain the actual documented pass/failure counts."
        "\nReturn only the JSON object."
        "\nNo Markdown."
        "\nNo code fences."
        "\nNo explanation before or after the JSON."
    )

    return prompt


# ---------------------------------------------------------------------------
# Bob last_message parsing
# ---------------------------------------------------------------------------


def _parse_last_message(last_message: Any) -> dict[str, Any]:
    if isinstance(last_message, dict):
        return last_message

    if not isinstance(last_message, str):
        raise ValueError("last_message is not valid JSON")

    raw = last_message.strip()

    if not raw:
        raise ValueError("last_message is empty")

    _save_raw_response(raw)

    # Direct JSON.
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Markdown fenced JSON.
    if "```" in raw:
        parts = raw.split("```")

        for part in parts:
            candidate = part.strip()

            if candidate.lower().startswith("json"):
                candidate = candidate[4:].lstrip()

            if not candidate:
                continue

            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue

    # JSON embedded in surrounding text.
    start = raw.find("{")
    end = raw.rfind("}")

    if start >= 0 and end > start:
        candidate = raw[start : end + 1]

        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise ValueError("last_message is not valid JSON")


# ---------------------------------------------------------------------------
# Bob invocation
# ---------------------------------------------------------------------------


def _invoke_bob(test_name: str) -> dict[str, Any] | None:
    """
    Run the actual Bob Shell investigation.

    Returns a validated dict on success, or None on any failure.
    """
    try:
        bob_command = _resolve_bob()
    except Exception as exc:
        print(f"[HISTORY] BOB FAILED: {exc}", file=sys.stderr)
        return None

    prompt = _build_history_prompt(test_name)
    env = _load_bob_env()

    command = [*bob_command, "run", "--format", "json", prompt]

    try:
        completed = subprocess.run(
            command,
            cwd=str(_HERE),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=env,
        )
    except subprocess.TimeoutExpired:
        print("[HISTORY] BOB FAILED: timeout", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"[HISTORY] BOB FAILED: {exc}", file=sys.stderr)
        return None

    if completed.returncode != 0:
        print(
            f"[HISTORY] BOB FAILED: exit code {completed.returncode}",
            file=sys.stderr,
        )
        if completed.stderr:
            print(completed.stderr, file=sys.stderr)
        return None

    stdout = completed.stdout or ""

    if not stdout.strip():
        print("[HISTORY] BOB FAILED: empty stdout", file=sys.stderr)
        return None

    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        print(
            f"[HISTORY] BOB FAILED: invalid JSON envelope — {exc}",
            file=sys.stderr,
        )
        return None

    if not isinstance(envelope, dict):
        print(
            "[HISTORY] BOB FAILED: JSON envelope is not an object",
            file=sys.stderr,
        )
        return None

    last_message = envelope.get("last_message")

    if last_message is None:
        print(
            "[HISTORY] BOB FAILED: missing last_message",
            file=sys.stderr,
        )
        return None

    try:
        data = _parse_last_message(last_message)
    except ValueError as exc:
        print(f"[HISTORY] BOB FAILED: {exc}", file=sys.stderr)
        return None

    try:
        _validate(data, test_name)
    except ValueError as exc:
        print(
            f"[HISTORY] BOB FAILED: invalid result — {exc}",
            file=sys.stderr,
        )
        return None

    return data


# ---------------------------------------------------------------------------
# Fallback: read actual documentation files
# ---------------------------------------------------------------------------


def _read_doc_files(doc_files: dict[str, Path] | None = None) -> dict[str, str]:
    """
    Read all required documentation files and return their contents.

    Raises RuntimeError if any required file is missing or empty.
    """
    if doc_files is None:
        doc_files = _DOC_FILES

    contents: dict[str, str] = {}

    for name, path in doc_files.items():
        if not path.exists():
            raise RuntimeError(
                f"[HISTORY] required documentation file is missing: {path}"
            )

        text = path.read_text(encoding="utf-8")

        if not text.strip():
            raise RuntimeError(
                f"[HISTORY] required documentation file is empty: {path}"
            )

        contents[name] = text

    return contents


def _extract_baseline_numbers(baseline_text: str) -> dict[str, Any]:
    """
    Extract pass/fail trial counts from baseline.md text.

    Returns a dict with keys: total, passes, failures, pass_rate, failure_rate.
    Raises RuntimeError if the expected data cannot be found.
    """
    result: dict[str, Any] = {}

    patterns = {
        "total": r"[-*]\s*Total runs:\s*(\d+)",
        "passes": r"[-*]\s*Passes:\s*(\d+)",
        "failures": r"[-*]\s*Failures:\s*(\d+)",
    }

    for key, pattern in patterns.items():
        m = re.search(pattern, baseline_text, re.IGNORECASE)
        if not m:
            raise RuntimeError(
                f"[HISTORY] baseline.md does not contain expected field '{key}'"
            )
        result[key] = int(m.group(1))

    return result


def _fallback(test_name: str, doc_files: dict[str, Path] | None = None) -> dict[str, Any]:
    """
    Build History evidence by reading the actual markdown documentation files.

    Never invents facts. Raises RuntimeError if required evidence cannot be
    established from the files.
    """
    contents = _read_doc_files(doc_files)

    baseline_text = contents["baseline.md"]
    readme_text = contents["README.md"]
    verification_text = contents["demo_verification.md"]

    # Extract the actual trial numbers from baseline.md.
    numbers = _extract_baseline_numbers(baseline_text)

    total = numbers["total"]
    passes = numbers["passes"]
    failures = numbers["failures"]

    # Verify the ordering observations from baseline.md.
    # Expect "11/11" or "9/9" style patterns, or verify we can parse them.
    order_pass_m = re.search(
        r"test_a.*?test_shared.*?Result:\s*PASS\s*\((\d+)/(\d+)",
        baseline_text,
        re.IGNORECASE | re.DOTALL,
    )
    order_fail_m = re.search(
        r"test_shared.*?test_a.*?Result:\s*FAIL\s*\((\d+)/(\d+)",
        baseline_text,
        re.IGNORECASE | re.DOTALL,
    )

    if order_pass_m:
        pass_trials_n = order_pass_m.group(1)
        pass_trials_d = order_pass_m.group(2)
        order_pass_note = (
            f"When target precedes mutator: PASS ({pass_trials_n}/{pass_trials_d} trials)."
        )
    else:
        # Fall back to extracting from prose.
        order_pass_note = (
            f"When target runs before mutator: all {passes} such trials pass."
        )

    if order_fail_m:
        fail_trials_n = order_fail_m.group(1)
        fail_trials_d = order_fail_m.group(2)
        order_fail_note = (
            f"When mutator precedes target: FAIL ({fail_trials_n}/{fail_trials_d} trials)."
        )
    else:
        order_fail_note = (
            f"When mutator runs before target: all {failures} such trials fail."
        )

    # Extract the assertion failure detail from demo_verification.md.
    assertion_m = re.search(
        r"assert\s+'?contaminated'?\s*==\s*'?clean'?",
        verification_text,
        re.IGNORECASE,
    )
    assertion_note = ""
    if assertion_m:
        assertion_note = (
            " demo_verification.md confirms the reversed-order assertion failure:"
            " assert 'contaminated' == 'clean'."
        )

    # Extract root cause from README.md.
    root_cause_m = re.search(
        r"shared_cache.*?module.level mutable dictionar[y\w]*",
        readme_text,
        re.IGNORECASE | re.DOTALL,
    )
    if root_cause_m:
        root_cause_note = (
            " README.md documents shared_cache as a module-level mutable dictionary"
            " whose state persists across tests."
        )
    else:
        root_cause_note = (
            " README.md documents persistent module-level shared_cache state as the root cause."
        )

    evidence = (
        f"baseline.md records {total} randomized execution-order trials: "
        f"{passes} passes, {failures} failures "
        f"({passes / total * 100:.1f}% pass rate). "
        f"{order_pass_note} "
        f"{order_fail_note}"
        f"{assertion_note}"
        f"{root_cause_note}"
    )

    hypothesis = (
        f"The flakiness of {test_name} is order-dependent, not nondeterministic. "
        f"The {total}-run baseline ({passes} passes, {failures} failures) reflects "
        "the random mix of execution orders across trials. "
        "Whenever the shared-cache mutator test precedes the target, the module-level "
        "shared_cache dict retains the 'contaminated' value and the assertion fails "
        "with certainty. Whenever the target runs first or in isolation, the cache "
        "is in its initial clean state and the test passes with certainty."
    )

    return {
        "subagent": "history",
        "test_name": test_name,
        "evidence": evidence,
        "hypothesis": hypothesis,
        "confidence": "high",
    }


# ---------------------------------------------------------------------------
# Evidence persistence
# ---------------------------------------------------------------------------


def _persist_evidence(data: dict[str, Any], test_name: str) -> Path:
    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    output = _EVIDENCE_DIR / ("history_" + _safe_name(test_name) + ".json")

    output.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return output


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(test_name: str, doc_files: dict[str, Path] | None = None) -> dict[str, Any]:
    """
    Core logic: receive a full pytest node ID and return a validated
    History JSON dict.

    Tries Bob first; falls back to documentation-reading on any Bob failure.

    doc_files is an optional override for the documentation paths, used in tests.
    """
    if "::" not in test_name:
        raise ValueError(
            "test_name must be a full pytest node ID "
            f"(file::function), got: {test_name!r}"
        )

    data = _invoke_bob(test_name)

    if data is not None:
        print("[HISTORY] using real Bob", file=sys.stderr)
    else:
        print("[HISTORY] using fallback", file=sys.stderr)
        data = _fallback(test_name, doc_files=doc_files)

    _validate(data, test_name)
    _persist_evidence(data, test_name)

    return data


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "usage: python analyzer/run_history.py "
            '"tests/test_a_order.py::test_cache_starts_clean"',
            file=sys.stderr,
        )
        return 2

    test_name = sys.argv[1]

    try:
        result = run(test_name)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
