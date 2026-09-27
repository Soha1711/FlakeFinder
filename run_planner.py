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
import os
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
_DOTENV_FILE = _HERE / ".env"

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
# Bob executable resolution + environment helpers
# ---------------------------------------------------------------------------


def _resolve_bob() -> list[str] | None:
    """
    Return the argv prefix needed to run Bob Shell on this platform.

    On Windows, `shutil.which("bob")` may resolve to `bob.CMD`, which is a
    batch-file wrapper around `node <npm_dir>/node_modules/bobshell/dist/bob.js`.
    Batch files cannot be executed directly by Python's subprocess on Windows
    without shell=True (which causes stdin/stdout pipe issues), so we detect
    the `.CMD` case and build a [node, bob.js] prefix instead.

    Returns a list such as:
        Unix/EXE:   ["/usr/local/bin/bob"]
        Windows CMD: ["C:/Program Files/nodejs/node.exe",
                      "C:/Users/.../node_modules/bobshell/dist/bob.js"]
    Returns None if Bob cannot be found.
    """
    bob_path = shutil.which("bob")
    if bob_path is None:
        return None

    # On Windows, prefer the .cmd wrapper's companion bob.js invoked via node,
    # avoiding shell=True and the batch-file stdin-pipe deadlock.
    if sys.platform == "win32" and bob_path.lower().endswith((".cmd", ".bat")):
        node_path = shutil.which("node")
        if node_path is None:
            return None  # Node not on PATH — cannot invoke bob.js
        bob_dir = os.path.dirname(bob_path)
        bob_js = os.path.join(bob_dir, "node_modules", "bobshell", "dist", "bob.js")
        if not os.path.exists(bob_js):
            return None  # bob.js not where expected
        return [node_path, bob_js]

    # Unix or native Windows EXE — invoke directly.
    return [bob_path]


def _load_bob_env() -> dict:
    """
    Return an environment dict for the Bob subprocess.

    Bob Shell requires BOB_API_KEY.  When it is already present in the
    current process environment it is inherited automatically.  When it is
    absent we attempt to load it from a .env file in the project root
    (FlakeFinder/.env) using a minimal line-parser so we avoid adding a
    dotenv dependency.

    The returned dict is a copy of os.environ with BOB_API_KEY injected if
    found, or os.environ unchanged if it is already set or not found.
    """
    env = dict(os.environ)
    if env.get("BOB_API_KEY"):
        return env  # already present — nothing to do

    if not _DOTENV_FILE.exists():
        return env

    try:
        for raw_line in _DOTENV_FILE.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key in ("BOB_API_KEY", "BOBSHELL_API_KEY") and value:
                env["BOB_API_KEY"] = value
                break
    except OSError:
        pass

    return env


# ---------------------------------------------------------------------------
# Bob invocation path
# ---------------------------------------------------------------------------

# Allowed hypothesis labels (matches planner_prompt.md vocabulary exactly)
_ALLOWED_HYPOTHESES = frozenset({
    _HYPOTHESIS_ORDER,
    _HYPOTHESIS_RACE,
    _HYPOTHESIS_RANDOM,
    _HYPOTHESIS_REGRESSION,
})


def _normalize_hypotheses(raw: list) -> list[str]:
    """Return only hypothesis values that are in the allowed enum; preserve order."""
    seen: set[str] = set()
    result: list[str] = []
    for h in raw:
        if isinstance(h, str) and h in _ALLOWED_HYPOTHESES and h not in seen:
            result.append(h)
            seen.add(h)
    return result


def _build_bob_prompt(test_file: str, test_function: str) -> str:
    """Assemble the full prompt text to pass to Bob Shell.

    Structure:
        <planner_prompt.md — instructions verbatim>

        --- BEGIN TEST CONTEXT ---
        Test node ID: <full pytest node ID>
        Test file:    <test_file>

        [TEST FILE CONTENTS]
        <source of test_file>

        [GIT LOG]
        <last 20 commits touching test_file>
        --- END TEST CONTEXT ---

        Return ONLY the Planner JSON object. Do not explain your answer.
        Do not use markdown fences.
    """
    system_prompt = _PLANNER_PROMPT.read_text(encoding="utf-8").rstrip()

    # Append test file content if it exists inside demo-repo.
    test_file_path = _DEMO_REPO / test_file
    test_file_content = ""
    if test_file_path.exists():
        test_file_content = test_file_path.read_text(encoding="utf-8").rstrip()

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

    node_id = f"{test_file}::{test_function}"

    return (
        f"{system_prompt}\n\n"
        f"--- BEGIN TEST CONTEXT ---\n"
        f"Test node ID: {node_id}\n"
        f"Test file:    {test_file}\n\n"
        f"[TEST FILE CONTENTS]\n{test_file_content}\n\n"
        f"[GIT LOG]\n{git_log}\n"
        f"--- END TEST CONTEXT ---\n\n"
        f"Return ONLY the Planner JSON object. Do not explain your answer. "
        f"Do not use markdown fences."
    )


def _invoke_bob(node_id: str, test_file: str, test_function: str) -> dict | None:
    """
    Try to call the Bob Shell CLI via `bob run --format json "<prompt>"`.
    Parses the Bob Shell JSON envelope and extracts `last_message` as the Planner JSON.
    Returns a validated dict on success, or None on any failure.
    Writes 'BOB FAILED: <reason>' to stderr on failure.
    """
    argv_prefix = _resolve_bob()
    if argv_prefix is None:
        return None  # Bob not on PATH — silent, fallback activates

    prompt_text = _build_bob_prompt(test_file, test_function)
    bob_env = _load_bob_env()

    try:
        proc = subprocess.run(
            argv_prefix + ["run", "--format", "json", prompt_text],
            stdin=subprocess.DEVNULL,  # prevent bob from blocking on stdin (Windows)
            capture_output=True,
            text=True,
            timeout=120,
            env=bob_env,
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

    # Parse the Bob Shell JSON envelope.
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"BOB FAILED: response is not valid JSON — {exc}", file=sys.stderr)
        return None

    # Extract last_message from the envelope.
    if not isinstance(envelope, dict) or "last_message" not in envelope:
        print(
            f"BOB FAILED: JSON envelope missing 'last_message' key — keys: {list(envelope.keys()) if isinstance(envelope, dict) else type(envelope).__name__}",
            file=sys.stderr,
        )
        return None

    last_message = envelope["last_message"]

    # last_message may be a JSON string (parse it) or already a dict.
    if isinstance(last_message, str):
        try:
            data = json.loads(last_message)
        except json.JSONDecodeError as exc:
            print(f"BOB FAILED: last_message is not valid JSON — {exc}", file=sys.stderr)
            return None
    elif isinstance(last_message, dict):
        data = last_message
    else:
        print(
            f"BOB FAILED: last_message has unexpected type {type(last_message).__name__}",
            file=sys.stderr,
        )
        return None

    # Ensure test_name carries the full node ID the caller passed in, not a truncated form.
    data["test_name"] = node_id

    # Normalize hypotheses: keep only allowed enum values, drop unknown labels.
    if isinstance(data.get("hypotheses"), list):
        data["hypotheses"] = _normalize_hypotheses(data["hypotheses"])

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
        print("[PLANNER] using fallback", file=sys.stderr)
        result = _fallback(node_id, test_file)
    else:
        print("[PLANNER] using real Bob", file=sys.stderr)

    _validate(result)
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _OUTPUT_FILE.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


run_planner = run


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
