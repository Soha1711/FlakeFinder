from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


_HERE = Path(__file__).resolve().parent.parent

_BISECT_PROMPT = (
    _HERE / "bob_prompts" / "bisect_prompt.md"
)

_BISECT_SCRIPT = (
    _HERE
    / "agents"
    / "scripts"
    / "git_bisect_runner.sh"
)

_DEBUG_DIR = (
    _HERE / "state" / "debug"
)

_EVIDENCE_DIR = (
    _HERE / "state" / "evidence"
)

_ENV_FILE = _HERE / ".env"

_EXPECTED_FIRST_BAD = (
    "04805c176d22dc3b07ed581cfa99fc1e7b5dd37e"
)

_REQUIRED_FIELDS = {
    "subagent",
    "test_name",
    "evidence",
    "hypothesis",
    "confidence",
}


def _safe_name(value: str) -> str:
    return "".join(
        char
        if char.isalnum() or char in "._-"
        else "_"
        for char in value
    )


def _build_bisect_prompt(test_name: str) -> str:
    template = _BISECT_PROMPT.read_text(
        encoding="utf-8"
    )

    return template.replace(
        "{test_name}",
        test_name,
    )


def _save_raw_last_message(
    raw: str,
) -> None:
    _DEBUG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    output = (
        _DEBUG_DIR
        / "bisect_raw_response.txt"
    )

    output.write_text(
        raw,
        encoding="utf-8",
    )


def _parse_last_message(
    last_message: Any,
) -> dict[str, Any]:
    if isinstance(last_message, dict):
        return last_message

    if not isinstance(
        last_message,
        str,
    ):
        raise ValueError(
            "last_message is not valid JSON"
        )

    raw = last_message.strip()

    if not raw:
        raise ValueError(
            "last_message is empty"
        )

    _save_raw_last_message(raw)

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

            if candidate.lower().startswith(
                "json"
            ):
                candidate = (
                    candidate[4:].lstrip()
                )

            if not candidate:
                continue

            try:
                parsed = json.loads(
                    candidate
                )

                if isinstance(parsed, dict):
                    return parsed

            except json.JSONDecodeError:
                continue

    # JSON embedded in surrounding text.
    start = raw.find("{")
    end = raw.rfind("}")

    if start >= 0 and end > start:
        candidate = raw[
            start : end + 1
        ]

        try:
            parsed = json.loads(
                candidate
            )

            if isinstance(parsed, dict):
                return parsed

        except json.JSONDecodeError:
            pass

    raise ValueError(
        "last_message is not valid JSON"
    )


def _validate(
    data: dict[str, Any],
    test_name: str,
) -> None:
    missing = (
        _REQUIRED_FIELDS
        - set(data)
    )

    if missing:
        raise ValueError(
            "missing required fields: "
            + ", ".join(
                sorted(missing)
            )
        )

    if data["subagent"] != "bisect":
        raise ValueError(
            "'subagent' must be 'bisect'"
        )

    if data["test_name"] != test_name:
        raise ValueError(
            "'test_name' must be "
            f"{test_name!r}"
        )

    if (
        not isinstance(
            data["evidence"],
            str,
        )
        or not data["evidence"].strip()
    ):
        raise ValueError(
            "'evidence' must be "
            "a non-empty string"
        )

    if (
        not isinstance(
            data["hypothesis"],
            str,
        )
        or not data["hypothesis"].strip()
    ):
        raise ValueError(
            "'hypothesis' must be "
            "a non-empty string"
        )

    if data["confidence"] not in {
        "high",
        "medium",
        "low",
    }:
        raise ValueError(
            "'confidence' must be one of "
            "'high', 'medium', or 'low'"
        )

    if (
        _EXPECTED_FIRST_BAD
        not in data["evidence"]
    ):
        raise ValueError(
            "evidence does not contain "
            "the expected first bad commit "
            f"{_EXPECTED_FIRST_BAD}"
        )


