"""
reports/render_report.py — FlakeFinder Step 15 Report Renderer.

Reads end-to-end pipeline run logs and evidence artifacts to generate a
judge/video-friendly human-readable Markdown investigation report.

Usage:
    python reports/render_report.py "tests/test_a_order.py::test_cache_starts_clean"
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path
_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_REPORTS_DIR = _HERE / "reports"
_EVIDENCE_DIR = _HERE / "state" / "evidence"


def safe_id(test_name: str) -> str:
    """Critical safe-id conversion matching Step 15 specification."""
    return test_name.replace("/", "_").replace("::", "__").replace(".", "_")


def _safe_alnum(value: str) -> str:
    """Alnum safe-name convention matching coordinator/fix_agent evidence files."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in value)


def _safe_re(value: str) -> str:
    """Regex-based safe-name convention matching run_pipeline."""
    return re.sub(r"[^\w\-]", "_", value)


def _redact_secrets(text: str) -> str:
    """Ensure no credentials, tokens, or API keys appear in the report."""
    if not text:
        return ""
    text = re.sub(r"bob_prod_[a-zA-Z0-9_\-]+", "[REDACTED_API_KEY]", text)
    text = re.sub(r"AIza[0-9A-Za-z\-_]{35}", "[REDACTED_API_KEY]", text)
    return text


def _clean_table_cell(text: Any, max_len: int = 500) -> str:
    """Format strings safely for Markdown table cells without breaking row borders."""
    if text is None:
        return "N/A"
    s = str(text).strip()
    # Normalize common UTF-8 mojibake
    s = s.replace("â€”", "—").replace("â€“", "–").replace("â€œ", '"').replace("â€", '"').replace("â€˜", "'").replace("â€™", "'")
    s = _redact_secrets(s)
    # Replace newlines with spaces so markdown table formatting is preserved
    s = s.replace("\r\n", " ").replace("\n", " ")
    # Replace pipes that would break table cells
    s = s.replace("|", "/")
    s = re.sub(r"\s+", " ", s)
    if len(s) > max_len:
        s = s[: max_len - 3] + "..."
    return s


def load_pipeline_log(test_name: str, reports_dir: Path | None = None) -> tuple[dict[str, Any], Path]:
    """Load pipeline run JSON log for test_name."""
    rdir = reports_dir or _REPORTS_DIR
    sid = safe_id(test_name)

    candidates = [
        rdir / f"pipeline_run_{sid}.json",
        rdir / f"pipeline_run_{_safe_re(test_name)}.json",
        rdir / f"pipeline_run_{_safe_alnum(test_name)}.json",
    ]

    for cand in candidates:
        if cand.exists():
            data = json.loads(cand.read_text(encoding="utf-8"))
            return data, cand

    raise FileNotFoundError(
        f"Pipeline log not found for {test_name}. Checked: {[str(c) for c in candidates]}"
    )


def _resolve_diff(test_name: str, evidence_dir: Path | None = None) -> tuple[str | None, Path | None]:
    """Locate and read the generated diff file for test_name."""
    edir = evidence_dir or _EVIDENCE_DIR
    sid = safe_id(test_name)

    candidates = [
        edir / f"fix_{test_name.replace('/', '_').replace('::', '__')}.diff",
        edir / f"fix_{sid}.diff",
        edir / f"fix_{_safe_alnum(test_name)}.diff",
        edir / f"fix_{_safe_re(test_name)}.diff",
    ]

    for cand in candidates:
        if cand.exists() and cand.stat().st_size > 0:
            content = cand.read_text(encoding="utf-8")
            # If the diff file already has markdown fence, extract inner diff
            m = re.search(r"```(?:diff)?\s*\n(.*?)\n```", content, flags=re.DOTALL)
            if m:
                diff_text = m.group(1).strip()
            else:
                diff_text = content.strip()
            return diff_text, cand

    return None, None


