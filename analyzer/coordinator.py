"""
analyzer/coordinator.py — FlakeFinder Step 11 Coordinator entry point.

Usage:
    python analyzer/coordinator.py <pytest_node_id>

Example:
    python analyzer/coordinator.py "tests/test_a_order.py::test_cache_starts_clean"

Architecture:
    1. Load the five evidence JSON files written by the subagents.
    2. If `bob` is on PATH, build a synthesis prompt and invoke Bob Shell.
    3. Parse and validate the Coordinator JSON schema from Bob's response.
    4. If Bob is unavailable or fails, use the conservative fallback.
       The fallback deliberately refuses to fabricate a ranked root cause.
    5. Persist the Coordinator result to state/evidence/coordinator_<safe>.json.
    6. Print the JSON to stdout.

Coordinator JSON schema:
    {
        "test_name": "...",
        "ranked_cause": "...",
        "winning_evidence": "...",
        "convergence_count": 1-5,
        "all_evidence_summary": [
            {"subagent": "isolation", "verdict": "...", "supports_winning_cause": true/false},
            {"subagent": "shuffle",   "verdict": "...", "supports_winning_cause": true/false},
            {"subagent": "bisect",    "verdict": "...", "supports_winning_cause": true/false},
            {"subagent": "static_scan","verdict": "...","supports_winning_cause": true/false},
            {"subagent": "history",   "verdict": "...", "supports_winning_cause": true/false}
        ],
        "confidence": "high | medium | low"
    }

Bisect handling:
    Bisect is only relevant for tests with regression history (test_d).
    For tests A/B/C, if no bisect evidence exists, bisect is recorded as
    "NOT APPLICABLE" and does NOT reduce convergence_count.
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

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_COORDINATOR_PROMPT = _HERE / "bob_prompts" / "coordinator_prompt.md"
_EVIDENCE_DIR = _HERE / "state" / "evidence"
_DEBUG_DIR = _HERE / "state" / "debug"
_ENV_FILE = _HERE / ".env"

# ---------------------------------------------------------------------------
# Source-file mapping — same as run_all_subagents.py
# ---------------------------------------------------------------------------

_SOURCE_FILE_MAP: dict[str, str] = {
    "tests/test_a_order.py::test_cache_starts_clean":
        "demo-repo/src/shared_cache.py",
    "tests/test_b_race.py::test_background_update_completes":
        "demo-repo/src/async_worker.py",
    "tests/test_c_random.py::test_value_is_valid":
        "demo-repo/src/nondeterministic.py",
    "tests/test_d_regression.py::test_calculate_total":
        "demo-repo/src/regression.py",
}

# Tests for which bisect evidence is applicable (has real git regression history).
_BISECT_APPLICABLE_TESTS = frozenset({
    "tests/test_d_regression.py::test_calculate_total",
})

_ALL_SUBAGENT_NAMES = ("isolation", "shuffle", "bisect", "static_scan", "history")

_VALID_CONFIDENCE = frozenset({"high", "medium", "low"})

# ---------------------------------------------------------------------------
# Filename-safe helpers — must match the exact variant used by each runner
# ---------------------------------------------------------------------------

def _safe_re(value: str) -> str:
    """isolation + shuffle use re.sub(r'[^\\w\\-]', '_', ...)"""
    return re.sub(r"[^\w\-]", "_", value)


def _safe_alnum(value: str) -> str:
    """bisect + history + static_scan use isalnum() or char in '._-'"""
    return "".join(
        c if c.isalnum() or c in "._-" else "_"
        for c in value
    )


# ---------------------------------------------------------------------------
# Evidence loading
# ---------------------------------------------------------------------------

def load_evidence_bundle(
    test_name: str,
    evidence_dir: Path | None = None,
) -> dict[str, Any]:
    """
    Load all five subagent evidence files for *test_name*.

    Returns a dict keyed by subagent name.  Missing files are represented as
    None (caller decides how to handle).

    File naming follows each runner's own _safe_name convention:
        isolation  → isolation_{safe_re(test_name)}.json
        shuffle    → shuffle_{safe_re(test_name)}.json
        history    → history_{safe_alnum(test_name)}.json
        bisect     → bisect_{safe_alnum(test_name)}.json
        static_scan→ static_{safe_alnum(source_file)}.json
    """
    edir = evidence_dir or _EVIDENCE_DIR

    bundle: dict[str, Any] = {}

    # isolation
    iso_path = edir / f"isolation_{_safe_re(test_name)}.json"
    bundle["isolation"] = _read_json(iso_path)

    # shuffle
    shuffle_path = edir / f"shuffle_{_safe_re(test_name)}.json"
    bundle["shuffle"] = _read_json(shuffle_path)

    # history
    history_path = edir / f"history_{_safe_alnum(test_name)}.json"
    bundle["history"] = _read_json(history_path)

    # bisect
    bisect_path = edir / f"bisect_{_safe_alnum(test_name)}.json"
    bundle["bisect"] = _read_json(bisect_path)

    # static_scan — keyed by source file, not test node ID
    source_file = _SOURCE_FILE_MAP.get(test_name)
    if source_file is not None:
        static_path = edir / f"static_{_safe_alnum(source_file)}.json"
        bundle["static_scan"] = _read_json(static_path)
    else:
        bundle["static_scan"] = None

    return bundle


def _read_json(path: Path) -> dict[str, Any] | None:
    """Return parsed JSON from *path*, or None if the file does not exist."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_coordinator_prompt(test_name: str, bundle: dict[str, Any]) -> str:
    """
    Build the full coordinator prompt by substituting test_name and the
    evidence bundle into the coordinator_prompt.md template.
    """
    template = _COORDINATOR_PROMPT.read_text(encoding="utf-8")

    # Format each subagent's evidence as annotated JSON.
    evidence_parts: list[str] = []
    for agent in _ALL_SUBAGENT_NAMES:
        data = bundle.get(agent)
        if agent == "bisect" and data is None:
            if test_name not in _BISECT_APPLICABLE_TESTS:
                data = {
                    "subagent": "bisect",
                    "test_name": test_name,
                    "verdict": "NOT APPLICABLE — no regression history relevant to this test",
                    "note": (
                        "This test does not have a regression commit in its git history. "
                        "Bisect evidence is not available. Do not penalize convergence_count."
                    ),
                }
        if data is None:
            data = {
                "subagent": agent,
                "test_name": test_name,
                "evidence": "(evidence file not found — subagent may not have run yet)",
                "hypothesis": "(unavailable)",
                "confidence": "low",
            }
        evidence_parts.append(
            f"### {agent.upper()} SUBAGENT\n"
            + json.dumps(data, indent=2)
        )

    evidence_bundle_text = "\n\n".join(evidence_parts)

    prompt = (
        template
        .replace("{test_name}", test_name)
        .replace("{evidence_bundle}", evidence_bundle_text)
    )

    return prompt


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def validate_coordinator_schema(result: dict[str, Any], test_name: str) -> None:
    """
    Raise ValueError if *result* does not satisfy the Coordinator JSON schema.
    """
    required = {
        "test_name",
        "ranked_cause",
        "winning_evidence",
        "convergence_count",
        "all_evidence_summary",
        "confidence",
    }

    missing = required - set(result)
    if missing:
        raise ValueError(
            f"Coordinator JSON missing keys: {sorted(missing)}"
        )

    if result["test_name"] != test_name:
        raise ValueError(
            f"'test_name' must be {test_name!r}, got {result['test_name']!r}"
        )

    if not isinstance(result["ranked_cause"], str) or not result["ranked_cause"].strip():
        raise ValueError("'ranked_cause' must be a non-empty string")

    if not isinstance(result["winning_evidence"], str) or not result["winning_evidence"].strip():
        raise ValueError("'winning_evidence' must be a non-empty string")

    cc = result["convergence_count"]
    if not isinstance(cc, int) or not (1 <= cc <= 5):
        raise ValueError(
            f"'convergence_count' must be an integer 1–5, got {cc!r}"
        )

    if result["confidence"] not in _VALID_CONFIDENCE:
        raise ValueError(
            f"'confidence' must be one of {sorted(_VALID_CONFIDENCE)}, "
            f"got {result['confidence']!r}"
        )

    aes = result["all_evidence_summary"]
    if not isinstance(aes, list) or len(aes) != 5:
        raise ValueError(
            f"'all_evidence_summary' must be a list of exactly 5 entries, "
            f"got {type(aes).__name__} of length {len(aes) if isinstance(aes, list) else '?'}"
        )

    present_agents = set()
    for i, entry in enumerate(aes):
        if not isinstance(entry, dict):
            raise ValueError(
                f"all_evidence_summary[{i}] must be a dict"
            )
        for field in ("subagent", "verdict", "supports_winning_cause"):
            if field not in entry:
                raise ValueError(
                    f"all_evidence_summary[{i}] missing field {field!r}"
                )
        name = entry["subagent"]
        if name not in _ALL_SUBAGENT_NAMES:
            raise ValueError(
                f"all_evidence_summary[{i}] has unknown subagent {name!r}"
            )
        if name in present_agents:
            raise ValueError(
                f"all_evidence_summary contains duplicate subagent {name!r}"
            )
        present_agents.add(name)
        if not isinstance(entry["verdict"], str) or not entry["verdict"].strip():
            raise ValueError(
                f"all_evidence_summary[{i}]['verdict'] must be a non-empty string"
            )
        if not isinstance(entry["supports_winning_cause"], bool):
            raise ValueError(
                f"all_evidence_summary[{i}]['supports_winning_cause'] "
                f"must be a boolean, got {type(entry['supports_winning_cause']).__name__}"
            )

    if present_agents != set(_ALL_SUBAGENT_NAMES):
        missing_agents = set(_ALL_SUBAGENT_NAMES) - present_agents
        raise ValueError(
            f"all_evidence_summary missing subagent(s): {sorted(missing_agents)}"
        )


