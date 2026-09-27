"""
analyzer/run_static_scan.py — FlakeFinder Step 9c Static Scan subagent entry point.

Usage:
    python analyzer/run_static_scan.py <target_source_file>

Example:
    python analyzer/run_static_scan.py demo-repo/src/regression.py

Architecture:
    1. Try real Bob first.
       Bob is instructed to run the ACTUAL scanner:
           python agents/scripts/static_scan.py "<target>"
    2. Parse and validate the Static Scan JSON schema from Bob's response.
    3. If Bob is unavailable, fails, or returns invalid/non-conforming JSON:
       - Print "[STATIC_SCAN] using fallback" to stderr.
       - Run agents/scripts/static_scan.py "<target>" directly using the
         repository's Python environment.
       - Convert the scanner's actual JSON output to the normalised schema.
    4. Save the raw Bob response to state/debug/static_scan_raw_response.txt.
    5. Persist the normalised evidence to state/evidence/static_<safe_target>.json.
    6. Print the JSON to stdout.

Static Scan JSON schema:
    {
        "subagent": "static_scan",
        "test_name": "<target_source_file>",
        "evidence": "<findings summary>",
        "hypothesis": "<grounded hypothesis>",
        "confidence": "high | medium | low"
    }

Supported targets:
    demo-repo/src/shared_cache.py
    demo-repo/src/async_worker.py
    demo-repo/src/nondeterministic.py
    demo-repo/src/regression.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent.parent

_STATIC_PROMPT = _HERE / "bob_prompts" / "static_prompt.md"

_SCANNER_SCRIPT = _HERE / "agents" / "scripts" / "static_scan.py"

_DEBUG_DIR = _HERE / "state" / "debug"

_EVIDENCE_DIR = _HERE / "state" / "evidence"

_ENV_FILE = _HERE / ".env"

_SUPPORTED_TARGETS = frozenset(
    {
        "demo-repo/src/shared_cache.py",
        "demo-repo/src/async_worker.py",
        "demo-repo/src/nondeterministic.py",
        "demo-repo/src/regression.py",
    }
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
    _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    (_DEBUG_DIR / "static_scan_raw_response.txt").write_text(
        raw,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Bob prompt
# ---------------------------------------------------------------------------


def _build_static_scan_prompt(target: str) -> str:
    template = _STATIC_PROMPT.read_text(encoding="utf-8")

    # The static_prompt.md uses {test_file} and {test_name} placeholders.
    # For the static scan subagent the target source file serves as both.
    prompt = template.replace("{test_file}", target).replace(
        "{test_name}", target
    )

    # Append an explicit instruction to run the actual scanner.
    prompt += (
        "\n\nYou are the Static Analysis subagent for FlakeFinder."
        "\n\nYou MUST execute the actual static analysis script:"
        f"\n\n    python agents/scripts/static_scan.py {target}"
        "\n\nDo not guess findings from reading the source."
        "\nDo not merely describe what the code appears to do."
        "\nThe command output is the evidence."
        "\n\nReturn exactly one JSON object:"
        "\n{"
        '\n  "subagent": "static_scan",'
        f'\n  "test_name": "{target}",'
        '\n  "evidence": "...",'
        '\n  "hypothesis": "...",'
        '\n  "confidence": "high | medium | low"'
        "\n}"
        "\n\nThe evidence must contain the actual findings reported by static_scan.py."
        "\nIf the scanner reports no findings, that is valid evidence."
        "\nReturn only the JSON object."
        "\nNo Markdown."
        "\nNo code fences."
        "\nNo explanation before or after the JSON."
    )

    return prompt


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate(data: dict[str, Any], target: str) -> None:
    missing = _REQUIRED_FIELDS - set(data)

    if missing:
        raise ValueError(
            "missing required fields: " + ", ".join(sorted(missing))
        )

    if data["subagent"] != "static_scan":
        raise ValueError("'subagent' must be 'static_scan'")

    if data["test_name"] != target:
        raise ValueError(f"'test_name' must be {target!r}")

    if not isinstance(data["evidence"], str) or not data["evidence"].strip():
        raise ValueError("'evidence' must be a non-empty string")

    if (
        not isinstance(data["hypothesis"], str)
        or not data["hypothesis"].strip()
    ):
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
            bob_path.parent
            / "node_modules"
            / "@bob"
            / "bob-shell"
            / "bob.js",
            bob_path.parent / "node_modules" / "bob-shell" / "bob.js",
        ]

        for candidate in candidates:
            if candidate.exists():
                return [node, str(candidate)]

        return [str(bob_path)]

    return [str(bob_path)]


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


def _invoke_bob(target: str) -> dict[str, Any] | None:
    """
    Run the actual Bob Shell investigation.

    Returns a validated dict on success, or None on any failure.
    """
    try:
        bob_command = _resolve_bob()
    except Exception as exc:
        print(f"[STATIC_SCAN] BOB FAILED: {exc}", file=sys.stderr)
        return None

    prompt = _build_static_scan_prompt(target)
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
        print("[STATIC_SCAN] BOB FAILED: timeout", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"[STATIC_SCAN] BOB FAILED: {exc}", file=sys.stderr)
        return None

    if completed.returncode != 0:
        print(
            f"[STATIC_SCAN] BOB FAILED: exit code {completed.returncode}",
            file=sys.stderr,
        )
        if completed.stderr:
            print(completed.stderr, file=sys.stderr)
        return None

    stdout = completed.stdout or ""

    if not stdout.strip():
        print("[STATIC_SCAN] BOB FAILED: empty stdout", file=sys.stderr)
        return None

    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        print(
            f"[STATIC_SCAN] BOB FAILED: invalid JSON envelope — {exc}",
            file=sys.stderr,
        )
        return None

    if not isinstance(envelope, dict):
        print(
            "[STATIC_SCAN] BOB FAILED: JSON envelope is not an object",
            file=sys.stderr,
        )
        return None

    last_message = envelope.get("last_message")

    if last_message is None:
        print(
            "[STATIC_SCAN] BOB FAILED: missing last_message",
            file=sys.stderr,
        )
        return None

    try:
        data = _parse_last_message(last_message)
    except ValueError as exc:
        print(f"[STATIC_SCAN] BOB FAILED: {exc}", file=sys.stderr)
        return None

    try:
        _validate(data, target)
    except ValueError as exc:
        print(
            f"[STATIC_SCAN] BOB FAILED: invalid result — {exc}",
            file=sys.stderr,
        )
        return None

    return data


# ---------------------------------------------------------------------------
# Python executable resolution for fallback
# ---------------------------------------------------------------------------


def _resolve_python() -> str:
    """
    Return the Python executable to use for the fallback scanner.

    Preference order:
      1. Active venv's python (FlakeFinder/venv/Scripts/python.exe on Windows,
         FlakeFinder/venv/bin/python on Unix).
      2. sys.executable (the interpreter running this file).

    Never selects WSL bash — the scanner is a pure Python script.
    """
    if os.name == "nt":
        venv_python = _HERE / "venv" / "Scripts" / "python.exe"
    else:
        venv_python = _HERE / "venv" / "bin" / "python"

    if venv_python.exists():
        return str(venv_python)

    return sys.executable


# ---------------------------------------------------------------------------
# Fallback: run the actual scanner
# ---------------------------------------------------------------------------


def _scanner_output_to_normalized(
    scanner_data: dict[str, Any],
    target: str,
) -> dict[str, Any]:
    """
    Convert static_scan.py's output schema to the normalised subagent schema.

    scanner_data shape:
        {
            "subagent": "static_scan",
            "target": "...",
            "findings": [...],
            "finding_count": N
        }
    """
    if "finding_count" not in scanner_data:
        raise ValueError("scanner output missing 'finding_count'")

    finding_count = scanner_data["finding_count"]

    if not isinstance(finding_count, int) or finding_count < 0:
        raise ValueError(
            f"'finding_count' must be a non-negative integer, "
            f"got {finding_count!r}"
        )

    findings = scanner_data.get("findings", [])

    if finding_count == 0:
        evidence = (
            f"Scanner reported 0 findings for {target}. No static issues detected."
        )
        hypothesis = (
            "No statically detectable flakiness patterns found in this file. "
            "The flakiness may originate elsewhere or require dynamic analysis."
        )
        confidence = "low"
    else:
        # Build a compact evidence summary from the actual findings.
        finding_parts = []
        for f in findings:
            line = f.get("line", "?")
            ftype = f.get("type", "unknown")
            ev = f.get("evidence", "")
            finding_parts.append(f"line {line}, type '{ftype}', '{ev}'")

        findings_text = "; ".join(finding_parts)
        evidence = (
            f"Scanner reported {finding_count} finding"
            f"{'s' if finding_count != 1 else ''}: {findings_text}."
        )

        # Build a hypothesis grounded in the actual finding types.
        types_found = {f.get("type", "") for f in findings}

        parts = []
        if "random_usage" in types_found:
            parts.append(
                "imports the random module without a fixed seed, "
                "introducing non-determinism"
            )
        if "time_usage" in types_found:
            parts.append("uses the time module, which can cause timing-sensitive flakiness")
        if "async_task" in types_found:
            parts.append(
                "uses asyncio.create_task, which may produce race conditions "
                "if tasks are not awaited"
            )
        if "module_mutable_state" in types_found:
            parts.append(
                "defines module-level mutable state that may leak between tests"
            )

        if parts:
            hypothesis = (
                f"The file {'; '.join(parts)}. "
                "This is consistent with the observed flakiness."
            )
        else:
            hypothesis = (
                f"The file has {finding_count} static finding(s) that may "
                "contribute to flaky test behaviour."
            )

        confidence = "high"

    return {
        "subagent": "static_scan",
        "test_name": target,
        "evidence": evidence,
        "hypothesis": hypothesis,
        "confidence": confidence,
    }


def _fallback(target: str) -> dict[str, Any]:
    """
    Run agents/scripts/static_scan.py and convert its output to the
    normalised schema.  Never invents findings.
    """
    if not _SCANNER_SCRIPT.exists():
        raise RuntimeError(
            f"Static scan script does not exist: {_SCANNER_SCRIPT}"
        )

    python_exe = _resolve_python()

    command = [python_exe, str(_SCANNER_SCRIPT), target]

    env = os.environ.copy()

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
            timeout=60,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("fallback static scan script timed out") from exc
    except Exception as exc:
        raise RuntimeError(f"fallback static scan script failed: {exc}") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.strip() if completed.stderr else ""
        message = f"fallback script exited with code {completed.returncode}"
        if stderr:
            message += f" — {stderr}"
        raise RuntimeError(message)

    stdout = completed.stdout.strip() if completed.stdout else ""

    if not stdout:
        raise RuntimeError("fallback static scan script produced no output")

    try:
        scanner_data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"fallback scanner output is not valid JSON — {exc}\n"
            f"Output was: {stdout[:200]!r}"
        ) from exc

    if not isinstance(scanner_data, dict):
        raise RuntimeError("fallback scanner JSON must be an object")

    return _scanner_output_to_normalized(scanner_data, target)


# ---------------------------------------------------------------------------
# Evidence persistence
# ---------------------------------------------------------------------------


def _persist_evidence(data: dict[str, Any], target: str) -> Path:
    _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    output = _EVIDENCE_DIR / ("static_" + _safe_name(target) + ".json")

    output.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return output


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(target: str) -> dict[str, Any]:
    """
    Core logic: receive a source file path and return a validated
    Static Scan JSON dict.

    Tries Bob first; falls back to static_scan.py on any Bob failure.
    """
    if not target:
        raise ValueError("target must be a non-empty source file path")

    data = _invoke_bob(target)

    if data is not None:
        print("[STATIC_SCAN] using real Bob")
    else:
        print("[STATIC_SCAN] using fallback", file=sys.stderr)
        data = _fallback(target)

    _validate(data, target)
    _persist_evidence(data, target)

    return data


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "usage: python analyzer/run_static_scan.py "
            '"demo-repo/src/regression.py"',
            file=sys.stderr,
        )
        return 2

    target = sys.argv[1]

    try:
        result = run(target)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
