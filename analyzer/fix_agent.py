"""
analyzer/fix_agent.py — FlakeFinder Step 12 Fix Agent entry point.

Usage:
    python analyzer/fix_agent.py <pytest_node_id>

Example:
    python analyzer/fix_agent.py "tests/test_a_order.py::test_cache_starts_clean"

Architecture:
    1. Accept a pytest node ID.
    2. Load the corresponding Coordinator JSON from state/evidence/.
       If missing, report clearly that Step 11 input is missing and stop.
    3. Load bob_prompts/fix_prompt.md and construct prompt.
    4. Invoke Bob Shell using the project's standard Bob invocation conventions.
    5. Parse response containing:
       - unified diff
       - JSON metadata block
    6. Strictly validate JSON schema, test_name, confidence, subagent == "fix_agent".
    7. Strictly reject forbidden verification claims (e.g. "verified", "confirmed",
       "fix works", "tests pass", etc.).
    8. Strictly validate unified diff structure (reject missing diff or non-diff).
    9. If Bob fails, times out, or violates validation:
       - Fail safely and activate conservative fallback.
       - NEVER fabricate a diff.
       - Provide structured JSON explaining human review is required.
    10. Persist proposed diff to state/evidence/fix_<safe_name>.diff on success.
    11. Persist structured JSON to state/evidence/fix_<safe_name>.json.
    12. Print structured JSON to stdout.

Schema:
    {
      "subagent": "fix_agent",
      "test_name": "...",
      "fix_summary": "...",
      "justification": "...",
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

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_FIX_PROMPT = _HERE / "bob_prompts" / "fix_prompt.md"
_EVIDENCE_DIR = _HERE / "state" / "evidence"
_DEBUG_DIR = _HERE / "state" / "debug"
_ENV_FILE = _HERE / ".env"

_VALID_CONFIDENCE = frozenset({"high", "medium", "low"})

_REQUIRED_KEYS = frozenset({
    "subagent",
    "test_name",
    "fix_summary",
    "justification",
    "confidence",
})

# Forbidden phrases that claim the fix has been verified or confirmed to work.
FORBIDDEN_VERIFICATION_CLAIMS = (
    "confirmed",
    "verified",
    "tested and works",
    "guaranteed",
    "fix works",
    "tests pass",
    "test passes",
    "tests passed",
    "issue resolved",
    "issues resolved",
    "successfully fixes",
    "successfully fixed",
)


# ---------------------------------------------------------------------------
# Safe name helpers
# ---------------------------------------------------------------------------

def _safe_alnum(value: str) -> str:
    """Standard alnum safe-name convention matching coordinator/bisect/history."""
    return "".join(
        c if c.isalnum() or c in "._-" else "_"
        for c in value
    )


def _safe_re(value: str) -> str:
    """Regex-based safe-name convention matching isolation/shuffle."""
    return re.sub(r"[^\w\-]", "_", value)


# ---------------------------------------------------------------------------
# Evidence loading
# ---------------------------------------------------------------------------

def load_coordinator_evidence(
    test_name: str,
    evidence_dir: Path | None = None,
) -> dict[str, Any]:
    """
    Load the Coordinator JSON file for test_name from evidence_dir.

    Raises FileNotFoundError if no coordinator file exists for this test.
    Raises ValueError if the file contains invalid JSON.
    """
    edir = evidence_dir or _EVIDENCE_DIR

    # Check both alnum and regex filename variations.
    candidates = [
        edir / f"coordinator_{_safe_alnum(test_name)}.json",
        edir / f"coordinator_{_safe_re(test_name)}.json",
    ]

    coord_path: Path | None = None
    for cand in candidates:
        if cand.exists():
            coord_path = cand
            break

    if coord_path is None:
        raise FileNotFoundError(
            f"Coordinator evidence not found for {test_name}. "
            f"Step 11 input is missing. Looked in: {edir}"
        )

    try:
        content = coord_path.read_text(encoding="utf-8")
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError(f"Coordinator file at {coord_path} did not contain a JSON object.")
        return data
    except json.JSONDecodeError as exc:
        raise ValueError(f"Corrupted Coordinator JSON at {coord_path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_fix_prompt(
    test_name: str,
    coordinator_data: dict[str, Any],
    prompt_path: Path | None = None,
) -> str:
    """
    Build the fix prompt by substituting test_name and coordinator_json
    into bob_prompts/fix_prompt.md.
    """
    template_path = prompt_path or _FIX_PROMPT
    if not template_path.exists():
        raise FileNotFoundError(f"Fix prompt template not found: {template_path}")

    template = template_path.read_text(encoding="utf-8")
    coord_json_str = json.dumps(coordinator_data, indent=2)

    prompt = (
        template
        .replace("{test_name}", test_name)
        .replace("{coordinator_json}", coord_json_str)
    )
    return prompt


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def check_forbidden_verification_claims(data: dict[str, Any]) -> None:
    """
    Raise ValueError if fix_summary or justification contains forbidden claims
    suggesting the fix is confirmed, verified, tested, or guaranteed.
    """
    for field in ("fix_summary", "justification"):
        val = data.get(field)
        if not isinstance(val, str):
            continue
        text_lower = val.lower()
        for phrase in FORBIDDEN_VERIFICATION_CLAIMS:
            pattern = r"(?:\b|_)" + re.escape(phrase) + r"(?:\b|_)"
            if re.search(pattern, text_lower):
                raise ValueError(
                    f"Forbidden verification claim detected in '{field}': '{phrase}'. "
                    "The Fix Agent must NOT claim that the fix is verified or works."
                )


def validate_fix_schema(data: dict[str, Any], expected_test_name: str) -> None:
    """
    Strictly validate the Fix Agent JSON schema.
    Raises ValueError on any violation.
    """
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object, got {type(data).__name__}")

    missing = _REQUIRED_KEYS - set(data.keys())
    if missing:
        raise ValueError(f"Fix JSON missing required keys: {sorted(missing)}")

    if data["subagent"] != "fix_agent":
        raise ValueError(
            f"Expected subagent 'fix_agent', got {data['subagent']!r}"
        )

    if data["test_name"] != expected_test_name:
        raise ValueError(
            f"Expected test_name {expected_test_name!r}, got {data['test_name']!r}"
        )

    if data["confidence"] not in _VALID_CONFIDENCE:
        raise ValueError(
            f"Invalid confidence {data['confidence']!r}; must be one of {sorted(_VALID_CONFIDENCE)}"
        )

    if not isinstance(data["fix_summary"], str) or not data["fix_summary"].strip():
        raise ValueError("'fix_summary' must be a non-empty string")

    if not isinstance(data["justification"], str) or not data["justification"].strip():
        raise ValueError("'justification' must be a non-empty string")

    check_forbidden_verification_claims(data)


# ---------------------------------------------------------------------------
# Diff and JSON response parsing
# ---------------------------------------------------------------------------

def _strip_markdown_fence(text: str, lang: str = "") -> str:
    """Strip surrounding markdown code fences from text."""
    s = text.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        first_line = lines[0].strip().lower()
        if lang:
            if first_line.startswith(f"```{lang}") or first_line == "```":
                lines = lines[1:]
        else:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s


def _is_recognizable_diff(diff_text: str) -> bool:
    """Check if the text has standard unified diff hallmarks."""
    if not diff_text or not diff_text.strip():
        return False

    has_minus_header = bool(re.search(r"^--- [^\n]+", diff_text, re.MULTILINE))
    has_plus_header = bool(re.search(r"^\+\+\+ [^\n]+", diff_text, re.MULTILINE))
    has_hunk_header = bool(re.search(r"^@@ [^\n]+ @@", diff_text, re.MULTILINE))
    has_git_header = bool(re.search(r"^diff --git ", diff_text, re.MULTILINE))

    # Must have either hunk headers, git header, or unified +/- file headers
    if (has_minus_header and has_plus_header) or has_hunk_header or has_git_header:
        return True

    return False


def parse_fix_response(
    raw_text: str,
    expected_test_name: str,
) -> tuple[str, dict[str, Any]]:
    """
    Parse Bob's response into (unified_diff, json_metadata).

    Rules:
    - Locate JSON block.
    - Parse JSON block.
    - Extract text before JSON block as the diff.
    - Remove optional markdown code fences from diff and JSON.
    - Reject response containing only JSON.
    - Reject malformed JSON.
    - Reject missing/invalid diff.
    - Validate JSON schema.
    """
    if not raw_text or not raw_text.strip():
        raise ValueError("Bob response is empty")

    raw = raw_text.strip()

    # Step 1: Locate JSON candidate
    # First look for ```json ... ``` blocks
    json_candidate: str | None = None
    diff_candidate: str | None = None

    if "```json" in raw:
        match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL | re.IGNORECASE)
        if match:
            json_candidate = match.group(1).strip()
            diff_candidate = raw[:match.start()] + raw[match.end():]

    # If no ```json block, search for JSON object by curly braces
    if json_candidate is None:
        # Search backwards from the last '}'
        last_brace = raw.rfind("}")
        if last_brace != -1:
            # Look backwards for a matching '{' that produces valid JSON with "subagent"
            # Try potential '{' occurrences
            open_braces = [i for i, c in enumerate(raw[:last_brace]) if c == "{"]
            for start_idx in reversed(open_braces):
                candidate_str = raw[start_idx : last_brace + 1].strip()
                try:
                    parsed = json.loads(candidate_str)
                    if isinstance(parsed, dict) and "subagent" in parsed:
                        json_candidate = candidate_str
                        diff_candidate = raw[:start_idx] + raw[last_brace + 1 :]
                        break
                except json.JSONDecodeError:
                    continue

    if json_candidate is None:
        raise ValueError("Malformed response: no valid JSON block found in Bob output")

    # Step 2: Parse and validate JSON
    try:
        json_data = json.loads(json_candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed JSON metadata: {exc}") from exc

    validate_fix_schema(json_data, expected_test_name)

    # Step 3: Extract and clean diff
    if diff_candidate is None:
        diff_candidate = ""

    # If diff is enclosed in a markdown block, extract its content
    diff_block_match = re.search(r"```(?:diff)?\s*\n(.*?)\n```", diff_candidate, re.DOTALL | re.IGNORECASE)
    if diff_block_match:
        cleaned_diff = diff_block_match.group(1).strip()
    else:
        cleaned_diff = _strip_markdown_fence(diff_candidate, "diff")
        cleaned_diff = _strip_markdown_fence(cleaned_diff)

    if not cleaned_diff.strip():
        raise ValueError(
            "Missing unified diff: response contained only JSON metadata with no diff"
        )

    if not _is_recognizable_diff(cleaned_diff):
        raise ValueError(
            "Invalid diff: response does not contain a recognizable unified diff "
            "(missing '--- / +++', '@@ @@', or 'diff --git' markers)"
        )

    # Ensure diff ends with a newline
    if not cleaned_diff.endswith("\n"):
        cleaned_diff += "\n"

    return cleaned_diff, json_data


# ---------------------------------------------------------------------------
# Fallback mechanism
# ---------------------------------------------------------------------------

def fallback_fix(
    test_name: str,
    reason: str = "Bob was unavailable or failed to propose a valid fix",
) -> dict[str, Any]:
    """
    Conservative fallback when Bob fails, times out, or produces invalid output.

    Deliberately refuses to invent or fabricate a diff.
    Indicates that human review is required.
    """
    print("[FIX_AGENT] using fallback", file=sys.stderr)

    return {
        "subagent": "fix_agent",
        "test_name": test_name,
        "fix_summary": f"NO FIX GENERATED — human review required. {reason}".strip(),
        "justification": f"Human review required. No automated fix was generated because: {reason}".strip(),
        "confidence": "low",
    }


# ---------------------------------------------------------------------------
# Bob Shell infrastructure
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
        value = value.strip().strip('"').strip("'")
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
        if node:
            candidates = [
                bob_path.parent / "node_modules" / "bobshell" / "dist" / "bob.js",
                bob_path.parent / "node_modules" / "@bob" / "bob-shell" / "bob.js",
                bob_path.parent / "node_modules" / "bob-shell" / "bob.js",
            ]
            for candidate in candidates:
                if candidate.exists():
                    return [node, str(candidate)]

        return [str(bob_path)]

    return [str(bob_path)]


def _save_raw_response(raw: str) -> None:
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        (_DEBUG_DIR / "fix_raw_response.txt").write_text(raw, encoding="utf-8")
    except Exception:
        pass


def _invoke_bob(
    prompt: str,
    test_name: str,
) -> tuple[tuple[str, dict[str, Any]] | None, str | None]:
    """
    Attempt to invoke Bob Shell with prompt.
    Returns ((diff, json_data), None) on success,
    or (None, failure_reason) on failure.
    """
    argv_prefix = _resolve_bob()
    if argv_prefix is None:
        return None, "Bob Shell not found on PATH"

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
            timeout=120,
            env=env,
            cwd=str(_HERE),
        )
    except subprocess.TimeoutExpired:
        return None, "Bob process timed out after 120 seconds"
    except Exception as exc:
        return None, f"Bob subprocess error: {exc}"
    finally:
        # Guarantee demo-repo is never modified
        try:
            demo_repo = _HERE / "demo-repo"
            if demo_repo.exists() and (demo_repo / ".git").exists():
                subprocess.run(
                    ["git", "restore", "."],
                    cwd=str(demo_repo),
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "clean", "-fd"],
                    cwd=str(demo_repo),
                    capture_output=True,
                )
        except Exception:
            pass

    if proc.returncode != 0:
        err_snippet = (proc.stderr or "").strip()[:200]
        return None, f"Bob exited with code {proc.returncode}: {err_snippet}"

    stdout = (proc.stdout or "").strip()
    if not stdout:
        return None, "Bob stdout was empty"

    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return None, f"Invalid JSON envelope from Bob: {exc}"

    if not isinstance(envelope, dict) or "last_message" not in envelope:
        return None, "Missing last_message in Bob JSON envelope"

    last_message = envelope["last_message"]
    raw_msg = last_message if isinstance(last_message, str) else json.dumps(last_message)
    _save_raw_response(raw_msg)

    try:
        diff_text, fix_json = parse_fix_response(raw_msg, test_name)
    except (ValueError, json.JSONDecodeError) as exc:
        return None, f"Failed to parse Bob output: {exc}"

    return (diff_text, fix_json), None


# ---------------------------------------------------------------------------
# Evidence persistence
# ---------------------------------------------------------------------------

def _persist_diff(
    diff_text: str,
    test_name: str,
    evidence_dir: Path | None = None,
) -> Path:
    edir = evidence_dir or _EVIDENCE_DIR
    edir.mkdir(parents=True, exist_ok=True)
    out_path = edir / f"fix_{_safe_alnum(test_name)}.diff"
    out_path.write_text(diff_text, encoding="utf-8")
    return out_path


def _remove_stale_diff(
    test_name: str,
    evidence_dir: Path | None = None,
) -> None:
    edir = evidence_dir or _EVIDENCE_DIR
    diff_path = edir / f"fix_{_safe_alnum(test_name)}.diff"
    if diff_path.exists():
        try:
            diff_path.unlink()
        except OSError:
            pass


def _persist_evidence(
    data: dict[str, Any],
    test_name: str,
    evidence_dir: Path | None = None,
) -> Path:
    edir = evidence_dir or _EVIDENCE_DIR
    edir.mkdir(parents=True, exist_ok=True)
    out_path = edir / f"fix_{_safe_alnum(test_name)}.json"
    out_path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_fix_agent(
    test_name: str,
    evidence_dir: Path | None = None,
    prompt_path: Path | None = None,
) -> dict[str, Any]:
    """
    Run the Fix Agent for test_name.

    1. Load Coordinator evidence.
    2. Build fix prompt.
    3. Invoke Bob Shell.
    4. Parse and validate diff + JSON metadata.
    5. On Bob failure/validation error: activate fallback (never invent diff).
    6. Persist diff (on success only) and structured evidence JSON.
    7. Return structured evidence JSON.
    """
    if "::" not in test_name:
        raise ValueError(
            f"test_name must be a full pytest node ID (file::function), got: {test_name!r}"
        )

    edir = evidence_dir or _EVIDENCE_DIR

    # Load coordinator evidence
    coordinator_data = load_coordinator_evidence(test_name, evidence_dir=edir)
    print(f"[FIX_AGENT] loaded coordinator evidence for {test_name}", file=sys.stderr)

    # Build prompt
    prompt = build_fix_prompt(test_name, coordinator_data, prompt_path=prompt_path)

    # Invoke Bob
    result, failure_reason = _invoke_bob(prompt, test_name)

    if result is not None:
        diff_text, fix_json = result
        print("[FIX_AGENT] using real Bob fix proposal", file=sys.stderr)
        diff_path = _persist_diff(diff_text, test_name, evidence_dir=edir)
        print(f"[FIX_AGENT] proposed diff persisted to {diff_path}", file=sys.stderr)
    else:
        reason_msg = failure_reason or "Bob was unavailable or failed to propose a valid fix"
        print(f"[FIX_AGENT] BOB FAILED: {reason_msg}", file=sys.stderr)
        fix_json = fallback_fix(test_name, reason=reason_msg)
        _remove_stale_diff(test_name, evidence_dir=edir)

    # Persist structured evidence JSON
    json_path = _persist_evidence(fix_json, test_name, evidence_dir=edir)
    print(f"[FIX_AGENT] evidence persisted to {json_path}", file=sys.stderr)

    return fix_json


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main() -> int:
    if len(sys.argv) != 2:
        print(
            "Usage: python analyzer/fix_agent.py <pytest_node_id>",
            file=sys.stderr,
        )
        print(
            "Example: python analyzer/fix_agent.py "
            '"tests/test_a_order.py::test_cache_starts_clean"',
            file=sys.stderr,
        )
        return 2

    test_name = sys.argv[1]

    try:
        result = run_fix_agent(test_name)
    except FileNotFoundError as exc:
        print(f"[FIX_AGENT] ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[FIX_AGENT] ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