# ---------------------------------------------------------------------------
# Conservative fallback
# ---------------------------------------------------------------------------

def fallback_coordinator(
    test_name: str,
    bundle: dict[str, Any],
) -> dict[str, Any]:
    """
    Conservative fallback when Bob is unavailable or fails.

    Deliberately refuses to fabricate a ranked root cause.
    Builds honest verdicts directly from the raw evidence fields.
    """
    print("[COORDINATOR] using fallback", file=sys.stderr)

    summaries: list[dict[str, Any]] = []

    for agent in _ALL_SUBAGENT_NAMES:
        data = bundle.get(agent)

        if agent == "bisect" and data is None and test_name not in _BISECT_APPLICABLE_TESTS:
            summaries.append({
                "subagent": "bisect",
                "verdict": (
                    "NOT APPLICABLE — no regression history relevant to this test; "
                    "bisect evidence is not applicable and does not reduce convergence."
                ),
                "supports_winning_cause": False,
            })
            continue

        if data is None:
            summaries.append({
                "subagent": agent,
                "verdict": f"Evidence file not found; {agent} subagent may not have run.",
                "supports_winning_cause": False,
            })
            continue

        # Use hypothesis + confidence from evidence as the verdict.
        hypothesis = data.get("hypothesis", "(no hypothesis recorded)").strip()
        confidence = data.get("confidence", "unknown")
        verdict = f"{hypothesis} [confidence: {confidence}]"
        summaries.append({
            "subagent": agent,
            "verdict": verdict,
            "supports_winning_cause": False,  # fallback cannot determine this
        })

    return {
        "test_name": test_name,
        "ranked_cause": (
            "UNABLE TO SYNTHESIZE VIA FALLBACK — human review required. "
            "Bob was unavailable to perform cross-evidence synthesis. "
            "See all_evidence_summary for raw subagent findings."
        ),
        "winning_evidence": (
            "No synthesis performed. Raw evidence is available in "
            "all_evidence_summary entries."
        ),
        "convergence_count": 1,
        "all_evidence_summary": summaries,
        "confidence": "low",
    }


# ---------------------------------------------------------------------------
# Bob infrastructure
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
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key and value:
            env.setdefault(key, value)

    return env


def _resolve_bob() -> list[str] | None:
    """Return argv prefix for Bob Shell, or None if not available."""
    bob = shutil.which("bob")
    if not bob:
        return None

    bob_path = Path(bob)

    if os.name == "nt" and bob_path.suffix.lower() in {".cmd", ".bat"}:
        node = shutil.which("node")
        if not node:
            return None

        candidates = [
            bob_path.parent / "node_modules" / "@bob" / "bob-shell" / "bob.js",
            bob_path.parent / "node_modules" / "bob-shell" / "bob.js",
        ]
        for candidate in candidates:
            if candidate.exists():
                return [node, str(candidate)]

        return [str(bob_path)]

    return [str(bob_path)]


def _parse_last_message(last_message: Any) -> dict[str, Any]:
    """Parse Bob's last_message (dict, plain JSON, or fenced JSON)."""
    if isinstance(last_message, dict):
        return last_message

    if not isinstance(last_message, str):
        raise ValueError(
            f"last_message has unexpected type {type(last_message).__name__}"
        )

    raw = last_message.strip()
    if not raw:
        raise ValueError("last_message is empty")

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
        try:
            parsed = json.loads(raw[start: end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise ValueError("last_message does not contain valid JSON")


def _save_raw_response(raw: str) -> None:
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (_DEBUG_DIR / "coordinator_raw_response.txt").write_text(
            raw, encoding="utf-8"
        )
    except Exception:
        pass


def _invoke_bob(test_name: str, bundle: dict[str, Any]) -> dict[str, Any] | None:
    """
    Attempt to call Bob Shell with the coordinator synthesis prompt.
    Returns a validated dict on success, or None on any failure.
    """
    argv_prefix = _resolve_bob()
    if argv_prefix is None:
        print("[COORDINATOR] BOB FAILED: Bob Shell not found on PATH", file=sys.stderr)
        return None

    prompt = build_coordinator_prompt(test_name, bundle)
    env = _load_bob_env()

    try:
        proc = subprocess.run(
            argv_prefix + ["run", "--format", "json", prompt],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env=env,
            cwd=str(_HERE),
        )
    except subprocess.TimeoutExpired:
        print("[COORDINATOR] BOB FAILED: timeout", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"[COORDINATOR] BOB FAILED: subprocess error — {exc}", file=sys.stderr)
        return None

    if proc.returncode != 0:
        print(
            f"[COORDINATOR] BOB FAILED: exit code {proc.returncode} — "
            f"{proc.stderr.strip()[:200]}",
            file=sys.stderr,
        )
        return None

    stdout = (proc.stdout or "").strip()
    if not stdout:
        print("[COORDINATOR] BOB FAILED: empty stdout", file=sys.stderr)
        return None

    # Parse the Bob Shell JSON envelope.
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        print(
            f"[COORDINATOR] BOB FAILED: invalid JSON envelope — {exc}",
            file=sys.stderr,
        )
        return None

    if not isinstance(envelope, dict) or "last_message" not in envelope:
        print(
            "[COORDINATOR] BOB FAILED: missing last_message in envelope",
            file=sys.stderr,
        )
        return None

    last_message = envelope["last_message"]
    _save_raw_response(
        last_message if isinstance(last_message, str) else json.dumps(last_message)
    )

    try:
        data = _parse_last_message(last_message)
    except ValueError as exc:
        print(f"[COORDINATOR] BOB FAILED: {exc}", file=sys.stderr)
        return None

    # Enforce test_name.
    data["test_name"] = test_name

    # Coerce convergence_count to int if Bob returned a string.
    if isinstance(data.get("convergence_count"), str):
        try:
            data["convergence_count"] = int(data["convergence_count"])
        except (ValueError, TypeError):
            pass

    # Coerce supports_winning_cause values to bool if Bob returned strings.
    aes = data.get("all_evidence_summary", [])
    if isinstance(aes, list):
        for entry in aes:
            if isinstance(entry, dict):
                swc = entry.get("supports_winning_cause")
                if isinstance(swc, str):
                    entry["supports_winning_cause"] = swc.lower() in {"true", "yes", "1"}

    try:
        validate_coordinator_schema(data, test_name)
    except ValueError as exc:
        print(f"[COORDINATOR] BOB FAILED: schema error — {exc}", file=sys.stderr)
        return None

    return data


# ---------------------------------------------------------------------------
# Evidence persistence
# ---------------------------------------------------------------------------

def _safe_name_coord(test_name: str) -> str:
    """Use the alnum variant for coordinator filenames."""
    return _safe_alnum(test_name)


def _persist_evidence(data: dict[str, Any], test_name: str) -> Path:
    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    output = _EVIDENCE_DIR / f"coordinator_{_safe_name_coord(test_name)}.json"
    output.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_coordinator(
    test_name: str,
    evidence_dir: Path | None = None,
) -> dict[str, Any]:
    """
    Core logic: load evidence, run Bob (or fallback), validate, persist, return.
    """
    if "::" not in test_name:
        raise ValueError(
            "test_name must be a full pytest node ID "
            f"(file::function), got: {test_name!r}"
        )

    bundle = load_evidence_bundle(test_name, evidence_dir=evidence_dir)

    # Log what was loaded.
    for agent, data in bundle.items():
        if data is not None:
            print(
                f"[COORDINATOR] loaded {agent} evidence",
                file=sys.stderr,
            )
        else:
            if agent == "bisect" and test_name not in _BISECT_APPLICABLE_TESTS:
                print(
                    f"[COORDINATOR] bisect not applicable for this test",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[COORDINATOR] WARNING: {agent} evidence not found",
                    file=sys.stderr,
                )

    result = _invoke_bob(test_name, bundle)

    if result is not None:
        print("[COORDINATOR] using real Bob", file=sys.stderr)
    else:
        result = fallback_coordinator(test_name, bundle)

    validate_coordinator_schema(result, test_name)

    out_path = _persist_evidence(result, test_name)
    print(f"[COORDINATOR] evidence persisted to {out_path}", file=sys.stderr)

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    if len(sys.argv) != 2:
        print(
            "Usage: python analyzer/coordinator.py <pytest_node_id>",
            file=sys.stderr,
        )
        print(
            "Example: python analyzer/coordinator.py "
            '"tests/test_a_order.py::test_cache_starts_clean"',
            file=sys.stderr,
        )
        return 2

    test_name = sys.argv[1]

    try:
        result = run_coordinator(test_name)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
