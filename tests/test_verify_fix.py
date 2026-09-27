"""
tests/test_verify_fix.py — Automated tests for Step 13 Verification Stage.

Covers all 18 requirements:
1. scratch-copy creation
2. scratch copy is independent
3. PYTHONPATH is set correctly
4. repeated pytest execution
5. pass/failure counting
6. diff --check success
7. diff --check failure
8. diff application
9. commit-revert helper
10. before/after verification
11. fix_confirmed logic
12. neighbor regression checks
13. malformed/missing diff handling
14. pytest execution failure handling
15. evidence persistence
16. no Bob invocation
17. Test A path
18. Test D path
"""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from analyzer.verify_fix import (
    DEFAULT_NEIGHBORS,
    _safe_alnum,
    apply_commit_revert,
    apply_diff,
    create_scratch_copy,
    run_test_n_times,
    run_verification,
    verify_fix,
)


def _remove_readonly(func, path, exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


def _init_git_repo(repo_path: Path):
    """Helper to initialize a functional git repo in repo_path."""
    subprocess.run(["git", "init"], cwd=str(repo_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_path), capture_output=True, check=True)


# ---------------------------------------------------------------------------
# 1. Scratch Copy Creation
# ---------------------------------------------------------------------------

def test_scratch_copy_creation(tmp_path):
    source_dir = tmp_path / "mock_demo_repo"
    source_dir.mkdir()
    (source_dir / "src").mkdir()
    (source_dir / "src" / "app.py").write_text("print('hello')", encoding="utf-8")

    scratch_dest = tmp_path / "scratch" / "run1"

    created = create_scratch_copy(source=source_dir, dest=scratch_dest)

    assert created.exists()
    assert (created / "src" / "app.py").exists()
    assert (created / ".git").exists()
    assert (created / "src" / "app.py").read_text(encoding="utf-8") == "print('hello')"


# ---------------------------------------------------------------------------
# 2. Scratch Copy is Independent
# ---------------------------------------------------------------------------

def test_scratch_copy_is_independent(tmp_path):
    source_dir = tmp_path / "mock_demo_repo"
    source_dir.mkdir()
    file_path = source_dir / "file.txt"
    file_path.write_text("original", encoding="utf-8")

    scratch_dest = tmp_path / "scratch" / "run2"
    created = create_scratch_copy(source=source_dir, dest=scratch_dest)

    # Modify the scratch copy
    (created / "file.txt").write_text("modified in scratch", encoding="utf-8")

    # Assert source is completely untouched
    assert file_path.read_text(encoding="utf-8") == "original"
    assert (created / "file.txt").read_text(encoding="utf-8") == "modified in scratch"


# ---------------------------------------------------------------------------
# 3. PYTHONPATH is Set Correctly
# ---------------------------------------------------------------------------

def test_pythonpath_is_set_correctly(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    captured_envs = []

    def mock_run(cmd, cwd=None, env=None, capture_output=None, text=None):
        captured_envs.append(env)
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        return mock_proc

    with patch("subprocess.run", side_effect=mock_run):
        run_test_n_times(repo_dir, "tests/test_dummy.py::test_func", n=1)

    assert len(captured_envs) == 1
    passed_env = captured_envs[0]
    assert "PYTHONPATH" in passed_env
    assert str(repo_dir.resolve()) in passed_env["PYTHONPATH"]


# ---------------------------------------------------------------------------
# 4. Repeated Pytest Execution
# ---------------------------------------------------------------------------

def test_repeated_pytest_execution(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    call_count = 0

    def mock_run(cmd, cwd=None, env=None, capture_output=None, text=None):
        nonlocal call_count
        call_count += 1
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        return mock_proc

    with patch("subprocess.run", side_effect=mock_run):
        result = run_test_n_times(repo_dir, "tests/test_dummy.py::test_func", n=7)

    assert call_count == 7
    assert result["runs"] == 7
    assert result["passes"] == 7
    assert result["failures"] == 0


# ---------------------------------------------------------------------------
# 5. Pass/Failure Counting
# ---------------------------------------------------------------------------

def test_pass_failure_counting(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    # Alternate returncode: 0, 1, 0, 1, 0, 1, 0, 1, 0, 1
    returns = [0, 1, 0, 1, 0, 1, 0, 1, 0, 1]
    call_idx = 0

    def mock_run(cmd, cwd=None, env=None, capture_output=None, text=None):
        nonlocal call_idx
        mock_proc = MagicMock()
        mock_proc.returncode = returns[call_idx]
        call_idx += 1
        return mock_proc

    with patch("subprocess.run", side_effect=mock_run):
        result = run_test_n_times(repo_dir, "tests/test_dummy.py::test_func", n=10)

    assert result["runs"] == 10
    assert result["passes"] == 5
    assert result["failures"] == 5


# ---------------------------------------------------------------------------
# 6. Diff --check Success
# ---------------------------------------------------------------------------

def test_diff_check_success(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_git_repo(repo_dir)

    target_file = repo_dir / "code.py"
    target_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True)

    diff_file = tmp_path / "fix.diff"
    diff_file.write_text(
        "--- a/code.py\n+++ b/code.py\n@@ -2,1 +2,1 @@\n-    return a + b\n+    return (a + b)\n",
        encoding="utf-8",
    )

    success = apply_diff(repo_dir, diff_file)
    assert success is True
    assert "return (a + b)" in target_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 7. Diff --check Failure
# ---------------------------------------------------------------------------

def test_diff_check_failure(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_git_repo(repo_dir)

    target_file = repo_dir / "code.py"
    target_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True)

    # Invalid diff modifying lines that do not exist
    invalid_diff = tmp_path / "bad.diff"
    invalid_diff.write_text(
        "--- a/code.py\n+++ b/code.py\n@@ -1,2 +1,2 @@\n-def subtract(x, y):\n+def subtract(a, b):\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="git apply --check failed"):
        apply_diff(repo_dir, invalid_diff)

    # Verify working tree code was not altered
    assert "def add(a, b):" in target_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 8. Diff Application
# ---------------------------------------------------------------------------

def test_diff_application(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_git_repo(repo_dir)

    target = repo_dir / "val.py"
    target.write_text("X = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_dir), check=True)

    diff = tmp_path / "update.diff"
    diff.write_text("--- a/val.py\n+++ b/val.py\n@@ -1,1 +1,1 @@\n-X = 1\n+X = 42\n", encoding="utf-8")

    apply_diff(repo_dir, diff)
    assert target.read_text(encoding="utf-8") == "X = 42\n"


# ---------------------------------------------------------------------------
# 9. Commit-Revert Helper
# ---------------------------------------------------------------------------

def test_commit_revert_helper(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_git_repo(repo_dir)

    target = repo_dir / "tracked.txt"
    target.write_text("line 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "-m", "commit 1"], cwd=str(repo_dir), check=True)

    target.write_text("line 1\nline 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "-m", "commit 2"], cwd=str(repo_dir), check=True)

    rev_proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    commit2_hash = rev_proc.stdout.strip()

    # Revert commit 2
    res = apply_commit_revert(repo_dir, commit2_hash)
    assert res is True
    assert target.read_text(encoding="utf-8") == "line 1\n"


# ---------------------------------------------------------------------------
# 10. Before/After Verification
# ---------------------------------------------------------------------------

def test_before_after_verification(tmp_path):
    diff_file = tmp_path / "fix.diff"
    diff_file.write_text("--- a/f\n+++ b/f\n", encoding="utf-8")

    with patch("analyzer.verify_fix.create_scratch_copy", return_value=tmp_path):
        with patch("analyzer.verify_fix.apply_diff", return_value=True):
            with patch("analyzer.verify_fix.run_test_n_times") as mock_run:
                # Before fix: 6 passes, 4 failures. After fix: 10 passes, 0 failures.
                mock_run.side_effect = [
                    {"runs": 10, "passes": 6, "failures": 4},
                    {"runs": 10, "passes": 10, "failures": 0},
                ]

                res = verify_fix("tests/test_d.py::test_foo", diff_file, n=10)

                assert res["subagent"] == "verification"
                assert res["test_name"] == "tests/test_d.py::test_foo"
                assert res["before_fix"]["failures"] == 4
                assert res["after_fix"]["failures"] == 0
                assert res["fix_confirmed"] is True


# ---------------------------------------------------------------------------
# 11. Fix Confirmed Logic
# ---------------------------------------------------------------------------

def test_fix_confirmed_logic(tmp_path):
    diff_file = tmp_path / "fix.diff"
    diff_file.write_text("--- a/f\n+++ b/f\n", encoding="utf-8")

    cases = [
        # (before_failures, after_failures, expected_confirmed)
        (4, 0, True),    # Real confirmation
        (0, 0, False),   # Baseline had 0 failures -> fix NOT confirmed
        (4, 1, False),   # Still failed after fix
        (0, 1, False),   # Failed after fix
    ]

    for before_fail, after_fail, expected in cases:
        with patch("analyzer.verify_fix.create_scratch_copy", return_value=tmp_path):
            with patch("analyzer.verify_fix.apply_diff", return_value=True):
                with patch("analyzer.verify_fix.run_test_n_times") as mock_run:
                    mock_run.side_effect = [
                        {"runs": 10, "passes": 10 - before_fail, "failures": before_fail},
                        {"runs": 10, "passes": 10 - after_fail, "failures": after_fail},
                    ]
                    res = verify_fix("test::sample", diff_file, n=10)
                    assert res["fix_confirmed"] == expected, (
                        f"Failed for before={before_fail}, after={after_fail}"
                    )


# ---------------------------------------------------------------------------
# 12. Neighbor Regression Checks
# ---------------------------------------------------------------------------

def test_neighbor_regression_checks(tmp_path):
    diff_file = tmp_path / "fix.diff"
    diff_file.write_text("--- a/f\n+++ b/f\n", encoding="utf-8")

    # Case A: Neighbor tests all pass -> regression_safe is True
    with patch("analyzer.verify_fix.create_scratch_copy", return_value=tmp_path):
        with patch("analyzer.verify_fix.apply_diff", return_value=True):
            with patch("analyzer.verify_fix.run_test_n_times") as mock_run:
                mock_run.side_effect = [
                    {"runs": 10, "passes": 5, "failures": 5},  # before
                    {"runs": 10, "passes": 10, "failures": 0}, # after
                    {"runs": 3, "passes": 3, "failures": 0},   # neighbor 1
                    {"runs": 3, "passes": 3, "failures": 0},   # neighbor 2
                ]
                res = verify_fix(
                    "test::sample",
                    diff_file,
                    neighbor_tests=["neighbor_1", "neighbor_2"],
                    n=10,
                )
                assert res["regression_safe"] is True
                assert len(res["regression_check"]) == 2
                assert res["regression_check"]["neighbor_1"]["passes"] == 3

    # Case B: One neighbor fails -> regression_safe is False
    with patch("analyzer.verify_fix.create_scratch_copy", return_value=tmp_path):
        with patch("analyzer.verify_fix.apply_diff", return_value=True):
            with patch("analyzer.verify_fix.run_test_n_times") as mock_run:
                mock_run.side_effect = [
                    {"runs": 10, "passes": 5, "failures": 5},  # before
                    {"runs": 10, "passes": 10, "failures": 0}, # after
                    {"runs": 3, "passes": 3, "failures": 0},   # neighbor 1
                    {"runs": 3, "passes": 2, "failures": 1},   # neighbor 2 failed
                ]
                res = verify_fix(
                    "test::sample",
                    diff_file,
                    neighbor_tests=["neighbor_1", "neighbor_2"],
                    n=10,
                )
                assert res["regression_safe"] is False

    # Case C: No neighbor tests supplied -> regression_safe is None
    with patch("analyzer.verify_fix.create_scratch_copy", return_value=tmp_path):
        with patch("analyzer.verify_fix.apply_diff", return_value=True):
            with patch("analyzer.verify_fix.run_test_n_times") as mock_run:
                mock_run.side_effect = [
                    {"runs": 10, "passes": 5, "failures": 5},
                    {"runs": 10, "passes": 10, "failures": 0},
                ]
                res = verify_fix("test::sample", diff_file, neighbor_tests=None, n=10)
                assert res["regression_safe"] is None
                assert res["regression_check"] == {}


# ---------------------------------------------------------------------------
# 13. Malformed / Missing Diff Handling
# ---------------------------------------------------------------------------

def test_malformed_missing_diff_handling(tmp_path):
    # Missing diff
    missing_diff = tmp_path / "does_not_exist.diff"
    res1 = run_verification("test::sample", missing_diff, persist=False)
    assert res1["fix_confirmed"] is False
    assert "error" in res1
    assert "not found" in res1["error"].lower()

    # Empty diff
    empty_diff = tmp_path / "empty.diff"
    empty_diff.write_text("   \n", encoding="utf-8")
    res2 = run_verification("test::sample", empty_diff, persist=False)
    assert res2["fix_confirmed"] is False
    assert "error" in res2
    assert "empty" in res2["error"].lower()


# ---------------------------------------------------------------------------
# 14. Pytest Execution Failure Handling
# ---------------------------------------------------------------------------

def test_pytest_execution_failure_handling(tmp_path):
    diff_file = tmp_path / "fix.diff"
    diff_file.write_text("--- a/f\n+++ b/f\n", encoding="utf-8")

    with patch("analyzer.verify_fix.create_scratch_copy", return_value=tmp_path):
        with patch("analyzer.verify_fix.run_test_n_times", side_effect=RuntimeError("Pytest execution failed")):
            res = run_verification("test::sample", diff_file, persist=False)

            assert res["fix_confirmed"] is False
            assert "error" in res
            assert "Pytest execution failed" in res["error"]
            assert res["note"] == "Fix could not be verified."


# ---------------------------------------------------------------------------
# 15. Evidence Persistence
# ---------------------------------------------------------------------------

def test_evidence_persistence(tmp_path):
    evidence_dir = tmp_path / "evidence"
    diff_file = tmp_path / "fix.diff"
    diff_file.write_text("--- a/f\n+++ b/f\n", encoding="utf-8")

    with patch("analyzer.verify_fix.create_scratch_copy", return_value=tmp_path):
        with patch("analyzer.verify_fix.apply_diff", return_value=True):
            with patch("analyzer.verify_fix.run_test_n_times") as mock_run:
                mock_run.side_effect = [
                    {"runs": 10, "passes": 6, "failures": 4},
                    {"runs": 10, "passes": 10, "failures": 0},
                ]
                run_verification(
                    "tests/test_d_regression.py::test_calculate_total",
                    diff_file,
                    evidence_dir=evidence_dir,
                    persist=True,
                )

    safe_name = _safe_alnum("tests/test_d_regression.py::test_calculate_total")
    expected_file = evidence_dir / f"verification_{safe_name}.json"
    assert expected_file.exists()

    data = json.loads(expected_file.read_text(encoding="utf-8"))
    assert data["subagent"] == "verification"
    assert data["fix_confirmed"] is True


# ---------------------------------------------------------------------------
# 16. No Bob Invocation
# ---------------------------------------------------------------------------

def test_no_bob_invocation(monkeypatch):
    import analyzer.verify_fix as vf_module
    import inspect

    # Verify no Bob functions or modules are imported in verify_fix
    source_code = inspect.getsource(vf_module)
    assert "call_bob" not in source_code
    assert "run_planner" not in source_code
    assert "fix_agent" not in source_code
    assert "BOB_API_KEY" not in source_code
    assert "BOBSHELL_API_KEY" not in source_code

    # Verify execution runs in an environment completely devoid of Bob keys
    monkeypatch.delenv("BOB_API_KEY", raising=False)
    monkeypatch.delenv("BOBSHELL_API_KEY", raising=False)

    diff_path = Path("state/evidence/fix_tests_test_d_regression.py__test_calculate_total.diff")
    if diff_path.exists():
        # Dry check that verification logic does not query or require Bob
        with patch("analyzer.verify_fix.create_scratch_copy", return_value=Path(".")):
            with patch("analyzer.verify_fix.apply_diff", return_value=True):
                with patch("analyzer.verify_fix.run_test_n_times") as mock_run:
                    mock_run.side_effect = [
                        {"runs": 2, "passes": 1, "failures": 1},
                        {"runs": 2, "passes": 2, "failures": 0},
                    ]
                    res = verify_fix("test::node", diff_path, n=2)
                    assert res["fix_confirmed"] is True


# ---------------------------------------------------------------------------
# 17. Test A Path
# ---------------------------------------------------------------------------

def test_test_a_path(tmp_path):
    test_a_name = "tests/test_a_order.py::test_cache_starts_clean"
    diff_file = tmp_path / "fix_test_a.diff"
    diff_file.write_text("--- a/dummy\n+++ b/dummy\n", encoding="utf-8")

    # In isolation, Test A baseline produces 0 failures
    with patch("analyzer.verify_fix.create_scratch_copy", return_value=tmp_path):
        with patch("analyzer.verify_fix.apply_diff", return_value=True):
            with patch("analyzer.verify_fix.run_test_n_times") as mock_run:
                mock_run.side_effect = [
                    {"runs": 10, "passes": 10, "failures": 0},  # baseline in isolation
                    {"runs": 10, "passes": 10, "failures": 0},  # after fix
                    {"runs": 3, "passes": 3, "failures": 0},    # neighbor 1
                    {"runs": 3, "passes": 3, "failures": 0},    # neighbor 2
                ]
                res = verify_fix(
                    test_a_name,
                    diff_file,
                    neighbor_tests=DEFAULT_NEIGHBORS.get(test_a_name),
                    n=10,
                )

                # Fix is not confirmed because baseline had 0 failures (honest reporting)
                assert res["fix_confirmed"] is False
                assert res["before_fix"]["failures"] == 0
                assert res["after_fix"]["failures"] == 0
                assert res["regression_safe"] is True


# ---------------------------------------------------------------------------
# 18. Test D Path
# ---------------------------------------------------------------------------

def test_test_d_path(tmp_path):
    test_d_name = "tests/test_d_regression.py::test_calculate_total"
    diff_file = tmp_path / "fix_test_d.diff"
    diff_file.write_text("--- a/dummy\n+++ b/dummy\n", encoding="utf-8")

    # Test D baseline produces genuine failures (e.g. 4/10)
    with patch("analyzer.verify_fix.create_scratch_copy", return_value=tmp_path):
        with patch("analyzer.verify_fix.apply_diff", return_value=True):
            with patch("analyzer.verify_fix.run_test_n_times") as mock_run:
                mock_run.side_effect = [
                    {"runs": 10, "passes": 6, "failures": 4},   # baseline failures
                    {"runs": 10, "passes": 10, "failures": 0},  # all pass after fix
                    {"runs": 3, "passes": 3, "failures": 0},    # neighbor 1
                    {"runs": 3, "passes": 3, "failures": 0},    # neighbor 2
                ]
                res = verify_fix(
                    test_d_name,
                    diff_file,
                    neighbor_tests=DEFAULT_NEIGHBORS.get(test_d_name),
                    n=10,
                )

                # Fix is confirmed because before_fix had failures and after_fix has 0
                assert res["fix_confirmed"] is True
                assert res["before_fix"]["failures"] == 4
                assert res["after_fix"]["failures"] == 0
                assert res["regression_safe"] is True
