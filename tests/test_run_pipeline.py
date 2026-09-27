"""
tests/test_run_pipeline.py — Unit tests for Step 14 Full Pipeline Orchestrator.

Mocks all Bob-dependent stages to prevent spending Bobcoins during automated testing.

Tests:
1. Stage ordering and structure
2. Timing fields
3. Pipeline log structure and persistence
4. No-diff verification skip
5. Neighbor propagation
6. Safe filename generation
7. Error handling across stages
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from analyzer.run_pipeline import (
    _safe_name,
    run_full_pipeline,
)


@pytest.fixture
def mock_stages():
    """Mock the 5 pipeline stages."""
    with patch("analyzer.run_pipeline.run_planner") as m_planner, \
         patch("analyzer.run_pipeline.run_all_subagents_parallel") as m_inv, \
         patch("analyzer.run_pipeline.run_coordinator") as m_coord, \
         patch("analyzer.run_pipeline.run_fix_agent") as m_fix, \
         patch("analyzer.run_pipeline.run_verification") as m_verif:

        m_planner.return_value = {
            "test_name": "tests/test_a_order.py::test_cache_starts_clean",
            "test_file": "tests/test_a_order.py",
            "hypotheses": ["order_dependency"],
            "search_targets": ["demo-repo/src/shared_cache.py"],
        }
        m_inv.return_value = {
            "test_name": "tests/test_a_order.py::test_cache_starts_clean",
            "results": {
                "isolation": {"subagent": "isolation", "confidence": "high"},
                "shuffle": {"subagent": "shuffle", "confidence": "high"},
                "static_scan": {"subagent": "static_scan", "confidence": "high"},
                "history": {"subagent": "history", "confidence": "high"},
                "bisect": {"subagent": "bisect", "confidence": "low"},
            },
            "timings": {
                "isolation": 1.0,
                "shuffle": 1.2,
                "static_scan": 0.8,
                "history": 1.1,
                "bisect": 0.5,
            },
            "overall_wall_clock_seconds": 2.5,
            "errors": {},
        }
        m_coord.return_value = {
            "test_name": "tests/test_a_order.py::test_cache_starts_clean",
            "ranked_cause": "order_dependency",
            "winning_evidence": "Shuffle and isolation demonstrate order dependency",
            "convergence_count": 4,
            "all_evidence_summary": [],
            "confidence": "high",
        }
        m_fix.return_value = {
            "subagent": "fix_agent",
            "test_name": "tests/test_a_order.py::test_cache_starts_clean",
            "fix_summary": "Add autouse reset_cache fixture",
            "justification": "Resets shared cache between tests",
            "confidence": "high",
        }
        m_verif.return_value = {
            "subagent": "verification",
            "test_name": "tests/test_a_order.py::test_cache_starts_clean",
            "before_fix": {"runs": 10, "passes": 10, "failures": 0},
            "after_fix": {"runs": 10, "passes": 10, "failures": 0},
            "fix_confirmed": False,
            "regression_check": {},
            "regression_safe": True,
        }

        yield {
            "planner": m_planner,
            "investigation": m_inv,
            "coordinator": m_coord,
            "fix_agent": m_fix,
            "verification": m_verif,
        }


def test_stage_ordering_and_structure(tmp_path, mock_stages):
    test_target = "tests/test_a_order.py::test_cache_starts_clean"
    reports_dir = tmp_path / "reports"
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True)

    # Provide a mock diff file so verification stage runs
    diff_file = evidence_dir / "fix_tests_test_a_order.py__test_cache_starts_clean.diff"
    diff_file.write_text("--- a/dummy\n+++ b/dummy\n", encoding="utf-8")

    result = run_full_pipeline(
        test_name=test_target,
        neighbor_tests=["neighbor_test_1"],
        reports_dir=reports_dir,
        evidence_dir=evidence_dir,
    )

    # Verify calls occurred in correct order
    assert mock_stages["planner"].called
    assert mock_stages["investigation"].called
    assert mock_stages["coordinator"].called
    assert mock_stages["fix_agent"].called
    assert mock_stages["verification"].called

    # Verify stage output structure
    assert result["test_name"] == test_target
    assert set(result["stages"].keys()) == {
        "planner",
        "investigation",
        "coordinator",
        "fix_agent",
        "verification",
    }
    assert result["stages"]["planner"]["output"]["test_name"] == test_target
    assert result["stages"]["coordinator"]["output"]["ranked_cause"] == "order_dependency"
    assert result["stages"]["verification"]["output"]["regression_safe"] is True


def test_timing_fields(tmp_path, mock_stages):
    test_target = "tests/test_a_order.py::test_cache_starts_clean"
    reports_dir = tmp_path / "reports"
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True)

    diff_file = evidence_dir / "fix_tests_test_a_order.py__test_cache_starts_clean.diff"
    diff_file.write_text("--- a/dummy\n+++ b/dummy\n", encoding="utf-8")

    result = run_full_pipeline(
        test_name=test_target,
        reports_dir=reports_dir,
        evidence_dir=evidence_dir,
    )

    assert "total_elapsed" in result
    assert isinstance(result["total_elapsed"], float)
    assert result["total_elapsed"] >= 0.0

    for stage_name, stage_data in result["stages"].items():
        assert "elapsed" in stage_data
        assert isinstance(stage_data["elapsed"], float)
        assert stage_data["elapsed"] >= 0.0


def test_no_diff_verification_skip(tmp_path, mock_stages):
    test_target = "tests/test_a_order.py::test_cache_starts_clean"
    reports_dir = tmp_path / "reports"
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True)

    # No diff file exists in evidence_dir
    result = run_full_pipeline(
        test_name=test_target,
        reports_dir=reports_dir,
        evidence_dir=evidence_dir,
    )

    # Verification must NOT be called
    assert not mock_stages["verification"].called

    verif_stage = result["stages"]["verification"]
    assert verif_stage["output"]["skipped"] is True
    assert verif_stage["output"]["fix_confirmed"] is False
    assert "No usable diff" in verif_stage["output"]["reason"]


def test_neighbor_propagation(tmp_path, mock_stages):
    test_target = "tests/test_a_order.py::test_cache_starts_clean"
    reports_dir = tmp_path / "reports"
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True)

    diff_file = evidence_dir / "fix_tests_test_a_order.py__test_cache_starts_clean.diff"
    diff_file.write_text("--- a/dummy\n+++ b/dummy\n", encoding="utf-8")

    neighbors = [
        "tests/test_shared_cache_mutator.py::test_contaminate_shared_cache",
        "tests/test_shared_cache_stable.py",
    ]

    run_full_pipeline(
        test_name=test_target,
        neighbor_tests=neighbors,
        reports_dir=reports_dir,
        evidence_dir=evidence_dir,
    )

    mock_stages["verification"].assert_called_once()
    call_kwargs = mock_stages["verification"].call_args.kwargs
    assert call_kwargs["neighbor_tests"] == neighbors


def test_safe_filename_generation(tmp_path, mock_stages):
    test_target = "tests/test_a_order.py::test_cache_starts_clean"
    reports_dir = tmp_path / "reports"
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True)

    run_full_pipeline(
        test_name=test_target,
        reports_dir=reports_dir,
        evidence_dir=evidence_dir,
    )

    safe_name = _safe_name(test_target)
    assert safe_name == "tests_test_a_order_py__test_cache_starts_clean"

    expected_file = reports_dir / f"pipeline_run_{safe_name}.json"
    assert expected_file.exists()

    data = json.loads(expected_file.read_text(encoding="utf-8"))
    assert data["test_name"] == test_target
    assert "stages" in data


def test_error_handling_planner(tmp_path, mock_stages):
    test_target = "tests/test_a_order.py::test_cache_starts_clean"
    reports_dir = tmp_path / "reports"
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True)

    mock_stages["planner"].side_effect = RuntimeError("Planner failed to connect")

    result = run_full_pipeline(
        test_name=test_target,
        reports_dir=reports_dir,
        evidence_dir=evidence_dir,
    )

    assert result["stages"]["planner"]["output"] is None
    assert "error" in result["stages"]["planner"]
    assert "Planner failed to connect" in result["stages"]["planner"]["error"]
    assert not mock_stages["investigation"].called


def test_error_handling_investigation(tmp_path, mock_stages):
    test_target = "tests/test_a_order.py::test_cache_starts_clean"
    reports_dir = tmp_path / "reports"
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(parents=True)

    mock_stages["investigation"].side_effect = RuntimeError("Subagent execution timeout")

    result = run_full_pipeline(
        test_name=test_target,
        reports_dir=reports_dir,
        evidence_dir=evidence_dir,
    )

    assert mock_stages["planner"].called
    assert result["stages"]["investigation"]["output"] is None
    assert "Subagent execution timeout" in result["stages"]["investigation"]["error"]
    assert not mock_stages["coordinator"].called