def _load_bob_env() -> dict[str, str]:
    env = os.environ.copy()

    if (
        env.get("BOB_API_KEY")
        or env.get(
            "BOBSHELL_API_KEY"
        )
    ):
        return env

    if not _ENV_FILE.exists():
        return env

    try:
        lines = _ENV_FILE.read_text(
            encoding="utf-8"
        ).splitlines()
    except OSError:
        return env

    for line in lines:
        line = line.strip()

        if (
            not line
            or line.startswith("#")
        ):
            continue

        if "=" not in line:
            continue

        key, value = line.split(
            "=",
            1,
        )

        key = key.strip()
        value = value.strip()

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {
                '"',
                "'",
            }
        ):
            value = value[1:-1]

        if key and value:
            env.setdefault(
                key,
                value,
            )

    return env


def _resolve_bob() -> list[str]:
    bob = shutil.which("bob")

    if not bob:
        raise FileNotFoundError(
            "Bob Shell executable was not found"
        )

    bob_path = Path(bob)

    if (
        os.name == "nt"
        and bob_path.suffix.lower()
        in {
            ".cmd",
            ".bat",
        }
    ):
        node = shutil.which("node")

        if not node:
            raise FileNotFoundError(
                "Node.js was not found "
                "while resolving Bob Shell"
            )

        candidates = [
            (
                bob_path.parent
                / "node_modules"
                / "@bob"
                / "bob-shell"
                / "bob.js"
            ),
            (
                bob_path.parent
                / "node_modules"
                / "bob-shell"
                / "bob.js"
            ),
        ]

        for candidate in candidates:
            if candidate.exists():
                return [
                    node,
                    str(candidate),
                ]

        return [
            str(bob_path)
        ]

    return [
        str(bob_path)
    ]


