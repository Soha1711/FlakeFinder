"""
analyzer/run_pipeline.py — FlakeFinder Step 14 Full Pipeline Orchestrator.

Usage:
    python analyzer/run_pipeline.py <pytest_node_id> [neighbor_tests...]

Example:
    python analyzer/run_pipeline.py \\
        "tests/test_a_order.py::test_cache_starts_clean" \\
        "tests/test_shared_cache_mutator.py::test_contaminate_shared_cache" \\
        "tests/test_shared_cache_stable.py"

Pipeline Sequence:
    Stage 1 — Planner (analyzer.run_planner.run_planner)
    Stage 2 — Parallel Investigation (analyzer.run_investigation.run_all_subagents_parallel)
    Stage 3 — Coordinator (analyzer.coordinator.run_coordinator)
    Stage 4 — Fix Agent (analyzer.fix_agent.run_fix_agent)
    Stage 5 — Verification (analyzer.verify_fix.run_verification)

Output:
    reports/pipeline_run_<safe_test_name>.json
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path
_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from analyzer.run_planner import run_planner
from analyzer.run_investigation import run_all_subagents_parallel
from analyzer.coordinator import run_coordinator
from analyzer.fix_agent import run_fix_agent
from analyzer.verify_fix import run_verification

_REPORTS_DIR = _HERE / "reports"
_EVIDENCE_DIR = _HERE / "state" / "evidence"

DEFAULT_NEIGHBORS: dict[str, list[str]] = {
    "tests/test_a_order.py::test_cache_starts_clean": [
        "tests/test_shared_cache_mutator.py::test_contaminate_shared_cache",
        "tests/test_shared_cache_stable.py",
    ],
    "tests/test_d_regression.py::test_calculate_total": [
        "tests/test_regression_stable.py::test_calculate_total_empty_list",
        "tests/test_regression_stable.py::test_calculate_total_multiple_items",
    ],
}


def _safe_name(test_name: str) -> str:
    """Deterministic regex-based safe-name matching Step 14 report specification."""
    return re.sub(r"[^\w\-]", "_", test_name)


def _safe_alnum(value: str) -> str:
    """Alnum safe-name convention matching fix_agent and coordinator."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in value)


def _log_header(stage_number: int, stage_name: str) -> None:
    sep = "=" * 60
    print(f"\n[PIPELINE] {sep}", file=sys.stderr, flush=True)
    print(f"[PIPELINE] Stage {stage_number} — {stage_name}", file=sys.stderr, flush=True)
    print(f"[PIPELINE] {sep}\n", file=sys.stderr, flush=True)


def _resolve_diff_path(test_name: str, evidence_dir: Path | None = None) -> Path | None:
    """Find the diff file produced by the Fix Agent for test_name."""
    edir = evidence_dir or _EVIDENCE_DIR
    candidates = [
        edir / f"fix_{_safe_alnum(test_name)}.diff",
        edir / f"fix_{_safe_name(test_name)}.diff",
    ]
    for cand in candidates:
        if cand.exists() and cand.stat().st_size > 0:
            return cand
    return None


