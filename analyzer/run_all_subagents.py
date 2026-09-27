"""
analyzer/run_all_subagents.py — FlakeFinder Step 10 parallel orchestration layer.

Usage:
    python analyzer/run_all_subagents.py "<pytest_node_id>"

Example:
    python analyzer/run_all_subagents.py "tests/test_a_order.py::test_cache_starts_clean"

Architecture:
    PHASE 1 — four subagents run concurrently (ThreadPoolExecutor, max_workers=4):
        isolation, shuffle, static_scan, history

    PHASE 2 — bisect runs sequentially after Phase 1 completes.

    This ordering avoids git-bisect corrupting the demo-repo working tree while
    pytest-based subagents are running against the same repository.

Output (stdout, valid JSON):
    {
        "test_name": "<pytest_node_id>",
        "results": { "isolation": {...}, "shuffle": {...},
                     "static_scan": {...}, "history": {...}, "bisect": {...} },
        "timings": { "isolation": 4.21, ... },
        "overall_wall_clock_seconds": 8.61,
        "parallel_phase_wall_clock_seconds": 5.18,
        "bisect_phase_wall_clock_seconds": 3.42,
        "errors": {}
    }

Logging:
    Status messages are written to stderr only.
    stdout remains pure JSON.
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Ensure FlakeFinder root is on sys.path so runner imports work.
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent.parent

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# ---------------------------------------------------------------------------
# Import the five runner modules (their public run() functions).
# ---------------------------------------------------------------------------

import analyzer.run_investigation as _isolation_mod  # noqa: E402
import analyzer.run_shuffle as _shuffle_mod          # noqa: E402
import analyzer.run_static_scan as _static_mod       # noqa: E402
import analyzer.run_history as _history_mod          # noqa: E402
import analyzer.run_bisect as _bisect_mod            # noqa: E402


# ---------------------------------------------------------------------------
# Source-file mapping: pytest node ID -> source file for static_scan
# ---------------------------------------------------------------------------

_SOURCE_FILE_MAP: dict[str, str] = {
    "tests/test_a_order.py::test_cache_starts_clean":
        "demo-repo/src/shared_cache.py",
    "tests/test_b_race.py::test_background_update_completes":
        "demo-repo/src/async_worker.py",
    "tests/test_c_random.py::test_value_is_valid":
        "demo-repo/src/nondeterministic.py",
    "tests/test_d_regression.py::test_calculate_total":
        "demo-repo/src/regression.py",
}


def _resolve_source_file(test_name: str) -> str:
    """
    Return the source file path for static_scan given a pytest node ID.

    Raises ValueError for unknown targets rather than silently passing None.
    """
    try:
        return _SOURCE_FILE_MAP[test_name]
    except KeyError:
        known = "\n".join(f"  {k}" for k in sorted(_SOURCE_FILE_MAP))
        raise ValueError(
            f"[ORCHESTRATOR] unknown test target {test_name!r}. "
            f"No source-file mapping exists.\n"
            f"Known targets:\n{known}"
        )


# ---------------------------------------------------------------------------
# Callable wrappers
# The five runner modules already expose a run() function. We wrap them here
# to give each a consistent (test_name: str) -> dict signature.
# ---------------------------------------------------------------------------

def _run_isolation(test_name: str) -> dict[str, Any]:
    return _isolation_mod.run(test_name)


def _run_shuffle(test_name: str) -> dict[str, Any]:
    return _shuffle_mod.run(test_name)


def _run_static_scan(test_name: str) -> dict[str, Any]:
    source_file = _resolve_source_file(test_name)
    return _static_mod.run(source_file)


def _run_history(test_name: str) -> dict[str, Any]:
    return _history_mod.run(test_name)


def _run_bisect(test_name: str) -> dict[str, Any]:
    return _bisect_mod.run(test_name)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _log(message: str) -> None:
    """Write a status line to stderr."""
    print(f"[ORCHESTRATOR] {message}", file=sys.stderr, flush=True)


def _timed_call(
    name: str,
    fn,
    test_name: str,
) -> tuple[str, dict[str, Any] | None, float, str | None]:
    """
    Call fn(test_name), measure wall-clock time, and return
    (name, result_or_None, elapsed_seconds, error_message_or_None).
    """
    t0 = time.monotonic()
    try:
        result = fn(test_name)
        elapsed = time.monotonic() - t0
        return name, result, elapsed, None
    except Exception as exc:
        elapsed = time.monotonic() - t0
        return name, None, elapsed, str(exc)


# ---------------------------------------------------------------------------
# Public orchestration API
# ---------------------------------------------------------------------------

def run_all(test_name: str) -> dict[str, Any]:
    """
    Orchestrate all five subagents for *test_name* and return the combined
    result dict.

    Phase 1: isolation, shuffle, static_scan, history run concurrently.
    Phase 2: bisect runs after Phase 1 finishes.
    """
    if "::" not in test_name:
        raise ValueError(
            "test_name must be a full pytest node ID "
            f"(file::function), got: {test_name!r}"
        )

    # Validate static_scan mapping early so we fail fast.
    _resolve_source_file(test_name)

    results: dict[str, Any] = {}
    timings: dict[str, float] = {}
    errors: dict[str, str] = {}

    # -----------------------------------------------------------------------
    # PHASE 1: parallel
    # -----------------------------------------------------------------------

    parallel_agents = [
        ("isolation",   _run_isolation),
        ("shuffle",     _run_shuffle),
        ("static_scan", _run_static_scan),
        ("history",     _run_history),
    ]

    agent_names_str = ", ".join(n for n, _ in parallel_agents)
    _log(f"starting parallel phase: {agent_names_str}")

    parallel_t0 = time.monotonic()

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_timed_call, name, fn, test_name): name
            for name, fn in parallel_agents
        }

        for future in as_completed(futures):
            name, result, elapsed, error = future.result()
            timings[name] = round(elapsed, 3)
            if error is not None:
                errors[name] = error
                results[name] = None
                _log(f"{name} FAILED in {elapsed:.2f}s — {error}")
            else:
                results[name] = result
                _log(f"{name} completed in {elapsed:.2f}s")

    parallel_elapsed = time.monotonic() - parallel_t0
    _log(f"parallel phase completed in {parallel_elapsed:.2f}s")

    # -----------------------------------------------------------------------
    # PHASE 2: bisect (sequential, after all parallel jobs are done)
    # -----------------------------------------------------------------------

    _log("starting bisect phase")

    bisect_t0 = time.monotonic()
    _, bisect_result, bisect_elapsed, bisect_error = _timed_call(
        "bisect", _run_bisect, test_name
    )
    bisect_elapsed_actual = time.monotonic() - bisect_t0

    timings["bisect"] = round(bisect_elapsed, 3)

    if bisect_error is not None:
        errors["bisect"] = bisect_error
        results["bisect"] = None
        _log(f"bisect FAILED in {bisect_elapsed:.2f}s — {bisect_error}")
    else:
        results["bisect"] = bisect_result
        _log(f"bisect completed in {bisect_elapsed:.2f}s")

    # -----------------------------------------------------------------------
    # Overall wall-clock time
    # -----------------------------------------------------------------------

    overall_elapsed = parallel_elapsed + bisect_elapsed_actual

    _log(f"total wall-clock time: {overall_elapsed:.2f}s")

    return {
        "test_name": test_name,
        "results": results,
        "timings": timings,
        "overall_wall_clock_seconds": round(overall_elapsed, 3),
        "parallel_phase_wall_clock_seconds": round(parallel_elapsed, 3),
        "bisect_phase_wall_clock_seconds": round(bisect_elapsed_actual, 3),
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    if len(sys.argv) != 2:
        print(
            "Usage: python analyzer/run_all_subagents.py <pytest_node_id>",
            file=sys.stderr,
        )
        print(
            "Example: python analyzer/run_all_subagents.py "
            '"tests/test_a_order.py::test_cache_starts_clean"',
            file=sys.stderr,
        )
        return 2

    test_name = sys.argv[1]

    try:
        output = run_all(test_name)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
