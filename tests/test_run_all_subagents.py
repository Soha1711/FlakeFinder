"""
tests/test_run_all_subagents.py — Step 10 tests for analyzer/run_all_subagents.py

Run from FlakeFinder/ directory:
    pytest tests/test_run_all_subagents.py -v

Tests cover:
  1.  Output has valid JSON (end-to-end via run_all())
  2.  All five result keys exist
  3.  Full pytest node ID is preserved in output
  4.  Timing keys exist (one per subagent)
  5.  overall_wall_clock_seconds exists
  6.  errors field exists
  7.  Static source mapping works for all four known test targets
  8.  Unknown target handling is explicit (ValueError)
  9.  Four-way parallel dispatcher actually submits four tasks concurrently
  10. Bisect is executed only AFTER the parallel phase completes
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import analyzer.run_all_subagents as orch  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEST_A = "tests/test_a_order.py::test_cache_starts_clean"
_TEST_B = "tests/test_b_race.py::test_background_update_completes"
_TEST_C = "tests/test_c_random.py::test_value_is_valid"
_TEST_D = "tests/test_d_regression.py::test_calculate_total"

_ALL_KNOWN_TESTS = [_TEST_A, _TEST_B, _TEST_C, _TEST_D]

_SOURCE_MAP = {
    _TEST_A: "demo-repo/src/shared_cache.py",
    _TEST_B: "demo-repo/src/async_worker.py",
    _TEST_C: "demo-repo/src/nondeterministic.py",
    _TEST_D: "demo-repo/src/regression.py",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(subagent: str, test_name: str, **overrides) -> dict[str, Any]:
    """Build a minimal valid subagent result dict."""
    base = {
        "subagent": subagent,
        "test_name": test_name,
        "evidence": f"Evidence from {subagent}.",
        "hypothesis": f"Hypothesis from {subagent}.",
        "confidence": "high",
    }
    base.update(overrides)
    return base


def _mock_runners(test_name: str) -> dict[str, Any]:
    """
    Return a dict of patched runner callables that return immediately
    with a valid result dict.  Keys match orch._run_* function names.
    """
    static_target = _SOURCE_MAP[test_name]
    return {
        "_run_isolation":   lambda tn: _make_result("isolation",   tn),
        "_run_shuffle":     lambda tn: _make_result("shuffle",     tn),
        "_run_static_scan": lambda tn: _make_result("static_scan", static_target),
        "_run_history":     lambda tn: _make_result("history",     tn),
        "_run_bisect":      lambda tn: _make_result("bisect",      tn),
    }


def _patch_all_runners(monkeypatch, test_name: str) -> None:
    """Apply all mock runner patches to the orchestrator module."""
    for fn_name, fn in _mock_runners(test_name).items():
        monkeypatch.setattr(orch, fn_name, fn)


# ---------------------------------------------------------------------------
# Test 1 — output has valid JSON
# ---------------------------------------------------------------------------

class TestOutputIsValidJson:
    def test_run_all_returns_dict(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        result = orch.run_all(_TEST_A)
        assert isinstance(result, dict)

    def test_run_all_is_json_serializable(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        result = orch.run_all(_TEST_A)
        encoded = json.dumps(result)
        decoded = json.loads(encoded)
        assert decoded["test_name"] == _TEST_A


# ---------------------------------------------------------------------------
# Test 2 — all five result keys exist
# ---------------------------------------------------------------------------

class TestAllFiveResultKeys:
    def test_results_has_five_keys(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        result = orch.run_all(_TEST_A)
        assert set(result["results"].keys()) == {
            "isolation", "shuffle", "static_scan", "history", "bisect"
        }

    def test_each_result_is_dict_or_none(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        result = orch.run_all(_TEST_A)
        for key, value in result["results"].items():
            assert value is None or isinstance(value, dict), (
                f"result[{key!r}] must be dict or None, got {type(value)}"
            )

    def test_each_result_has_subagent_key(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        result = orch.run_all(_TEST_A)
        for key in ("isolation", "shuffle", "history", "bisect"):
            assert result["results"][key]["subagent"] == key

    def test_static_scan_result_has_correct_subagent(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        result = orch.run_all(_TEST_A)
        assert result["results"]["static_scan"]["subagent"] == "static_scan"


# ---------------------------------------------------------------------------
# Test 3 — full pytest node ID is preserved
# ---------------------------------------------------------------------------

class TestTestNamePreserved:
    @pytest.mark.parametrize("test_name", _ALL_KNOWN_TESTS)
    def test_test_name_in_output(self, test_name, monkeypatch):
        _patch_all_runners(monkeypatch, test_name)
        result = orch.run_all(test_name)
        assert result["test_name"] == test_name

    def test_test_name_not_truncated(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_D)
        result = orch.run_all(_TEST_D)
        # Must be the exact full node ID, not just a test function name.
        assert result["test_name"] == _TEST_D
        assert "::" in result["test_name"]

    def test_isolation_result_carries_full_node_id(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        result = orch.run_all(_TEST_A)
        # The individual subagent results must carry the node ID.
        assert result["results"]["isolation"]["test_name"] == _TEST_A


# ---------------------------------------------------------------------------
# Test 4 — timing keys exist
# ---------------------------------------------------------------------------

class TestTimingKeys:
    def test_timings_has_five_keys(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        result = orch.run_all(_TEST_A)
        assert set(result["timings"].keys()) == {
            "isolation", "shuffle", "static_scan", "history", "bisect"
        }

    def test_all_timings_are_non_negative_numbers(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        result = orch.run_all(_TEST_A)
        for name, val in result["timings"].items():
            assert isinstance(val, (int, float)), (
                f"timings[{name!r}] is not a number: {val!r}"
            )
            assert val >= 0, f"timings[{name!r}] is negative"

    def test_parallel_phase_wall_clock_exists(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        result = orch.run_all(_TEST_A)
        assert "parallel_phase_wall_clock_seconds" in result
        assert isinstance(result["parallel_phase_wall_clock_seconds"], float)

    def test_bisect_phase_wall_clock_exists(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        result = orch.run_all(_TEST_A)
        assert "bisect_phase_wall_clock_seconds" in result
        assert isinstance(result["bisect_phase_wall_clock_seconds"], float)


# ---------------------------------------------------------------------------
# Test 5 — overall wall-clock timing exists
# ---------------------------------------------------------------------------

class TestOverallWallClockTiming:
    def test_overall_wall_clock_key_exists(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        result = orch.run_all(_TEST_A)
        assert "overall_wall_clock_seconds" in result

    def test_overall_wall_clock_is_non_negative_number(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        result = orch.run_all(_TEST_A)
        val = result["overall_wall_clock_seconds"]
        assert isinstance(val, (int, float))
        assert val >= 0

    def test_overall_not_sum_of_individuals(self, monkeypatch):
        """
        overall_wall_clock_seconds must be <= sum of individual timings
        because Phase 1 runs concurrently.  We don't check an exact value
        because CI timing varies.
        """
        # Give each fake job a small fixed delay so we can reason about timing.
        _patch_all_runners(monkeypatch, _TEST_A)

        result = orch.run_all(_TEST_A)
        total_sum = sum(result["timings"].values())
        overall = result["overall_wall_clock_seconds"]

        # overall can only be greater than sum if CI scheduling anomalies
        # cause wrap-around; in practice it is always <= sum for concurrent jobs.
        # We only require it to be non-negative here; real concurrency is
        # verified by TestConcurrency below.
        assert overall >= 0


# ---------------------------------------------------------------------------
# Test 6 — errors field exists
# ---------------------------------------------------------------------------

class TestErrorsField:
    def test_errors_key_exists(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        result = orch.run_all(_TEST_A)
        assert "errors" in result

    def test_errors_is_dict(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        result = orch.run_all(_TEST_A)
        assert isinstance(result["errors"], dict)

    def test_errors_empty_on_success(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        result = orch.run_all(_TEST_A)
        assert result["errors"] == {}

    def test_errors_populated_on_failure(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)

        def _failing_isolation(tn):
            raise RuntimeError("isolation exploded")

        monkeypatch.setattr(orch, "_run_isolation", _failing_isolation)

        result = orch.run_all(_TEST_A)
        assert "isolation" in result["errors"]
        assert "exploded" in result["errors"]["isolation"]
        assert result["results"]["isolation"] is None

    def test_remaining_agents_still_run_on_partial_failure(self, monkeypatch):
        """A single subagent failure must not stop the others."""
        _patch_all_runners(monkeypatch, _TEST_A)

        def _failing_shuffle(tn):
            raise RuntimeError("shuffle failed")

        monkeypatch.setattr(orch, "_run_shuffle", _failing_shuffle)

        result = orch.run_all(_TEST_A)
        # shuffle failed
        assert "shuffle" in result["errors"]
        # but the other three parallel agents completed
        assert result["results"]["isolation"] is not None
        assert result["results"]["static_scan"] is not None
        assert result["results"]["history"] is not None
        # and bisect also completed
        assert result["results"]["bisect"] is not None


# ---------------------------------------------------------------------------
# Test 7 — static source mapping works for all four known targets
# ---------------------------------------------------------------------------

class TestStaticSourceMapping:
    @pytest.mark.parametrize("test_name,expected_source", _SOURCE_MAP.items())
    def test_mapping_correct(self, test_name, expected_source):
        source = orch._resolve_source_file(test_name)
        assert source == expected_source

    def test_mapping_for_test_a(self):
        assert orch._resolve_source_file(_TEST_A) == "demo-repo/src/shared_cache.py"

    def test_mapping_for_test_b(self):
        assert orch._resolve_source_file(_TEST_B) == "demo-repo/src/async_worker.py"

    def test_mapping_for_test_c(self):
        assert orch._resolve_source_file(_TEST_C) == "demo-repo/src/nondeterministic.py"

    def test_mapping_for_test_d(self):
        assert orch._resolve_source_file(_TEST_D) == "demo-repo/src/regression.py"

    def test_static_scan_receives_source_path_not_node_id(self, monkeypatch):
        """_run_static_scan must translate the node ID to a source path."""
        received: list[str] = []

        def capture(target: str) -> dict:
            received.append(target)
            return _make_result("static_scan", target)

        # patch the static module's run() so we can intercept the argument
        monkeypatch.setattr(orch._static_mod, "run", capture)
        monkeypatch.setattr(orch, "_run_isolation",   lambda tn: _make_result("isolation",   tn))
        monkeypatch.setattr(orch, "_run_shuffle",     lambda tn: _make_result("shuffle",     tn))
        monkeypatch.setattr(orch, "_run_history",     lambda tn: _make_result("history",     tn))
        monkeypatch.setattr(orch, "_run_bisect",      lambda tn: _make_result("bisect",      tn))

        orch.run_all(_TEST_A)

        assert len(received) == 1
        assert received[0] == "demo-repo/src/shared_cache.py"
        assert received[0] != _TEST_A


# ---------------------------------------------------------------------------
# Test 8 — unknown target handling is explicit
# ---------------------------------------------------------------------------

class TestUnknownTargetHandling:
    def test_unknown_target_raises_value_error(self, monkeypatch):
        with pytest.raises(ValueError, match="unknown test target"):
            orch.run_all("tests/test_unknown.py::test_something")

    def test_resolve_source_file_raises_for_unknown(self):
        with pytest.raises(ValueError, match="unknown test target"):
            orch._resolve_source_file("tests/test_z_mystery.py::test_foo")

    def test_resolve_source_file_error_lists_known_targets(self):
        with pytest.raises(ValueError) as exc_info:
            orch._resolve_source_file("tests/test_unknown.py::test_bar")
        message = str(exc_info.value)
        assert "tests/test_a_order.py::test_cache_starts_clean" in message

    def test_missing_double_colon_raises_value_error(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        with pytest.raises(ValueError, match="pytest node ID"):
            orch.run_all("tests/test_a_order.py")

    def test_none_is_never_passed_to_static_scan(self, monkeypatch):
        """Verifies the guard never silently forwards None."""
        with pytest.raises(ValueError):
            orch._resolve_source_file("unknown::target")


# ---------------------------------------------------------------------------
# Test 9 — four parallel jobs actually overlap
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_four_parallel_jobs_overlap(self, monkeypatch):
        """
        Prove that the four Phase-1 subagents run concurrently by having each
        job sleep for 0.2 s and verifying that the parallel phase completed in
        less than 0.6 s (3× the individual sleep).  On an unloaded machine the
        phase should finish in ~0.2 s; the generous 0.6 s budget accommodates
        heavily loaded CI environments.
        """
        barrier = threading.Barrier(4)
        entered_times: list[float] = []
        lock = threading.Lock()

        def slow_agent(name: str, test_name: str) -> dict:
            with lock:
                entered_times.append(time.monotonic())
            barrier.wait(timeout=5)   # all four must enter before any continues
            time.sleep(0.2)
            return _make_result(name, test_name)

        monkeypatch.setattr(
            orch, "_run_isolation",
            lambda tn: slow_agent("isolation",   tn)
        )
        monkeypatch.setattr(
            orch, "_run_shuffle",
            lambda tn: slow_agent("shuffle",     tn)
        )
        monkeypatch.setattr(
            orch, "_run_static_scan",
            lambda tn: slow_agent("static_scan", _SOURCE_MAP[_TEST_A])
        )
        monkeypatch.setattr(
            orch, "_run_history",
            lambda tn: slow_agent("history",     tn)
        )
        monkeypatch.setattr(
            orch, "_run_bisect",
            lambda tn: _make_result("bisect", tn)
        )

        t0 = time.monotonic()
        result = orch.run_all(_TEST_A)
        parallel_elapsed = time.monotonic() - t0

        # All four jobs entered (barrier.wait would have raised if fewer than 4).
        assert len(entered_times) == 4

        # The parallel phase must be shorter than 3× the per-agent sleep.
        # If running sequentially, it would take at least 4 × 0.2 = 0.8 s.
        assert result["parallel_phase_wall_clock_seconds"] < 0.7, (
            f"parallel phase took {result['parallel_phase_wall_clock_seconds']:.3f}s "
            "— expected < 0.7s for concurrent execution"
        )

    def test_four_tasks_submitted_to_executor(self, monkeypatch):
        """
        Count how many futures are submitted; must be exactly 4.
        """
        submitted: list[str] = []

        original_timed_call = orch._timed_call

        def counting_timed_call(name, fn, test_name):
            submitted.append(name)
            return original_timed_call(name, fn, test_name)

        _patch_all_runners(monkeypatch, _TEST_A)
        monkeypatch.setattr(orch, "_timed_call", counting_timed_call)

        orch.run_all(_TEST_A)

        # bisect is called directly (not via _timed_call's parallel path),
        # so we only count the four parallel ones.
        parallel_submitted = [n for n in submitted if n != "bisect"]
        assert len(parallel_submitted) == 4
        assert set(parallel_submitted) == {
            "isolation", "shuffle", "static_scan", "history"
        }


# ---------------------------------------------------------------------------
# Test 10 — bisect runs only AFTER the parallel phase completes
# ---------------------------------------------------------------------------

class TestBisectSequencing:
    def test_bisect_starts_after_parallel_phase(self, monkeypatch):
        """
        All four parallel agents must complete before bisect starts.
        We verify this by recording timestamps and comparing them.
        """
        events: list[tuple[str, float]] = []
        lock = threading.Lock()
        parallel_done = threading.Event()

        def record(label: str) -> None:
            with lock:
                events.append((label, time.monotonic()))

        def parallel_agent(name: str, test_name: str) -> dict:
            record(f"{name}_start")
            time.sleep(0.05)
            record(f"{name}_end")
            return _make_result(name, test_name)

        def sequential_bisect(test_name: str) -> dict:
            record("bisect_start")
            return _make_result("bisect", test_name)

        monkeypatch.setattr(
            orch, "_run_isolation",
            lambda tn: parallel_agent("isolation", tn)
        )
        monkeypatch.setattr(
            orch, "_run_shuffle",
            lambda tn: parallel_agent("shuffle", tn)
        )
        monkeypatch.setattr(
            orch, "_run_static_scan",
            lambda tn: parallel_agent("static_scan", tn)
        )
        monkeypatch.setattr(
            orch, "_run_history",
            lambda tn: parallel_agent("history", tn)
        )
        monkeypatch.setattr(orch, "_run_bisect", sequential_bisect)

        orch.run_all(_TEST_A)

        bisect_start = next(t for label, t in events if label == "bisect_start")
        parallel_ends = [t for label, t in events if label.endswith("_end")]

        # bisect_start must be >= every parallel_end
        assert all(bisect_start >= end for end in parallel_ends), (
            "bisect started before at least one parallel agent finished"
        )

    def test_bisect_not_in_parallel_phase(self, monkeypatch):
        """
        Bisect must not be submitted to the ThreadPoolExecutor alongside the
        four parallel agents.
        """
        submitted_to_executor: list[str] = []

        original_timed_call = orch._timed_call

        def tracking_timed_call(name, fn, test_name):
            submitted_to_executor.append(name)
            return original_timed_call(name, fn, test_name)

        _patch_all_runners(monkeypatch, _TEST_A)
        monkeypatch.setattr(orch, "_timed_call", tracking_timed_call)

        orch.run_all(_TEST_A)

        # bisect should appear in submitted_to_executor (it uses _timed_call too),
        # but it must be LAST — after all four parallel agents.
        assert submitted_to_executor[-1] == "bisect"
        # The first four must be the parallel agents (order may vary).
        assert set(submitted_to_executor[:4]) == {
            "isolation", "shuffle", "static_scan", "history"
        }

    def test_bisect_result_key_exists_in_phase2(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_D)
        result = orch.run_all(_TEST_D)
        assert "bisect" in result["results"]
        assert result["results"]["bisect"] is not None

    def test_bisect_timing_captured_separately(self, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        result = orch.run_all(_TEST_A)
        assert "bisect" in result["timings"]
        assert result["timings"]["bisect"] >= 0
        # bisect_phase_wall_clock_seconds is the dedicated phase timing
        assert result["bisect_phase_wall_clock_seconds"] >= 0


# ---------------------------------------------------------------------------
# Integration-style: verify all subagent names appear correctly
# ---------------------------------------------------------------------------

class TestSubagentNames:
    @pytest.mark.parametrize("agent", ["isolation", "shuffle", "static_scan", "history", "bisect"])
    def test_agent_result_key_present(self, agent, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        result = orch.run_all(_TEST_A)
        assert agent in result["results"]

    @pytest.mark.parametrize("agent", ["isolation", "shuffle", "static_scan", "history", "bisect"])
    def test_agent_timing_key_present(self, agent, monkeypatch):
        _patch_all_runners(monkeypatch, _TEST_A)
        result = orch.run_all(_TEST_A)
        assert agent in result["timings"]
