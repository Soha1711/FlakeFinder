"""
analyzer/verify_fix.py — FlakeFinder Step 13 Verification Stage.

Usage:
    python analyzer/verify_fix.py <pytest_node_id> <diff_path> [neighbor_tests...]

Examples:
    python analyzer/verify_fix.py \\
        "tests/test_a_order.py::test_cache_starts_clean" \\
        "state/evidence/fix_tests_test_a_order.py__test_cache_starts_clean.diff"

    python analyzer/verify_fix.py \\
        "tests/test_d_regression.py::test_calculate_total" \\
        "state/evidence/fix_tests_test_d_regression.py__test_calculate_total.diff"

Architecture:
    1. Create a clean, independent scratch copy of demo-repo in state/scratch/verify_run.
    2. Run target test n times BEFORE applying the proposed fix (default n=10).
    3. Apply the proposed diff to the scratch copy (with git apply --check first).
    4. Run target test n times AFTER applying the proposed fix.
    5. Run optional neighbor/regression tests (3 times each).
    6. Calculate fix_confirmed (after_fix failures == 0 AND before_fix failures > 0).
    7. Calculate regression_safe (all neighbor tests have 0 failures).
    8. Persist structured verification evidence to state/evidence/verification_<safe_name>.json.
    9. NEVER call Bob, import Bob, or read Bob credentials.
    10. NEVER modify the real demo-repo working tree.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path configurations
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent.parent

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_EVIDENCE_DIR = _HERE / "state" / "evidence"
_DEFAULT_SCRATCH_DEST = _HERE / "state" / "scratch" / "verify_run"
_DEFAULT_DEMO_SOURCE = _HERE / "demo-repo"

DEFAULT_NEIGHBORS: dict[str, list[str]] = {
    "tests/test_a_order.py::test_cache_starts_clean": [
        "tests/test_shared_cache_mutator.py::test_contaminate_shared_cache",
        "tests/test_shared_cache_stable.py::test_cache_initial_value",
    ],
    "tests/test_d_regression.py::test_calculate_total": [
        "tests/test_regression_stable.py::test_calculate_total_empty_list",
        "tests/test_regression_stable.py::test_calculate_total_multiple_items",
    ],
}


# ---------------------------------------------------------------------------
# Safe name helper
# ---------------------------------------------------------------------------

def _safe_alnum(value: str) -> str:
    """Standard alnum safe-name convention matching coordinator/bisect/fix."""
    return "".join(
        c if c.isalnum() or c in "._-" else "_"
        for c in value
    )


def _remove_readonly(func: Any, path: str, exc_info: Any) -> None:
    """Error handler for shutil.rmtree on Windows read-only files."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def _get_pytest_command() -> list[str]:
    """Locate the Python executable with pytest installed."""
    # Check workspace venv first
    venv_py = _HERE / "venv" / "Scripts" / "python.exe"
    if venv_py.exists():
        return [str(venv_py), "-m", "pytest"]
    # Fallback to current sys.executable
    return [sys.executable, "-m", "pytest"]


# ---------------------------------------------------------------------------
# Step 13A: Scratch Copy
# ---------------------------------------------------------------------------

def create_scratch_copy(
    source: str | Path = "demo-repo",
    dest: str | Path = "state/scratch/verify_run",
) -> Path:
    """
    Create a clean, isolated scratch copy of demo-repo.

    - Removes an existing scratch directory before recreating it.
    - Copies the complete demo-repo into the scratch directory.
    - Does NOT copy/use the project's virtual environment.
    - Initializes or isolates git so operations in scratch are self-contained.
    - Returns the absolute scratch path.
    """
    src_path = Path(source).resolve()
    dst_path = Path(dest).resolve()

    if not src_path.exists():
        raise FileNotFoundError(f"Source repository does not exist: {src_path}")

    # Remove existing scratch directory if present
    if dst_path.exists():
        shutil.rmtree(dst_path, onerror=_remove_readonly)

    dst_path.parent.mkdir(parents=True, exist_ok=True)

    # Ignore .git, caches, and virtualenvs during copy
    ignore_patterns = shutil.ignore_patterns(
        ".git",
        ".pytest_cache",
        "__pycache__",
        "venv",
        ".venv",
        "*.pyc",
    )
    shutil.copytree(src_path, dst_path, ignore=ignore_patterns)

    # Initialize independent git repository in scratch
    # If the source demo-repo uses a submodule gitdir, copy .git/modules/demo-repo
    # to retain full commit history for git revert
    submodule_git = _HERE / ".git" / "modules" / "demo-repo"
    git_setup_success = False

    if submodule_git.exists() and submodule_git.is_dir():
        try:
            shutil.copytree(submodule_git, dst_path / ".git")
            config_file = dst_path / ".git" / "config"
            if config_file.exists():
                lines = config_file.read_text(encoding="utf-8").splitlines()
                filtered = [l for l in lines if "worktree =" not in l]
                config_file.write_text("\n".join(filtered) + "\n", encoding="utf-8")
            git_setup_success = True
        except Exception:
            git_setup_success = False

    if not git_setup_success:
        # Fallback to clean git init
        subprocess.run(["git", "init"], cwd=str(dst_path), capture_output=True)
        subprocess.run(["git", "config", "user.name", "FlakeFinder Verifier"], cwd=str(dst_path), capture_output=True)
        subprocess.run(["git", "config", "user.email", "flakefinder@example.com"], cwd=str(dst_path), capture_output=True)
        subprocess.run(["git", "add", "."], cwd=str(dst_path), capture_output=True)
        subprocess.run(["git", "commit", "-m", "baseline"], cwd=str(dst_path), capture_output=True)

    return dst_path