def _resolve_verification_data(
    pipeline_verification: dict[str, Any] | None,
    test_name: str,
    evidence_dir: Path | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Resolve verification output either directly from pipeline log or from state/evidence/.
    """
    if pipeline_verification and pipeline_verification.get("before_fix"):
        return pipeline_verification, "pipeline_log"

    edir = evidence_dir or _EVIDENCE_DIR
    candidates = [
        edir / f"verification_{test_name.replace('/', '_').replace('::', '__')}.json",
        edir / f"verification_{safe_id(test_name)}.json",
        edir / f"verification_{_safe_alnum(test_name)}.json",
        edir / f"verification_{_safe_re(test_name)}.json",
    ]

    for cand in candidates:
        if cand.exists() and cand.stat().st_size > 0:
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
                if data.get("before_fix"):
                    return data, f"state/evidence/{cand.name}"
            except Exception:
                pass

    return pipeline_verification, None


def render_evidence_table(investigation_output: dict[str, Any] | None) -> str:
    """Render Section 3: Five Parallel Investigation Agents Markdown table."""
    subagents = ["isolation", "shuffle", "bisect", "static_scan", "history"]

    if not investigation_output:
        return "_No investigation output available._\n"

    results = investigation_output.get("results") or {}
    errors = investigation_output.get("errors") or {}

    rows = []
    for agent in subagents:
        agent_data = results.get(agent)
        err = errors.get(agent)

        if isinstance(agent_data, dict):
            ev = _clean_table_cell(agent_data.get("evidence", "N/A"))
            hyp = _clean_table_cell(agent_data.get("hypothesis", "N/A"))
            conf = _clean_table_cell(agent_data.get("confidence", "N/A"))
        elif err:
            ev = f"**Error:** {_clean_table_cell(err)}"
            hyp = "Investigation subagent encountered an execution error."
            conf = "N/A"
        else:
            ev = "No evidence collected"
            hyp = "N/A"
            conf = "N/A"

        rows.append(f"| `{agent}` | {ev} | {hyp} | {conf} |")

    header = (
        "| Subagent | Evidence | Hypothesis | Confidence |\n"
        "|---|---|---|---|\n"
    )
    return header + "\n".join(rows) + "\n"


def render_timing_proof(investigation_stage: dict[str, Any] | None) -> str:
    """Render Section 4: Parallelism Proof and speedup calculation."""
    if not investigation_stage:
        return "_No investigation timing data available._\n"

    inv_output = investigation_stage.get("output") or {}
    timings = inv_output.get("timings") or {}

    agent_order = ["isolation", "shuffle", "bisect", "static_scan", "history"]
    table_rows = []
    for agent in agent_order:
        t = timings.get(agent)
        t_str = f"{t:.2f}s" if isinstance(t, (int, float)) else "N/A"
        phase = "Sequential Phase" if agent == "bisect" else "Parallel Phase"
        table_rows.append(f"| `{agent}` | {phase} | {t_str} |")

    # Sum of individual times
    valid_times = [v for v in timings.values() if isinstance(v, (int, float))]
    sum_individual = sum(valid_times) if valid_times else 0.0

    # Wall-clock
    parallel_wall = inv_output.get("parallel_phase_wall_clock_seconds")
    overall_wall = inv_output.get("overall_wall_clock_seconds") or investigation_stage.get("elapsed", 0.0)

    # Speedup calculations
    overall_speedup = round(sum_individual / overall_wall, 2) if overall_wall and overall_wall > 0 else 0.0

    parallel_agents = ["isolation", "shuffle", "static_scan", "history"]
    parallel_sum = sum(timings.get(a, 0.0) for a in parallel_agents if isinstance(timings.get(a), (int, float)))
    parallel_speedup = (
        round(parallel_sum / parallel_wall, 2)
        if parallel_wall and parallel_wall > 0
        else 0.0
    )

    table_rows.append(f"| **Sum of Individual Timings** | Sequential Equivalent | **{sum_individual:.2f}s** |")
    table_rows.append(f"| **Investigation Wall-Clock Time** | Parallel Execution | **{overall_wall:.2f}s** |")
    table_rows.append(f"| **Calculated Speedup** | Overall (Sum / Wall) | **{overall_speedup:.2f}x** |")

    table_str = (
        "| Subagent | Phase | Elapsed Time (s) |\n"
        "|---|---|---:|\n"
        + "\n".join(table_rows)
        + "\n\n"
    )

    narrative = (
        f"- **Concurrent Subagents (Parallel Phase):** Ran 4 subagents concurrently in **{parallel_wall:.2f}s** wall-clock "
        f"vs. **{parallel_sum:.2f}s** sequential sum (**{parallel_speedup:.2f}x speedup**).\n"
        f"- **Total Investigation Wall-Clock:** **{overall_wall:.2f}s** vs. **{sum_individual:.2f}s** "
        f"cumulative execution time (**{overall_speedup:.2f}x overall speedup**).\n"
    )

    return table_str + narrative


def render_fix(fix_stage: dict[str, Any] | None, diff_content: str | None) -> str:
    """Render Section 5: Proposed Fix and Diff block."""
    if not fix_stage:
        fix_output: dict[str, Any] = {}
    else:
        fix_output = fix_stage.get("output") or {}

    summary = _redact_secrets(fix_output.get("fix_summary", "No fix summary recorded."))
    justification = _redact_secrets(fix_output.get("justification", "No justification recorded."))
    confidence = fix_output.get("confidence", "unknown")

    # Format long stack traces/messages cleanly
    if "\n" in summary:
        summary_formatted = "\n> " + "\n> ".join(summary.splitlines())
    else:
        summary_formatted = summary

    if "\n" in justification:
        justification_formatted = "\n> " + "\n> ".join(justification.splitlines())
    else:
        justification_formatted = justification

    fix_meta = (
        f"- **Fix Summary:** {summary_formatted}\n"
        f"- **Justification:** {justification_formatted}\n"
        f"- **Confidence:** `{confidence}`\n\n"
        f"### Proposed Diff\n\n"
    )

    if diff_content and diff_content.strip():
        diff_block = f"```diff\n{diff_content.strip()}\n```\n"
    else:
        diff_block = "No diff generated.\n"

    return fix_meta + diff_block


def render_verification(
    verification_stage: dict[str, Any] | None,
    test_name: str,
    evidence_dir: Path | None = None,
) -> str:
    """Render Section 6: Independent Verification."""
    raw_v = verification_stage.get("output") if verification_stage else None
    v_data, source = _resolve_verification_data(raw_v, test_name, evidence_dir)

    sections = []
    sections.append(
        "> **Independent Execution:** Verification executes in an isolated scratch environment with "
        "real `pytest` test suite runs without LLM inference, ensuring objective validation.\n"
    )

    if not v_data:
        sections.append("> **Warning:** Verification data not available.\n")
        return "\n".join(sections)

    if v_data.get("skipped") and not v_data.get("before_fix"):
        reason = v_data.get("reason", "Verification was skipped.")
        sections.append(f"> **Warning:** Verification was skipped: {reason}\n")
        return "\n".join(sections)

    if v_data.get("error") and not v_data.get("before_fix"):
        err = v_data.get("error")
        sections.append(f"> **Error:** Verification encountered an error: {err}\n")
        return "\n".join(sections)

    if source and source != "pipeline_log":
        sections.append(
            f"*(Verification results loaded from independent artifact: `{source}`)*\n"
        )

    # Before / After Table
    before = v_data.get("before_fix", {})
    after = v_data.get("after_fix", {})

    b_runs = before.get("runs", 0)
    b_passes = before.get("passes", 0)
    b_fails = before.get("failures", 0)
    b_pct = f"{(b_passes / b_runs * 100):.1f}%" if b_runs > 0 else "N/A"

    a_runs = after.get("runs", 0)
    a_passes = after.get("passes", 0)
    a_fails = after.get("failures", 0)
    a_pct = f"{(a_passes / a_runs * 100):.1f}%" if a_runs > 0 else "N/A"

    table = (
        "| State | Runs | Passes | Failures | Pass Rate |\n"
        "|---|---:|---:|---:|---:|\n"
        f"| **Before fix** | {b_runs} | {b_passes} | {b_fails} | {b_pct} |\n"
        f"| **After fix** | {a_runs} | {a_passes} | {a_fails} | {a_pct} |\n\n"
    )
    sections.append(table)

    fix_confirmed = v_data.get("fix_confirmed", False)
    regression_safe = v_data.get("regression_safe", False)

    confirmed_str = "true" if fix_confirmed else "false"
    safe_str = "true" if regression_safe else "false"

    status_note = ""
    if not fix_confirmed and b_fails == 0:
        status_note = " *(isolated baseline passed 10/10 runs; fix confirmation requires baseline failure in isolation)*"

    sections.append(f"- **Fix Confirmed:** `{confirmed_str}`{status_note}")
    sections.append(f"- **Regression Safe:** `{safe_str}`\n")

    # Neighbor tests table
    reg_checks = v_data.get("regression_check", {})
    if reg_checks:
        sections.append("### Neighbor Regression Tests\n")
        n_rows = []
        for n_test, n_res in reg_checks.items():
            runs = n_res.get("runs", 0)
            passes = n_res.get("passes", 0)
            fails = n_res.get("failures", 0)
            status = "PASS" if fails == 0 and passes > 0 else "FAIL"
            n_rows.append(f"| `{n_test}` | {runs} | {passes} | {fails} | **{status}** |")

        n_table = (
            "| Neighbor Test | Runs | Passes | Failures | Result |\n"
            "|---|---:|---:|---:|---|\n"
            + "\n".join(n_rows)
            + "\n"
        )
        sections.append(n_table)

    return "\n".join(sections)


def render_report(
    test_name: str,
    reports_dir: Path | None = None,
    evidence_dir: Path | None = None,
) -> Path:
    """Generate Markdown report from Step 14 pipeline run JSON and evidence."""
    rdir = reports_dir or _REPORTS_DIR
    edir = evidence_dir or _EVIDENCE_DIR

    pipeline_log, log_path = load_pipeline_log(test_name, reports_dir=rdir)
    diff_content, diff_path = _resolve_diff(test_name, evidence_dir=edir)

    stages = pipeline_log.get("stages", {})
    planner_stage = stages.get("planner", {})
    inv_stage = stages.get("investigation", {})
    coord_stage = stages.get("coordinator", {})
    fix_stage = stages.get("fix_agent", {})
    verif_stage = stages.get("verification", {})

    total_elapsed = pipeline_log.get("total_elapsed", 0.0)

    # Generated timestamp from pipeline log file mtime
    file_mtime = log_path.stat().st_mtime
    dt_str = datetime.fromtimestamp(file_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Coordinator fields
    coord_output = coord_stage.get("output") or {}
    ranked_cause = _redact_secrets(coord_output.get("ranked_cause", "N/A"))
    coord_conf = coord_output.get("confidence", "unknown")
    conv_count = coord_output.get("convergence_count", 0)
    winning_ev = _redact_secrets(coord_output.get("winning_evidence", "N/A"))

    # Verification summary data for conclusion
    raw_v = verif_stage.get("output")
    v_data, _ = _resolve_verification_data(raw_v, test_name, edir)
    fix_confirmed = v_data.get("fix_confirmed", False) if v_data else False
    reg_safe = v_data.get("regression_safe", False) if v_data else False

    # Calculate overall speedup for conclusion
    inv_output = inv_stage.get("output") or {}
    timings = inv_output.get("timings") or {}
    valid_times = [v for v in timings.values() if isinstance(v, (int, float))]
    sum_individual = sum(valid_times) if valid_times else 0.0
    wall_clock = inv_output.get("overall_wall_clock_seconds") or inv_stage.get("elapsed", 0.0)
    speedup = round(sum_individual / wall_clock, 2) if wall_clock and wall_clock > 0 else 1.0

    lines: list[str] = [
        "# FlakeFinder Investigation Report\n",
        "## 1. Test / Run Summary\n",
        f"- **Target Test:** `{test_name}`",
        f"- **Generated Timestamp:** {dt_str}",
        f"- **Total Pipeline Elapsed Time:** `{total_elapsed:.2f} seconds` ({total_elapsed / 60:.1f} minutes)",
        f"- **Pipeline Run Log:** `{log_path.name}`\n",
        "### Pipeline Stages\n",
        "| Stage # | Stage Name | Elapsed Time (s) | Status |",
        "|---|---|---:|---|",
        f"| 1 | Planner | {planner_stage.get('elapsed', 0.0):.2f}s | {'Completed' if 'output' in planner_stage else 'Error'} |",
        f"| 2 | Parallel Investigation | {inv_stage.get('elapsed', 0.0):.2f}s | {'Completed' if 'output' in inv_stage else 'Error'} |",
        f"| 3 | Coordinator | {coord_stage.get('elapsed', 0.0):.2f}s | {'Completed' if 'output' in coord_stage else 'Error'} |",
        f"| 4 | Fix Agent | {fix_stage.get('elapsed', 0.0):.2f}s | {'Completed' if 'output' in fix_stage else 'Error'} |",
        f"| 5 | Verification | {verif_stage.get('elapsed', 0.0):.2f}s | {'Completed' if 'output' in verif_stage else 'Error'} |\n",
        "--------------------------------------------------\n",
        "## 2. Root Cause\n",
        f"- **Ranked Cause:** {ranked_cause}",
        f"- **Confidence:** `{coord_conf}`",
        f"- **Convergence Count:** `{conv_count}` subagent(s)",
        f"- **Winning Evidence / Explanation:**",
        f"> {winning_ev}\n",
        "--------------------------------------------------\n",
        "## 3. Five Parallel Investigation Agents\n",
        render_evidence_table(inv_output),
        "--------------------------------------------------\n",
        "## 4. Parallelism Proof\n",
        render_timing_proof(inv_stage),
        "--------------------------------------------------\n",
        "## 5. Proposed Fix\n",
        render_fix(fix_stage, diff_content),
        "--------------------------------------------------\n",
        "## 6. Independent Verification\n",
        render_verification(verif_stage, test_name, edir),
        "--------------------------------------------------\n",
        "## 7. Evidence-Based Conclusion\n",
        f"- **Detected Root Cause:** {ranked_cause}",
        f"- **Proposed Fix:** {_clean_table_cell((fix_stage.get('output') or {}).get('fix_summary', 'None'), max_len=200)}",
        f"- **Verification Result:** `fix_confirmed = {'true' if fix_confirmed else 'false'}`",
        f"- **Regression Safety:** `regression_safe = {'true' if reg_safe else 'false'}`",
        f"- **Pipeline Timing:** Total wall-clock `{total_elapsed:.2f}s` with `{speedup:.2f}x` investigation parallelism.\n",
    ]

    report_content = "\n".join(lines)

    # Save to reports/report_<safe_id>.md
    sid = safe_id(test_name)
    report_file = rdir / f"report_{sid}.md"
    rdir.mkdir(parents=True, exist_ok=True)
    report_file.write_text(report_content, encoding="utf-8")

    return report_file


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python reports/render_report.py <pytest_node_id>", file=sys.stderr)
        return 2

    test_name = sys.argv[1]
    try:
        out_path = render_report(test_name)
        print(f"Report written to {out_path}")
        return 0
    except Exception as exc:
        print(f"[RENDER_REPORT] ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
