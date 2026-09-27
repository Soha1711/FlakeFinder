"""
analyzer/run_shuffle.py — FlakeFinder Step 9 Shuffle subagent entry point.

Usage:
    python analyzer/run_shuffle.py <pytest_node_id>

Example:
    python analyzer/run_shuffle.py tests/test_a_order.py::test_cache_starts_clean
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
_SHUFFLE_PROMPT = _HERE / "bob_prompts" / "shuffle_prompt.md"
_OUTPUT_DIR = _HERE / "state" / "evidence"
_DEBUG_DIR = _HERE / "state" / "debug"
_RUN_SHUFFLED_SH = _HERE / "agents" / "scripts" / "run_shuffled.sh"


# ---------------------------------------------------------------------------
# Re-use Bob infrastructure from run_planner
# ---------------------------------------------------------------------------

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from run_planner import _resolve_bob, _load_bob_env  # noqa: E402


_VALID_CONFIDENCE = frozenset({"high", "medium", "low"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_name(test_name: str) -> str:
    return re.sub(r"[^\w\-]", "_", test_name)


def _build_shuffle_prompt(test_name: str) -> str:
    template = _SHUFFLE_PROMPT.read_text(encoding="utf-8")
    return template.replace("{{TEST_NAME}}", test_name)


def _save_raw_last_message(last_message) -> None:
    """Save Bob's raw Shuffle response for diagnostics."""
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)

        if isinstance(last_message, str):
            raw_text = last_message
        else:
            raw_text = json.dumps(last_message, indent=2)

        (_DEBUG_DIR / "shuffle_raw_response.txt").write_text(
            raw_text,
            encoding="utf-8",
        )
    except Exception:
        pass


def _parse_last_message(last_message) -> dict | None:
    """
    Accept:
      1. dict
      2. plain JSON text
      3. JSON enclosed in a Markdown code fence
    """
    if isinstance(last_message, dict):
        return last_message

    if not isinstance(last_message, str):
        print(
            "[SHUFFLE] BOB FAILED: last_message has unexpected type "
            f"{type(last_message).__name__}",
            file=sys.stderr,
        )
        return None

    raw_message = last_message.strip()

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
            f"[SHUFFLE] BOB FAILED: last_message is not valid JSON — {exc}",
            file=sys.stderr,
        )
        return None

    if not isinstance(data, dict):
        print(
            "[SHUFFLE] BOB FAILED: parsed last_message is not a JSON object",
            file=sys.stderr,
        )
        return None

    return data


def _validate(data: dict, expected_test_name: str) -> None:
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
            f"Shuffle JSON missing keys: {sorted(missing)}"
        )

    if data["subagent"] != "shuffle":
        raise ValueError(
            f"'subagent' must be 'shuffle', got: {data['subagent']!r}"
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
# Bob invocation
# ---------------------------------------------------------------------------

def _invoke_bob(test_name: str) -> dict | None:
    argv_prefix = _resolve_bob()

    if argv_prefix is None:
        return None

    prompt_text = _build_shuffle_prompt(test_name)
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
            f"[SHUFFLE] BOB FAILED: subprocess error — {exc}",
            file=sys.stderr,
        )
        return None

    if proc.returncode != 0:
        print(
            f"[SHUFFLE] BOB FAILED: non-zero exit ({proc.returncode}) — "
            f"{proc.stderr.strip()[:200]}",
            file=sys.stderr,
        )
        return None

    raw = proc.stdout.strip()

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"[SHUFFLE] BOB FAILED: response is not valid JSON — {exc}",
            file=sys.stderr,
        )
        return None

    if not isinstance(envelope, dict) or "last_message" not in envelope:
        print(
            "[SHUFFLE] BOB FAILED: JSON envelope missing 'last_message'",
            file=sys.stderr,
        )
        return None

    last_message = envelope["last_message"]

    _save_raw_last_message(last_message)

    data = _parse_last_message(last_message)

    if data is None:
        return None

    data["test_name"] = test_name

    try:
        _validate(data, test_name)
    except ValueError as exc:
        print(
            f"[SHUFFLE] BOB FAILED: schema validation error — {exc}",
            file=sys.stderr,
        )
        return None

    return data


# ---------------------------------------------------------------------------
# Bash executable resolution
# ---------------------------------------------------------------------------

_GIT_BASH_DEFAULT = Path("C:/Program Files/Git/bin/bash.exe")


def _find_bash() -> str:
    if os.name == "nt":
        if _GIT_BASH_DEFAULT.exists():
            return str(_GIT_BASH_DEFAULT)

        return shutil.which("bash") or "bash"

    return shutil.which("bash") or "bash"


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------

def _fallback(test_name: str) -> dict:
    """
    Run the actual Shuffle shell script.

    Never invent pass/fail/run counts.
    """
    bash_exe = _find_bash()
    script_path = str(_RUN_SHUFFLED_SH).replace("\\", "/")

    fallback_env = _load_bob_env()

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
            f"[SHUFFLE] fallback script failed to run — {exc}"
        ) from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"[SHUFFLE] fallback script exited with code "
            f"{proc.returncode} — {proc.stderr.strip()[:200]}"
        )

    script_output = proc.stdout.strip()

    if not script_output:
        raise RuntimeError(
            "[SHUFFLE] fallback script produced no output"
        )

    try:
        script_data = json.loads(script_output)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"[SHUFFLE] fallback script output is not valid JSON — {exc}\n"
            f"Output was: {script_output[:200]!r}"
        ) from exc

    if not isinstance(script_data, dict):
        raise RuntimeError(
            "[SHUFFLE] fallback script JSON must be an object"
        )

    required = ("runs", "passes", "failures")

    missing = [
        key for key in required
        if key not in script_data
    ]

    if missing:
        raise RuntimeError(
            f"[SHUFFLE] fallback script JSON missing required keys: "
            f"{missing}\nGot: {script_data}"
        )

    runs = script_data["runs"]
    passes = script_data["passes"]
    failures = script_data["failures"]

    evidence = (
        f"runs: {runs}, passes: {passes}, failures: {failures}"
    )

    hypothesis = (
        f"The target test was executed {runs} times in shuffled order, "
        f"with {failures} failures. Immediate predecessors are contextual "
        f"evidence only and do not by themselves establish causation."
    )

    return {
        "subagent": "shuffle",
        "test_name": test_name,
        "evidence": evidence,
        "hypothesis": hypothesis,
        "confidence": "medium",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run(test_name: str) -> dict:
    if "::" not in test_name:
        raise ValueError(
            "test_name must be a full pytest node ID "
            f"(file::function), got: {test_name!r}"
        )

    result = _invoke_bob(test_name)

    if result is None:
        print(
            "[SHUFFLE] using fallback",
            file=sys.stderr,
        )
        result = _fallback(test_name)
    else:
        print(
            "[SHUFFLE] using real Bob",
            file=sys.stderr,
        )

    _validate(result, test_name)

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    output_file = (
        _OUTPUT_DIR
        / f"shuffle_{_safe_name(test_name)}.json"
    )

    output_file.write_text(
        json.dumps(result, indent=2) + "\n",
        encoding="utf-8",
    )

    return result


def main() -> None:
    if len(sys.argv) != 2:
        print(
            "Usage: python analyzer/run_shuffle.py <pytest_node_id>",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        data = run(sys.argv[1])
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()