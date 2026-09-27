"""
run_planner.py — FlakeFinder Step 6 Planner entry point.

Usage:
    python run_planner.py <pytest_node_id>

Example:
    python run_planner.py tests/test_a_order.py::test_cache_starts_clean

Architecture:
    1. If `bob` is on PATH, invoke Bob Shell with planner_prompt.md + test context.
    2. Parse and validate the Planner JSON schema from Bob's stdout.
    3. If Bob is unavailable, fails, or returns invalid JSON:
       - Print "BOB FAILED: <reason>" to stderr.
       - Activate the deterministic Python fallback.
    4. Validate the final JSON, print to stdout, write to state/evidence/planner_out.json.

Planner JSON schema:
    {
        "test_name":      "<full pytest node ID>",
        "test_file":      "<relative test file path>",
        "hypotheses":     ["<hypothesis_label>", ...],
        "search_targets": ["<file>", ...]
    }

Hypotheses are investigation guidance only. They do not control which subagents run.
All five subagents (Isolation, Order-Shuffle, Git-Bisect, Static-Pattern, History)
run for every investigated test regardless of what hypotheses the Planner returns.
"""

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
_PLANNER_PROMPT = _HERE / "bob_prompts" / "planner_prompt.md"
_DEMO_REPO = _HERE / "demo-repo"
_OUTPUT_DIR = _HERE / "state" / "evidence"
_OUTPUT_FILE = _OUTPUT_DIR / "planner_out.json"

# Hypothesis labels (matches planner_prompt.md vocabulary exactly)
_HYPOTHESIS_ORDER = "order_dependency"
_HYPOTHESIS_RACE = "race_condition"
_HYPOTHESIS_RANDOM = "non_determinism"
_HYPOTHESIS_REGRESSION = "regression"

_REGRESSION_KEYWORDS = {"regression", "revert", "off-by-one", "offbyone", "broke", "bug"}


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def _validate(data: dict) -> None:
    """Raise ValueError if data does not satisfy the Planner JSON schema."""
    required = {"test_name", "test_file", "hypotheses", "search_targets"}
    missing = required - data.keys()
    if missing:
        raise ValueError(f"Planner JSON missing keys: {sorted(missing)}")
    if not isinstance(data["hypotheses"], list) or len(data["hypotheses"]) == 0:
        raise ValueError("'hypotheses' must be a non-empty list")
    if not isinstance(data["search_targets"], list) or len(data["search_targets"]) == 0:
        raise ValueError("'search_targets' must be a non-empty list")
    if "::" not in data["test_name"]:
        raise ValueError(
            f"'test_name' must be a full pytest node ID (file::function), got: {data['test_name']!r}"
        )


# ---------------------------------------------------------------------------
# Bob invocation path
# ---------------------------------------------------------------------------

def _build_bob_prompt(test_file: str, test_function: str) -> str:
    """Assemble the full prompt text to send to Bob on stdin."""
    system_prompt = _PLANNER_PROMPT.read_text(encoding="utf-8")

    # Append test file content if it exists inside demo-repo.
    test_file_path = _DEMO_REPO / test_file
    test_file_content = ""
    if test_file_path.exists():
        test_file_content = test_file_path.read_text(encoding="utf-8")

    # Append the last 20 lines of git log for the test file from demo-repo.
    git_log = ""
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-20", "--", test_file],
            cwd=str(_DEMO_REPO),
            capture_output=True,
            text=True,
            timeout=15,
        )
        git_log = result.stdout.strip()
    except Exception:
        git_log = "(git log unavailable)"

    return (
        f"{system_prompt}\n\n"
        f"--- TEST FILE: {test_file} ---\n{test_file_content}\n\n"
        f"--- GIT LOG (last 20 commits touching {test_file}) ---\n{git_log}\n\n"
        f"Investigate: {test_file}::{test_function}"
    )