def _invoke_bob(
    test_name: str,
) -> dict[str, Any] | None:
    """
    Run the actual Bob Shell investigation.

    Bob receives the complete investigation prompt as
    the positional argument to:

        bob run --format json <prompt>

    stdin is deliberately disconnected so Bob cannot
    switch into an interactive/orientation workflow.
    """

    try:
        bob_command = _resolve_bob()
    except Exception as exc:
        print(
            f"[BISECT] BOB FAILED: {exc}",
            file=sys.stderr,
        )
        return None

    prompt = _build_bisect_prompt(
        test_name
    )

    command = [
        *bob_command,
        "run",
        "--format",
        "json",
        prompt,
    ]

    env = _load_bob_env()

    try:
        completed = subprocess.run(
            command,
            cwd=_HERE,
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
        print(
            "[BISECT] BOB FAILED: timeout",
            file=sys.stderr,
        )
        return None

    except Exception as exc:
        print(
            f"[BISECT] BOB FAILED: {exc}",
            file=sys.stderr,
        )
        return None

    if completed.returncode != 0:
        print(
            "[BISECT] BOB FAILED: "
            f"exit code {completed.returncode}",
            file=sys.stderr,
        )

        if completed.stderr:
            print(
                completed.stderr,
                file=sys.stderr,
            )

        return None

    stdout = completed.stdout or ""

    if not stdout.strip():
        print(
            "[BISECT] BOB FAILED: "
            "empty stdout",
            file=sys.stderr,
        )
        return None

    try:
        envelope = json.loads(
            stdout
        )
    except json.JSONDecodeError as exc:
        print(
            "[BISECT] BOB FAILED: "
            f"invalid JSON envelope — {exc}",
            file=sys.stderr,
        )
        return None

    if not isinstance(
        envelope,
        dict,
    ):
        print(
            "[BISECT] BOB FAILED: "
            "JSON envelope is not an object",
            file=sys.stderr,
        )
        return None

    last_message = envelope.get(
        "last_message"
    )

    if last_message is None:
        print(
            "[BISECT] BOB FAILED: "
            "missing last_message",
            file=sys.stderr,
        )
        return None

    try:
        data = _parse_last_message(
            last_message
        )
    except ValueError as exc:
        print(
            f"[BISECT] BOB FAILED: {exc}",
            file=sys.stderr,
        )
        return None

    try:
        _validate(
            data,
            test_name,
        )
    except ValueError as exc:
        print(
            "[BISECT] BOB FAILED: "
            f"invalid result — {exc}",
            file=sys.stderr,
        )
        return None

    return data


def _find_bash() -> str:
    """
    Prefer Git for Windows bash.exe.

    This prevents WSL's bash.exe from being selected
    accidentally.
    """

    if os.name == "nt":
        candidates = [
            Path(
                r"C:\Program Files\Git\bin\bash.exe"
            ),
            Path(
                r"C:\Program Files (x86)\Git\bin\bash.exe"
            ),
        ]

        for candidate in candidates:
            if candidate.exists():
                return str(candidate)

    bash = shutil.which("bash")

    if bash:
        return bash

    raise FileNotFoundError(
        "Git Bash was not found"
    )


def _fallback(
    test_name: str,
) -> dict[str, Any]:
    if not _BISECT_SCRIPT.exists():
        raise RuntimeError(
            "Bisect script does not exist: "
            f"{_BISECT_SCRIPT}"
        )

    bash = _find_bash()

    command = [
        bash,
        str(_BISECT_SCRIPT),
        test_name,
        "10",
        "1",
    ]

    env = os.environ.copy()

    venv_scripts = (
        _HERE / "venv" / "Scripts"
    )

    if venv_scripts.exists():
        env["PATH"] = (
            str(venv_scripts)
            + os.pathsep
            + env.get("PATH", "")
        )

    try:
        completed = subprocess.run(
            command,
            cwd=_HERE,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=env,
        )

    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "fallback bisect script timed out"
        ) from exc

    except Exception as exc:
        raise RuntimeError(
            "fallback bisect script failed: "
            f"{exc}"
        ) from exc

    if completed.returncode != 0:
        stderr = (
            completed.stderr.strip()
            if completed.stderr
            else ""
        )

        message = (
            "fallback script exited with "
            f"code {completed.returncode}"
        )

        if stderr:
            message += (
                f" — {stderr}"
            )

        raise RuntimeError(
            message
        )

    stdout = (
        completed.stdout.strip()
        if completed.stdout
        else ""
    )

    if not stdout:
        raise RuntimeError(
            "fallback bisect script "
            "produced no output"
        )

    if (
        _EXPECTED_FIRST_BAD
        not in stdout
    ):
        raise RuntimeError(
            "fallback bisect output does not "
            "contain the expected first bad "
            f"commit {_EXPECTED_FIRST_BAD}"
        )

    result = {
        "subagent": "bisect",
        "test_name": test_name,
        "evidence": (
            "Actual git-bisect runner completed. "
            "Expected first bad commit confirmed: "
            f"{_EXPECTED_FIRST_BAD}. "
            "The commit is the regression "
            "introduction identified by the "
            "scripted bisect."
        ),
        "hypothesis": (
            "The regression was introduced by "
            "the first bad commit identified "
            "by git bisect."
        ),
        "confidence": "high",
    }

    _validate(
        result,
        test_name,
    )

    return result


def _persist_evidence(
    data: dict[str, Any],
    test_name: str,
) -> Path:
    _EVIDENCE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    output = (
        _EVIDENCE_DIR
        / (
            "bisect_"
            + _safe_name(test_name)
            + ".json"
        )
    )

    output.write_text(
        json.dumps(
            data,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    return output


def run(
    test_name: str,
) -> dict[str, Any]:
    if "::" not in test_name:
        raise ValueError(
            "test_name must be a full pytest "
            "node ID such as "
            "'tests/test_d_regression.py::"
            "test_calculate_total'"
        )

    data = _invoke_bob(
        test_name
    )

    if data is not None:
        print(
            "[BISECT] using real Bob"
        )
    else:
        print(
            "[BISECT] using fallback",
            file=sys.stderr,
        )

        data = _fallback(
            test_name
        )

    _validate(
        data,
        test_name,
    )

    _persist_evidence(
        data,
        test_name,
    )

    return data


def main() -> int:
    if len(sys.argv) != 2:
        print(
            "usage: python "
            "analyzer/run_bisect.py "
            '"tests/test_d_regression.py::'
            'test_calculate_total"',
            file=sys.stderr,
        )
        return 2

    test_name = sys.argv[1]

    try:
        result = run(
            test_name
        )

    except Exception as exc:
        print(
            f"ERROR: {exc}",
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            result,
            indent=2,
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )