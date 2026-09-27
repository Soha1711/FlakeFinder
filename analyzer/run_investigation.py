"""
analyzer/run_investigation.py — FlakeFinder Step 8 Isolation subagent entry point.

Usage:
    python analyzer/run_investigation.py <pytest_node_id>

Example:
    python analyzer/run_investigation.py tests/test_b_race.py::test_background_update_completes

Architecture:
    1. Build the Isolation prompt from bob_prompts/isolation_prompt.md,
       substituting {test_name} with the supplied pytest node ID.
    2. If `bob` is on PATH, invoke Bob Shell with the prompt.
    3. Parse and validate the Isolation JSON schema from Bob's response.
    4. If Bob is unavailable, fails, or returns invalid/non-conforming JSON:
       - Print "[ISOLATION] using fallback" to stderr.
       - Run agents/scripts/run_isolated.sh "<test_name>" 10 via Git Bash.
       - Parse the script's real JSON output (never invent pass/fail counts).
    5. Save the validated JSON to state/evidence/isolation_<safe_test_name>.json.
    6. Print the JSON to stdout.

Isolation JSON schema:
    {
        "subagent": "isolation",
        "test_name": "<full pytest node ID>",
        "evidence": "<real script evidence>",
        "hypothesis": "<grounded hypothesis>",
        "confidence": "high | medium | low"
    }
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent.parent
_ISOLATION_PROMPT = _HERE / "bob_prompts" / "isolation_prompt.md"
_OUTPUT_DIR = _HERE / "state" / "evidence"
_DEBUG_DIR = _HERE / "state" / "debug"
_RUN_ISOLATED_SH = _HERE / "agents" / "scripts" / "run_isolated.sh"
_DEMO_REPO = _HERE / "demo-repo"
_DOTENV_FILE = _HERE / ".env"


# ---------------------------------------------------------------------------
# Re-use Bob infrastructure from run_planner
# ---------------------------------------------------------------------------

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from run_planner import _resolve_bob, _load_bob_env  # noqa: E402


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

_VALID_CONFIDENCE = frozenset({"high", "medium", "low"})


def _validate(data: dict, expected_test_name: str) -> None:
    """Raise ValueError if data does not satisfy the Isolation JSON schema."""
    required = {
        "subagent",
        "test_name",
        "evidence",
        "hypothesis",
        "confidence",
    }

    missing = required - data.keys()

    if missing:
        raise ValueError(
            f"Isolation JSON missing keys: {sorted(missing)}"
        )

    if data["subagent"] != "isolation":
        raise ValueError(
            f"'subagent' must be 'isolation', got: {data['subagent']!r}"
        )

    if data["test_name"] != expected_test_name:
        raise ValueError(
            f"'test_name' must be {expected_test_name!r}, "
            f"got: {data['test_name']!r}"
        )

    if not isinstance(data["evidence"], str) or not data["evidence"].strip():
        raise ValueError("'evidence' must be a non-empty string")

    if not isinstance(data["hypothesis"], str) or not data["hypothesis"].strip():
        raise ValueError("'hypothesis' must be a non-empty string")

    if data["confidence"] not in _VALID_CONFIDENCE:
        raise ValueError(
            f"'confidence' must be one of {sorted(_VALID_CONFIDENCE)}, "
            f"got: {data['confidence']!r}"
        )


# ---------------------------------------------------------------------------
# Safe filename helper
# ---------------------------------------------------------------------------

def _safe_name(test_name: str) -> str:
    """Convert a pytest node ID to a filesystem-safe string."""
    return re.sub(r"[^\w\-]", "_", test_name)


# ---------------------------------------------------------------------------
# Bob invocation
# ---------------------------------------------------------------------------

def _build_isolation_prompt(test_name: str) -> str:
    """Return the isolation prompt with {test_name} substituted."""
    template = _ISOLATION_PROMPT.read_text(encoding="utf-8")
    return template.replace("{test_name}", test_name)


def _save_raw_last_message(last_message) -> None:
    """
    Save Bob's raw last_message before parsing.

    This is diagnostic-only. Failure to write the debug file must never
    break the main Bob/fallback execution path.
    """
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)

        if isinstance(last_message, str):
            raw_text = last_message
        else:
            raw_text = json.dumps(last_message, indent=2)

        (_DEBUG_DIR / "isolation_raw_response.txt").write_text(
            raw_text,
            encoding="utf-8",
        )
    except Exception:
        pass


def _parse_last_message(last_message) -> dict | None:
    """
    Parse Bob's last_message.

    Accepted forms:
      1. A JSON object already represented as a dict.
      2. Plain JSON text.
      3. JSON enclosed in a Markdown code fence.

    Arbitrary prose surrounding JSON is intentionally not accepted.
    """
    if isinstance(last_message, dict):
        return last_message

    if not isinstance(last_message, str):
        print(
            "[ISOLATION] BOB FAILED: last_message has unexpected type "
            f"{type(last_message).__name__}",
            file=sys.stderr,
        )
        return None

    raw_message = last_message.strip()

    # Bob may return valid JSON inside a Markdown code fence:
    #
    # ```json
    # { ... }
    # ```
    #
    # Only strip a surrounding fence. Do not search for JSON inside
    # arbitrary prose.
    if raw_message.startswith("```") and raw_message.endswith("```"):
        lines = raw_message.splitlines()

        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]

        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]

        raw_message = "\n".join(lines).strip()

    try:
        data = json.loads(raw_message)
    except json.JSONDecodeError as exc:
        print(
            f"[ISOLATION] BOB FAILED: last_message is not valid JSON — {exc}",
            file=sys.stderr,
        )
        return None

    if not isinstance(data, dict):
        print(
            "[ISOLATION] BOB FAILED: parsed last_message is not a JSON object",
            file=sys.stderr,
        )
        return None

    return data


def _invoke_bob(test_name: str) -> dict | None:
    """
    Try to call the Bob Shell CLI via:
        bob run --format json "<prompt>"

    Returns a validated dict on success, or None on any failure.
    """
    argv_prefix = _resolve_bob()

    if argv_prefix is None:
        return None

    prompt_text = _build_isolation_prompt(test_name)
    bob_env = _load_bob_env()

    try:
        proc = subprocess.run(
            argv_prefix + ["run", "--format", "json", prompt_text],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=120,
            env=bob_env,
        )
    except Exception as exc:
        print(
            f"[ISOLATION] BOB FAILED: subprocess error — {exc}",
            file=sys.stderr,
        )
        return None

    if proc.returncode != 0:
        print(
            f"[ISOLATION] BOB FAILED: non-zero exit ({proc.returncode}) — "
            f"{proc.stderr.strip()[:200]}",
            file=sys.stderr,
        )
        return None

    raw = proc.stdout.strip()

    # Parse the Bob Shell JSON envelope.
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"[ISOLATION] BOB FAILED: response is not valid JSON — {exc}",
            file=sys.stderr,
        )
        return None

    # Extract last_message from the envelope.
    if not isinstance(envelope, dict) or "last_message" not in envelope:
        print(
            "[ISOLATION] BOB FAILED: JSON envelope missing "
            "'last_message' key — "
            f"keys: {list(envelope.keys()) if isinstance(envelope, dict) else type(envelope).__name__}",
            file=sys.stderr,
        )
        return None

    last_message = envelope["last_message"]

    # Capture exactly what Bob returned before parsing it.
    _save_raw_last_message(last_message)

    # Parse string, fenced JSON, or dict response.
    data = _parse_last_message(last_message)

    if data is None:
        return None

    # Enforce test_name to be the exact node ID supplied by the caller.
    data["test_name"] = test_name

    try:
        _validate(data, test_name)
    except ValueError as exc:
        print(
            f"[ISOLATION] BOB FAILED: schema validation error — {exc}",
            file=sys.stderr,
        )
        return None

    return data


# ---------------------------------------------------------------------------
# Bash executable resolution
# ---------------------------------------------------------------------------

_GIT_BASH_DEFAULT = Path("C:/Program Files/Git/bin/bash.exe")


def _find_bash() -> str:
    """
    Return the path to a bash executable suitable for run_isolated.sh.

    On Windows:
      1. Prefer Git Bash at C:\\Program Files\\Git\\bin\\bash.exe.
      2. Fall back to shutil.which("bash") if Git Bash is absent.
      3. Last resort: literal "bash".

    On non-Windows:
      Use shutil.which("bash") or "bash".
    """
    if os.name == "nt":
        if _GIT_BASH_DEFAULT.exists():
            return str(_GIT_BASH_DEFAULT)

        return shutil.which("bash") or "bash"

    return shutil.which("bash") or "bash"


# ---------------------------------------------------------------------------
# Fallback: run the actual shell script
# ---------------------------------------------------------------------------

def _fallback(test_name: str) -> dict:
    """
    Run agents/scripts/run_isolated.sh via Git Bash and parse its real JSON output.

    Raises RuntimeError if:
      - the subprocess fails, times out, or returns a non-zero exit code
      - the script output is empty or not valid JSON
      - the JSON is missing runs, passes, or failures

    Never invents or defaults pass/fail/run counts.
    """
    bash_exe = _find_bash()
    script_path = str(_RUN_ISOLATED_SH).replace("\\", "/")

    fallback_env = _load_bob_env()

    # Ensure the repository venv's Scripts directory comes first on Windows,
    # matching the environment used during the Step 7 manual verification.
    venv_scripts = str(_HERE / "venv" / "Scripts")
    fallback_env["PATH"] = (
        venv_scripts
        + os.pathsep
        + fallback_env.get("PATH", "")
    )

    try:
        proc = subprocess.run(
            [bash_exe, script_path, test_name, "10"],
            capture_output=True,
            text=True,
            timeout=180,
            env=fallback_env,
        )
    except Exception as exc:
        raise RuntimeError(
            f"[ISOLATION] fallback script failed to run — {exc}"
        ) from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"[ISOLATION] fallback script exited with code "
            f"{proc.returncode} — {proc.stderr.strip()[:200]}"
        )

    script_output = proc.stdout.strip()

    if not script_output:
        raise RuntimeError(
            "[ISOLATION] fallback script produced no output"
        )

    try:
        script_data = json.loads(script_output)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"[ISOLATION] fallback script output is not valid JSON — {exc}\n"
            f"Output was: {script_output[:200]!r}"
        ) from exc

    if not isinstance(script_data, dict):
        raise RuntimeError(
            "[ISOLATION] fallback script JSON must be an object"
        )

    missing = [
        key
        for key in ("runs", "passes", "failures")
        if key not in script_data
    ]

    if missing:
        raise RuntimeError(
            f"[ISOLATION] fallback script JSON missing required keys: "
            f"{missing}\nGot: {script_data}"
        )

    runs = script_data["runs"]
    passes = script_data["passes"]
    failures = script_data["failures"]

    evidence = (
        f"runs: {runs}, passes: {passes}, failures: {failures}"
    )

    if passes == 0:
        hypothesis = (
            "The test fails on every isolated run, ruling out cross-test "
            "order-dependency — the failure is intrinsic to the test or "
            "the code it exercises."
        )
        confidence = "high"

    elif failures == 0:
        hypothesis = (
            "The test passes on every isolated run, suggesting cross-test "
            "order-dependency or environment pollution rather than an "
            "intrinsic defect."
        )
        confidence = "high"

    else:
        rate = failures / runs
        confidence = "high" if rate >= 0.3 else "medium"

        hypothesis = (
            f"The test fails {failures}/{runs} times in isolation, indicating "
            "an intrinsic non-deterministic flake (race condition or timing "
            "sensitivity) rather than pure cross-test order-dependency."
        )

    return {
        "subagent": "isolation",
        "test_name": test_name,
        "evidence": evidence,
        "hypothesis": hypothesis,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(test_name: str) -> dict:
    """
    Core logic: receive a full pytest node ID and return a validated
    Isolation JSON dict.

    Tries Bob first; falls back to run_isolated.sh on any Bob failure.
    """
    if "::" not in test_name:
        raise ValueError(
            "test_name must be a full pytest node ID "
            f"(file::function), got: {test_name!r}"
        )

    result = _invoke_bob(test_name)

    if result is None:
        print(
            "[ISOLATION] using fallback",
            file=sys.stderr,
        )
        result = _fallback(test_name)
    else:
        print(
            "[ISOLATION] using real Bob",
            file=sys.stderr,
        )

    _validate(result, test_name)

    # Persist validated evidence.
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    output_file = (
        _OUTPUT_DIR
        / f"isolation_{_safe_name(test_name)}.json"
    )

    output_text = json.dumps(result, indent=2)

    output_file.write_text(
        output_text + "\n",
        encoding="utf-8",
    )

    return result


def run_all_subagents_parallel(test_name: str) -> dict:
    from analyzer.run_all_subagents import run_all
    return run_all(test_name)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) != 2:
        print(
            "Usage: python analyzer/run_investigation.py "
            "<pytest_node_id>",
            file=sys.stderr,
        )
        print(
            "Example: python analyzer/run_investigation.py "
            "tests/test_b_race.py::test_background_update_completes",
            file=sys.stderr,
        )
        sys.exit(1)

    test_name = sys.argv[1]

    try:
        data = run(test_name)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()