def _invoke_bob(node_id: str, test_file: str, test_function: str) -> dict | None:
    """
    Try to call the `bob` CLI. Return a validated dict on success, or None on any failure.
    Writes 'BOB FAILED: <reason>' to stderr on failure.
    """
    bob_path = shutil.which("bob")
    if bob_path is None:
        return None  # Bob not on PATH — silent, fallback activates

    prompt_text = _build_bob_prompt(test_file, test_function)

    try:
        proc = subprocess.run(
            [bob_path],
            input=prompt_text,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        print(f"BOB FAILED: subprocess error — {exc}", file=sys.stderr)
        return None

    if proc.returncode != 0:
        print(
            f"BOB FAILED: non-zero exit ({proc.returncode}) — {proc.stderr.strip()[:200]}",
            file=sys.stderr,
        )
        return None

    raw = proc.stdout.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"BOB FAILED: response is not valid JSON — {exc}", file=sys.stderr)
        return None

    # Ensure test_name carries the full node ID the caller passed in, not a truncated form.
    data["test_name"] = node_id

    try:
        _validate(data)
    except ValueError as exc:
        print(f"BOB FAILED: schema validation error — {exc}", file=sys.stderr)
        return None

    return data


# ---------------------------------------------------------------------------
# Deterministic Python fallback
# ---------------------------------------------------------------------------

def _fallback(node_id: str, test_file: str) -> dict:
    """
    Build the Planner JSON using AST analysis and git log inspection.
    Always returns a valid dict that satisfies the Planner schema.
    """
    hypotheses: list[str] = []
    search_targets: list[str] = [test_file]

    test_file_path = _DEMO_REPO / test_file
    if test_file_path.exists():
        source = test_file_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            tree = None

        if tree is not None:
            for node in ast.walk(tree):
                # Detect random module usage → non_determinism
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "random":
                            if _HYPOTHESIS_RANDOM not in hypotheses:
                                hypotheses.append(_HYPOTHESIS_RANDOM)
                elif isinstance(node, ast.ImportFrom):
                    if node.module == "random":
                        if _HYPOTHESIS_RANDOM not in hypotheses:
                            hypotheses.append(_HYPOTHESIS_RANDOM)
                    # Detect asyncio usage → race_condition
                    if node.module and "asyncio" in node.module:
                        if _HYPOTHESIS_RACE not in hypotheses:
                            hypotheses.append(_HYPOTHESIS_RACE)
                # Detect asyncio.create_task call → race_condition
                elif isinstance(node, ast.Call):
                    if (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr == "create_task"
                    ):
                        if _HYPOTHESIS_RACE not in hypotheses:
                            hypotheses.append(_HYPOTHESIS_RACE)

        # Detect shared state imports → order_dependency
        if "shared_cache" in source or "shared_state" in source:
            if _HYPOTHESIS_ORDER not in hypotheses:
                hypotheses.append(_HYPOTHESIS_ORDER)

        # Collect imported src/*.py files as additional search targets
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    # e.g. "from src.shared_cache import ..." → src/shared_cache.py
                    if node.module.startswith("src."):
                        candidate = node.module.replace(".", "/") + ".py"
                        if candidate not in search_targets:
                            search_targets.append(candidate)

    # Detect regression via git log keywords
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-20", "--", test_file],
            cwd=str(_DEMO_REPO),
            capture_output=True,
            text=True,
            timeout=15,
        )
        log_lower = result.stdout.lower()
        if any(kw in log_lower for kw in _REGRESSION_KEYWORDS):
            if _HYPOTHESIS_REGRESSION not in hypotheses:
                hypotheses.append(_HYPOTHESIS_REGRESSION)
    except Exception:
        pass

    # Always emit at least one hypothesis
    if not hypotheses:
        hypotheses.append(_HYPOTHESIS_ORDER)

    return {
        "test_name": node_id,
        "test_file": test_file,
        "hypotheses": hypotheses,
        "search_targets": search_targets,
    }


# ---------------------------------------------------------------------------
# Public API (importable by tests)
# ---------------------------------------------------------------------------

def run(node_id: str) -> dict:
    """
    Core logic: receive a pytest node ID, return a validated Planner JSON dict.
    Tries Bob first; falls back to deterministic Python on any failure.
    """
    if "::" not in node_id:
        raise ValueError(f"node_id must be in 'file::function' form, got: {node_id!r}")

    test_file, test_function = node_id.split("::", 1)

    # Attempt Bob invocation
    result = _invoke_bob(node_id, test_file, test_function)

    # Fall back to deterministic Python if Bob was unavailable or failed
    if result is None:
        result = _fallback(node_id, test_file)

    _validate(result)
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python run_planner.py <pytest_node_id>", file=sys.stderr)
        print("Example: python run_planner.py tests/test_a_order.py::test_cache_starts_clean", file=sys.stderr)
        sys.exit(1)

    node_id = sys.argv[1]

    try:
        data = run(node_id)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    output = json.dumps(data, indent=2)
    print(output)

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _OUTPUT_FILE.write_text(output + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