def run_full_pipeline(
    test_name: str,
    neighbor_tests: list[str] | None = None,
    reports_dir: Path | None = None,
    evidence_dir: Path | None = None,
) -> dict[str, Any]:
    """
    Execute the full end-to-end FlakeFinder pipeline for test_name.

    Stages:
        1. Planner
        2. Parallel Investigation (5 subagents)
        3. Coordinator
        4. Fix Agent
        5. Verification
    """
    if "::" not in test_name:
        raise ValueError(
            f"test_name must be a full pytest node ID (file::function), got: {test_name!r}"
        )

    r_dir = reports_dir or _REPORTS_DIR
    e_dir = evidence_dir or _EVIDENCE_DIR

    pipeline_log: dict[str, Any] = {
        "test_name": test_name,
        "stages": {},
        "total_elapsed": 0.0,
    }

    pipeline_t0 = time.monotonic()

    # -----------------------------------------------------------------------
    # Stage 1 — Planner
    # -----------------------------------------------------------------------
    _log_header(1, "Planner")
    t0 = time.monotonic()
    try:
        planner_output = run_planner(test_name)
        elapsed = time.monotonic() - t0
        pipeline_log["stages"]["planner"] = {
            "elapsed": round(elapsed, 3),
            "output": planner_output,
        }
        print(f"[PIPELINE] Stage 1 Planner finished in {elapsed:.2f}s", file=sys.stderr, flush=True)
    except Exception as exc:
        elapsed = time.monotonic() - t0
        pipeline_log["stages"]["planner"] = {
            "elapsed": round(elapsed, 3),
            "error": str(exc),
            "output": None,
        }
        print(f"[PIPELINE] Stage 1 Planner FAILED: {exc}", file=sys.stderr, flush=True)
        pipeline_log["total_elapsed"] = round(time.monotonic() - pipeline_t0, 3)
        _persist_pipeline_report(pipeline_log, test_name, r_dir)
        return pipeline_log

    # -----------------------------------------------------------------------
    # Stage 2 — Parallel Investigation
    # -----------------------------------------------------------------------
    _log_header(2, "Parallel Investigation")
    t0 = time.monotonic()
    try:
        investigation_output = run_all_subagents_parallel(test_name)
        elapsed = time.monotonic() - t0
        pipeline_log["stages"]["investigation"] = {
            "elapsed": round(elapsed, 3),
            "output": investigation_output,
        }
        if investigation_output.get("errors"):
            print(
                f"[PIPELINE] WARNING: Investigation encountered subagent errors: {investigation_output['errors']}",
                file=sys.stderr,
                flush=True,
            )
        print(f"[PIPELINE] Stage 2 Investigation finished in {elapsed:.2f}s", file=sys.stderr, flush=True)
    except Exception as exc:
        elapsed = time.monotonic() - t0
        pipeline_log["stages"]["investigation"] = {
            "elapsed": round(elapsed, 3),
            "error": str(exc),
            "output": None,
        }
        print(f"[PIPELINE] Stage 2 Investigation FAILED: {exc}", file=sys.stderr, flush=True)
        pipeline_log["total_elapsed"] = round(time.monotonic() - pipeline_t0, 3)
        _persist_pipeline_report(pipeline_log, test_name, r_dir)
        return pipeline_log

    # -----------------------------------------------------------------------
    # Stage 3 — Coordinator
    # -----------------------------------------------------------------------
    _log_header(3, "Coordinator")
    t0 = time.monotonic()
    try:
        coordinator_output = run_coordinator(test_name, evidence_dir=e_dir)
        elapsed = time.monotonic() - t0
        pipeline_log["stages"]["coordinator"] = {
            "elapsed": round(elapsed, 3),
            "output": coordinator_output,
        }
        print(f"[PIPELINE] Stage 3 Coordinator finished in {elapsed:.2f}s", file=sys.stderr, flush=True)
    except Exception as exc:
        elapsed = time.monotonic() - t0
        pipeline_log["stages"]["coordinator"] = {
            "elapsed": round(elapsed, 3),
            "error": str(exc),
            "output": None,
        }
        print(f"[PIPELINE] Stage 3 Coordinator FAILED: {exc}", file=sys.stderr, flush=True)
        pipeline_log["total_elapsed"] = round(time.monotonic() - pipeline_t0, 3)
        _persist_pipeline_report(pipeline_log, test_name, r_dir)
        return pipeline_log

    # -----------------------------------------------------------------------
    # Stage 4 — Fix Agent
    # -----------------------------------------------------------------------
    _log_header(4, "Fix Agent")
    t0 = time.monotonic()
    try:
        fix_output = run_fix_agent(test_name, evidence_dir=e_dir)
        elapsed = time.monotonic() - t0
        pipeline_log["stages"]["fix_agent"] = {
            "elapsed": round(elapsed, 3),
            "output": fix_output,
        }
        print(f"[PIPELINE] Stage 4 Fix Agent finished in {elapsed:.2f}s", file=sys.stderr, flush=True)
    except Exception as exc:
        elapsed = time.monotonic() - t0
        pipeline_log["stages"]["fix_agent"] = {
            "elapsed": round(elapsed, 3),
            "error": str(exc),
            "output": None,
        }
        print(f"[PIPELINE] Stage 4 Fix Agent FAILED: {exc}", file=sys.stderr, flush=True)
        pipeline_log["total_elapsed"] = round(time.monotonic() - pipeline_t0, 3)
        _persist_pipeline_report(pipeline_log, test_name, r_dir)
        return pipeline_log

    # -----------------------------------------------------------------------
    # Stage 5 — Verification
    # -----------------------------------------------------------------------
    _log_header(5, "Verification")
    t0 = time.monotonic()
    diff_path = _resolve_diff_path(test_name, evidence_dir=e_dir)

    if diff_path is None:
        elapsed = time.monotonic() - t0
        print(
            "[PIPELINE] WARNING: No usable diff produced by Fix Agent. Skipping Verification.",
            file=sys.stderr,
            flush=True,
        )
        pipeline_log["stages"]["verification"] = {
            "elapsed": round(elapsed, 3),
            "output": {
                "subagent": "verification",
                "test_name": test_name,
                "fix_confirmed": False,
                "skipped": True,
                "reason": "No usable diff produced by Fix Agent",
            },
        }
    else:
        try:
            print(f"[PIPELINE] Applying diff: {diff_path}", file=sys.stderr, flush=True)
            verification_output = run_verification(
                test_name=test_name,
                diff_path=diff_path,
                neighbor_tests=neighbor_tests,
                evidence_dir=e_dir,
            )
            elapsed = time.monotonic() - t0
            pipeline_log["stages"]["verification"] = {
                "elapsed": round(elapsed, 3),
                "output": verification_output,
            }
            print(f"[PIPELINE] Stage 5 Verification finished in {elapsed:.2f}s", file=sys.stderr, flush=True)
        except Exception as exc:
            elapsed = time.monotonic() - t0
            pipeline_log["stages"]["verification"] = {
                "elapsed": round(elapsed, 3),
                "error": str(exc),
                "output": {
                    "subagent": "verification",
                    "test_name": test_name,
                    "fix_confirmed": False,
                    "error": str(exc),
                },
            }
            print(f"[PIPELINE] Stage 5 Verification FAILED: {exc}", file=sys.stderr, flush=True)

    # -----------------------------------------------------------------------
    # Finish Pipeline
    # -----------------------------------------------------------------------
    total_elapsed = time.monotonic() - pipeline_t0
    pipeline_log["total_elapsed"] = round(total_elapsed, 3)

    report_path = _persist_pipeline_report(pipeline_log, test_name, r_dir)
    print(
        f"\n[PIPELINE] Completed in {total_elapsed:.2f}s. Report saved to: {report_path}",
        file=sys.stderr,
        flush=True,
    )

    return pipeline_log


def _persist_pipeline_report(
    pipeline_log: dict[str, Any],
    test_name: str,
    reports_dir: Path,
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_file = reports_dir / f"pipeline_run_{_safe_name(test_name)}.json"
    report_file.write_text(
        json.dumps(pipeline_log, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return report_file


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main() -> int:
    if len(sys.argv) < 2:
        print(
            "Usage: python analyzer/run_pipeline.py <pytest_node_id> [neighbor_tests...]",
            file=sys.stderr,
        )
        print(
            "Example: python analyzer/run_pipeline.py "
            '"tests/test_a_order.py::test_cache_starts_clean" '
            '"tests/test_shared_cache_mutator.py::test_contaminate_shared_cache" '
            '"tests/test_shared_cache_stable.py"',
            file=sys.stderr,
        )
        return 2

    test_name = sys.argv[1]
    if len(sys.argv) > 2:
        neighbor_tests = sys.argv[2:]
    else:
        neighbor_tests = DEFAULT_NEIGHBORS.get(test_name)

    try:
        report = run_full_pipeline(test_name, neighbor_tests=neighbor_tests)
        print(json.dumps(report, indent=2))
        return 0
    except Exception as exc:
        print(f"[PIPELINE] FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
