"""
tests/test_render_report.py — Unit tests for Step 15 Report Renderer.
"""

from __future__ import annotations

from pathlib import Path
from reports.render_report import safe_id, render_report

_TARGET_TEST = "tests/test_a_order.py::test_cache_starts_clean"


def test_safe_id():
    assert safe_id(_TARGET_TEST) == "tests_test_a_order_py__test_cache_starts_clean"


def test_render_report_generates_markdown():
    out_path = render_report(_TARGET_TEST)
    assert out_path.exists()
    assert out_path.name == "report_tests_test_a_order_py__test_cache_starts_clean.md"

    content = out_path.read_text(encoding="utf-8")

    # Verify all 7 required sections are present
    assert "# FlakeFinder Investigation Report" in content
    assert "## 1. Test / Run Summary" in content
    assert "## 2. Root Cause" in content
    assert "## 3. Five Parallel Investigation Agents" in content
    assert "## 4. Parallelism Proof" in content
    assert "## 5. Proposed Fix" in content
    assert "## 6. Independent Verification" in content
    assert "## 7. Evidence-Based Conclusion" in content

    # Verify 5 subagents are visible in the table
    for agent in ["isolation", "shuffle", "bisect", "static_scan", "history"]:
        assert f"`{agent}`" in content

    # Verify diff block is present
    assert "```diff" in content

    # Verify verification numbers are visible
    assert "| **Before fix** | 10 | 10 | 0 | 100.0% |" in content
    assert "| **After fix** | 10 | 10 | 0 | 100.0% |" in content
    assert "fix_confirmed" in content
    assert "regression_safe" in content