# ---------------------------------------------------------------------------
# Step 13C: Run Test N Times
# ---------------------------------------------------------------------------

def run_test_n_times(
    repo_path: str | Path,
    test_target: str,
    n: int = 10,
    pytest_cmd: list[str] | None = None,
) -> dict[str, int]:
    """
    Run pytest against the specified test_target n times.

    - Uses cwd=repo_path.
    - Sets PYTHONPATH to repo_path.
    - Captures stdout/stderr.
    - Return code 0 is PASS; nonzero is FAILURE.
    - Returns {"runs": n, "passes": <int>, "failures": <int>}.
    """
    r_path = Path(repo_path).resolve()
    base_cmd = pytest_cmd or _get_pytest_command()

    passes = 0
    failures = 0

    env = os.environ.copy()
    env["PYTHONPATH"] = str(r_path) + os.pathsep + env.get("PYTHONPATH", "")

    for _ in range(n):
        cmd = base_cmd + [test_target, "-q"]
        proc = subprocess.run(
            cmd,
            cwd=str(r_path),
            env=env,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            passes += 1
        else:
            failures += 1

    return {
        "runs": n,
        "passes": passes,
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Step 13D: Diff Application
# ---------------------------------------------------------------------------

def apply_diff(repo_path: str | Path, diff_path: str | Path) -> bool:
    """
    Apply a unified diff inside the scratch repository.

    - Verifies diff with 'git apply --check' first.
    - If check fails, raises RuntimeError without modifying code.
    - If check succeeds, applies diff with 'git apply'.
    - NEVER applies diff to real demo-repo.
    """
    r_path = Path(repo_path).resolve()
    d_path = Path(diff_path).resolve()

    if not d_path.exists():
        raise FileNotFoundError(f"Diff file not found: {d_path}")

    diff_content = d_path.read_text(encoding="utf-8")
    if not diff_content.strip():
        raise ValueError("Diff file is empty")

    # Candidate flag configurations to accommodate varying diff prefix conventions
    # (e.g., standard, -p1, -p2 with recount/whitespace tolerance)
    candidate_flags = [
        ["--check"],
        ["-p1", "--check"],
        ["-p2", "--recount", "--ignore-whitespace", "--check"],
        ["-p1", "--recount", "--ignore-whitespace", "--check"],
        ["--recount", "--ignore-whitespace", "--check"],
    ]

    matched_flags: list[str] | None = None
    last_err: str = ""

    for flags in candidate_flags:
        check_cmd = ["git", "apply"] + flags + [str(d_path)]
        check_proc = subprocess.run(
            check_cmd,
            cwd=str(r_path),
            capture_output=True,
            text=True,
        )
        if check_proc.returncode == 0:
            matched_flags = flags
            break
        else:
            last_err = (check_proc.stderr or check_proc.stdout).strip()

    if matched_flags is None:
        raise RuntimeError(f"git apply --check failed: {last_err}")

    # Remove --check to perform actual application
    apply_flags = [f for f in matched_flags if f != "--check"]
    apply_cmd = ["git", "apply"] + apply_flags + [str(d_path)]
    apply_proc = subprocess.run(
        apply_cmd,
        cwd=str(r_path),
        capture_output=True,
        text=True,
    )
    if apply_proc.returncode != 0:
        err_msg = (apply_proc.stderr or apply_proc.stdout).strip()
        raise RuntimeError(f"git apply failed ({apply_proc.returncode}): {err_msg}")

    return True


# ---------------------------------------------------------------------------
# Step 13E: Optional Commit Revert Helper
# ---------------------------------------------------------------------------

def apply_commit_revert(
    repo_path: str | Path,
    bad_commit: str = "04805c176d22dc3b07ed581cfa99fc1e7b5dd37e",
) -> bool:
    """
    Revert the specified commit inside the scratch repository without committing.

    Uses 'git revert --no-commit <bad_commit>'.
    """
    r_path = Path(repo_path).resolve()
    cmd = ["git", "revert", "--no-commit", bad_commit]
    proc = subprocess.run(
        cmd,
        cwd=str(r_path),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        err_msg = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"git revert failed ({proc.returncode}): {err_msg}")

    return True


# ---------------------------------------------------------------------------
# Step 13F & 13G: Verify Function
# ---------------------------------------------------------------------------

def verify_fix(
    test_name: str,
    diff_path: str | Path,
    neighbor_tests: list[str] | None = None,
    n: int = 10,
    scratch_source: str | Path = "demo-repo",
    scratch_dest: str | Path = "state/scratch/verify_run",
) -> dict[str, Any]:
    """
    Independently verify a proposed fix.

    1. Create a fresh scratch copy of demo-repo.
    2. Run target test n times BEFORE applying fix.
    3. Apply proposed diff to scratch copy.
    4. Run target test n times AFTER applying fix.
    5. If neighbor_tests supplied, run each 3 times after fix.
    6. Calculate:
       fix_confirmed = after_fix["failures"] == 0 AND before_fix["failures"] > 0
       regression_safe = all neighbor tests have zero failures (or null if none)
    """
    scratch_path = create_scratch_copy(source=scratch_source, dest=scratch_dest)

    # 1. Run target test before fix
    before_fix = run_test_n_times(scratch_path, test_name, n=n)

    # 2. Apply proposed diff
    apply_diff(scratch_path, diff_path)

    # 3. Run target test after fix
    after_fix = run_test_n_times(scratch_path, test_name, n=n)

    # 4. Optional neighbor regression checks (3 runs each)
    regression_check: dict[str, dict[str, int]] = {}
    regression_safe: bool | None = None

    if neighbor_tests:
        all_passed = True
        for neighbor in neighbor_tests:
            n_res = run_test_n_times(scratch_path, neighbor, n=3)
            regression_check[neighbor] = n_res
            if n_res["failures"] > 0:
                all_passed = False
        regression_safe = all_passed

    # 5. Fix confirmation logic:
    # Requires baseline failures > 0 AND after-fix failures == 0
    fix_confirmed = (after_fix["failures"] == 0 and before_fix["failures"] > 0)

    return {
        "subagent": "verification",
        "test_name": test_name,
        "before_fix": before_fix,
        "after_fix": after_fix,
        "fix_confirmed": fix_confirmed,
        "regression_check": regression_check,
        "regression_safe": regression_safe,
    }


# ---------------------------------------------------------------------------
# Step 13H & 13I: Error Handling & Persistence
# ---------------------------------------------------------------------------

def run_verification(
    test_name: str,
    diff_path: str | Path,
    neighbor_tests: list[str] | None = None,
    n: int = 10,
    scratch_source: str | Path = "demo-repo",
    scratch_dest: str | Path = "state/scratch/verify_run",
    evidence_dir: Path | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """
    Run verification with comprehensive error handling and evidence persistence.

    Handles:
    - missing diff
    - malformed diff
    - git apply --check failure
    - git apply failure
    - pytest execution failure
    - scratch copy creation failure
    """
    edir = evidence_dir or _EVIDENCE_DIR

    try:
        result = verify_fix(
            test_name=test_name,
            diff_path=diff_path,
            neighbor_tests=neighbor_tests,
            n=n,
            scratch_source=scratch_source,
            scratch_dest=scratch_dest,
        )
    except Exception as exc:
        result = {
            "subagent": "verification",
            "test_name": test_name,
            "fix_confirmed": False,
            "error": str(exc),
            "note": "Fix could not be verified.",
        }

    if persist:
        edir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_alnum(test_name)
        out_file = edir / f"verification_{safe_name}.json"
        out_file.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"[VERIFY] Saved verification evidence to {out_file}", file=sys.stderr)

    return result


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main() -> int:
    if len(sys.argv) < 3:
        print(
            "Usage: python analyzer/verify_fix.py <test_name> <diff_path> [neighbor_tests...]",
            file=sys.stderr,
        )
        print(
            'Example: python analyzer/verify_fix.py "tests/test_a_order.py::test_cache_starts_clean" "state/evidence/fix_tests_test_a_order.py__test_cache_starts_clean.diff"',
            file=sys.stderr,
        )
        return 2

    test_name = sys.argv[1]
    diff_path = sys.argv[2]

    if len(sys.argv) > 3:
        neighbor_tests = sys.argv[3:]
    else:
        neighbor_tests = DEFAULT_NEIGHBORS.get(test_name)

    result = run_verification(
        test_name=test_name,
        diff_path=diff_path,
        neighbor_tests=neighbor_tests,
        n=10,
    )

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
