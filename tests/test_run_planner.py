"""
tests/test_run_planner.py — Step 6 tests for run_planner.py

Run from FlakeFinder/ directory:
    pytest tests/test_run_planner.py

All tests use the fallback path (Bob is not expected to be on PATH in this
environment). Two tests explicitly exercise the Bob path by monkeypatching.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure FlakeFinder root is on sys.path so `import run_planner` works.
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import run_planner  # noqa: E402

# ---------------------------------------------------------------------------
# Canonical test node ID used across all tests
# ---------------------------------------------------------------------------

NODE_ID = "tests/test_a_order.py::test_cache_starts_clean"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call_run(node_id: str = NODE_ID) -> dict:
    """Call run_planner.run() and return the validated dict."""
    return run_planner.run(node_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOutputSchema:
    """The output must satisfy the Planner JSON schema exactly."""

    def test_output_schema_valid(self):
        data = _call_run()
        required = {"test_name", "test_file", "hypotheses", "search_targets"}
        assert required == set(data.keys()), (
            f"Expected keys {sorted(required)}, got {sorted(data.keys())}"
        )

    def test_hypotheses_not_empty(self):
        data = _call_run()
        assert isinstance(data["hypotheses"], list)
        assert len(data["hypotheses"]) >= 1, "hypotheses must be a non-empty list"

    def test_search_targets_not_empty(self):
        data = _call_run()
        assert isinstance(data["search_targets"], list)
        assert len(data["search_targets"]) >= 1, "search_targets must be a non-empty list"


class TestNodeIdPreservation:
    """The full pytest node ID must be preserved verbatim in test_name."""

    def test_full_node_id_preserved(self):
        data = _call_run()
        assert data["test_name"] == NODE_ID, (
            f"Expected test_name={NODE_ID!r}, got {data['test_name']!r}"
        )

    def test_test_file_derived_correctly(self):
        data = _call_run()
        expected_file = NODE_ID.split("::")[0]
        assert data["test_file"] == expected_file, (
            f"Expected test_file={expected_file!r}, got {data['test_file']!r}"
        )

    def test_node_id_not_truncated_to_function_name(self):
        """test_name must include the file path, not just the function name."""
        data = _call_run()
        assert "/" in data["test_name"] or "\\" in data["test_name"], (
            f"test_name looks truncated (no path separator): {data['test_name']!r}"
        )
        assert "::" in data["test_name"], (
            f"test_name must contain '::' separator: {data['test_name']!r}"
        )


class TestFallbackBehavior:
    """The fallback must produce valid output when Bob is absent."""

    def test_fallback_runs_when_bob_absent(self):
        """With bob patched off PATH, fallback runs and returns valid JSON."""
        with patch("run_planner.shutil.which", return_value=None):
            data = run_planner.run(NODE_ID)
        run_planner._validate(data)  # must not raise
        assert data["test_name"] == NODE_ID

    def test_fallback_result_is_serialisable(self):
        """Fallback output must be JSON-serialisable (no non-JSON types)."""
        with patch("run_planner.shutil.which", return_value=None):
            data = run_planner.run(NODE_ID)
        # json.dumps must not raise
        serialised = json.dumps(data)
        roundtripped = json.loads(serialised)
        assert roundtripped["test_name"] == NODE_ID


class TestBobFailureLogging:
    """Bob failures must be explicit on stderr, never silent."""

    def test_bob_failure_nonzero_exit_logs_to_stderr(self, capsys):
        """When bob returns non-zero exit, stderr must contain 'BOB FAILED:'."""
        fake_proc = MagicMock()
        fake_proc.returncode = 1
        fake_proc.stderr = "some internal error"
        fake_proc.stdout = ""

        with patch("run_planner.shutil.which", return_value="/usr/local/bin/bob"):
            with patch("run_planner.subprocess.run", return_value=fake_proc):
                data = run_planner.run(NODE_ID)

        captured = capsys.readouterr()
        assert "BOB FAILED:" in captured.err, (
            f"Expected 'BOB FAILED:' in stderr. Got: {captured.err!r}"
        )
        # Fallback must have activated: result is still valid
        run_planner._validate(data)

    def test_bob_failure_invalid_json_logs_to_stderr(self, capsys):
        """When bob returns non-JSON stdout, stderr must contain 'BOB FAILED:'."""
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.stderr = ""
        fake_proc.stdout = "This is not JSON at all."

        with patch("run_planner.shutil.which", return_value="/usr/local/bin/bob"):
            with patch("run_planner.subprocess.run", return_value=fake_proc):
                data = run_planner.run(NODE_ID)

        captured = capsys.readouterr()
        assert "BOB FAILED:" in captured.err, (
            f"Expected 'BOB FAILED:' in stderr. Got: {captured.err!r}"
        )
        run_planner._validate(data)

    def test_bob_absent_does_not_log_to_stderr(self, capsys):
        """When bob is simply not on PATH, stderr must be silent (no BOB FAILED:)."""
        with patch("run_planner.shutil.which", return_value=None):
            run_planner.run(NODE_ID)

        captured = capsys.readouterr()
        assert "BOB FAILED:" not in captured.err, (
            "bob not on PATH is not a failure — stderr must be silent"
        )


class TestSubagentIndependence:
    """Planner hypotheses must be data labels only — not execution paths."""

    def test_hypotheses_are_plain_labels_not_paths(self):
        """Hypothesis values must not contain file paths or script references."""
        data = _call_run()
        for h in data["hypotheses"]:
            assert ".sh" not in h, f"hypothesis contains .sh path: {h!r}"
            assert ".py" not in h, f"hypothesis contains .py path: {h!r}"
            assert "/" not in h, f"hypothesis contains path separator: {h!r}"
            assert "\\" not in h, f"hypothesis contains path separator: {h!r}"

    def test_hypotheses_values_are_known_labels(self):
        """Hypothesis values must be drawn from the four documented labels."""
        known = {"order_dependency", "race_condition", "non_determinism", "regression"}
        data = _call_run()
        for h in data["hypotheses"]:
            assert h in known, (
                f"Unknown hypothesis label {h!r}. Must be one of {sorted(known)}"
            )


class TestSchemaValidationFunction:
    """_validate() must raise ValueError on malformed dicts."""

    def test_validate_rejects_missing_keys(self):
        with pytest.raises(ValueError, match="missing keys"):
            run_planner._validate({"test_name": NODE_ID})

    def test_validate_rejects_empty_hypotheses(self):
        with pytest.raises(ValueError):
            run_planner._validate({
                "test_name": NODE_ID,
                "test_file": "tests/test_a_order.py",
                "hypotheses": [],
                "search_targets": ["tests/test_a_order.py"],
            })

    def test_validate_rejects_empty_search_targets(self):
        with pytest.raises(ValueError):
            run_planner._validate({
                "test_name": NODE_ID,
                "test_file": "tests/test_a_order.py",
                "hypotheses": ["order_dependency"],
                "search_targets": [],
            })

    def test_validate_rejects_truncated_node_id(self):
        with pytest.raises(ValueError, match="full pytest node ID"):
            run_planner._validate({
                "test_name": "test_cache_starts_clean",  # no file path
                "test_file": "tests/test_a_order.py",
                "hypotheses": ["order_dependency"],
                "search_targets": ["tests/test_a_order.py"],
            })

    def test_validate_accepts_valid_dict(self):
        run_planner._validate({
            "test_name": NODE_ID,
            "test_file": "tests/test_a_order.py",
            "hypotheses": ["order_dependency"],
            "search_targets": ["tests/test_a_order.py", "src/shared_cache.py"],
        })  # must not raise
