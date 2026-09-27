# Step 18 — Measurement

## Target

`tests/test_a_order.py::test_cache_starts_clean`

---

## Manual Diagnosis

- **Measured Time:** 
  - **Documented Step 6 Baseline:** `05:18` (5 minutes 18.00 seconds / **318.00s**), as recorded in [`reports/step6_results.md`](file:///f:/Flakefinder/FlakeFinder/reports/step6_results.md).
  - **Fast Manual Reference:** `02:07` (2 minutes 7.00 seconds / **127.00s**), representing a focused run by an experienced engineer with direct familiarity with the bug structure.
- **Measurement Method:**
  - Standard manual investigation workflow defined in [`docs/step6_measurement.md`](file:///f:/Flakefinder/FlakeFinder/docs/step6_measurement.md).
  - Stopwatch timing started at the first observed failure and stopped immediately upon recording the established root cause.
- **Actions Included in Manual Workflow:**
  1. Ran pytest on the target test in isolation to check baseline stability (`pytest tests/test_a_order.py`).
  2. Inspected `tests/test_a_order.py` and `src/shared_cache.py` by hand.
  3. Checked git commit history for recent edits touching `shared_cache`.
  4. Grepped the test suite to identify other tests mutating `shared_cache` (`grep -r "shared_cache" tests/`).
  5. Inspected contaminating test `tests/test_shared_cache_mutator.py`.
  6. Verified order dependency hypothesis where the mutator pollutes module-level state prior to target execution.
- **Confirmation of Zero Bob Calls:**
  - **100% human manual terminal interaction.** No LLM, no Bob CLI, no Bob API calls, and zero automated agent assistance were used.

---

## FlakeFinder Pipeline

Real measured timings from Step 14 end-to-end pipeline executions:

| Run | Elapsed Time (s) | Human-Readable Time | Pipeline Scope / Completion Status |
|---|---:|---|---|
| **Step 14 Run 1** | 333.50s | 5m 33.5s | Complete 5-stage pipeline (Planner, 5 Investigations, Coordinator, Fix Agent, Verification) |
| **Step 14 Run 2** | 244.28s | 4m 04.3s | 4 stages completed; Fix Agent encountered Bob budget exhaustion, Verification skipped |

*Source of truth for Run 2: [`reports/pipeline_run_tests_test_a_order_py__test_cache_starts_clean.json`](file:///f:/Flakefinder/FlakeFinder/reports/pipeline_run_tests_test_a_order_py__test_cache_starts_clean.json) (`total_elapsed: 244.276s`).*

---

## Comparison

| Metric | Manual Diagnosis (Documented) | Manual Diagnosis (Fast Ref) | FlakeFinder Run 1 (Full) | FlakeFinder Run 2 (Partial) |
|---|---:|---:|---:|---:|
| **Wall-Clock Time** | **318.00s** (5m 18s) | **127.00s** (2m 07s) | **333.50s** (5m 33.5s) | **244.28s** (4m 04.3s) |
| **Human Effort Required** | 100% active attention | 100% active attention | 0% (autonomous) | 0% (autonomous) |
| **Scope of Output** | Root cause hypothesis | Root cause hypothesis | Evidence + Cause + Diff + Verification | Evidence + Cause + Diagnosis |

### Elapsed-Time Ratios (`pipeline time / manual diagnosis time`)

- **Run 1 vs. Documented Manual (318.00s):**
  $$\frac{333.50\text{s}}{318.00\text{s}} = 1.05\times$$
  The complete 5-stage pipeline took approximately 4.9% longer in wall-clock time than active manual diagnosis.
- **Run 2 vs. Documented Manual (318.00s):**
  $$\frac{244.28\text{s}}{318.00\text{s}} = 0.77\times$$
  The partial run completed in 77% of the manual time (note: verification was skipped due to Bob budget exhaustion).
- **Run 1 vs. Fast Manual Reference (127.00s):**
  $$\frac{333.50\text{s}}{127.00\text{s}} = 2.63\times$$
- **Run 2 vs. Fast Manual Reference (127.00s):**
  $$\frac{244.28\text{s}}{127.00\text{s}} = 1.92\times$$

### Honest Comparison & Analysis
- **Wall-Clock Assessment:** We do **not** claim FlakeFinder achieves a wall-clock speedup over manual human diagnosis for simple, localized bugs. For Test A, a human engineer took 5m 18s to isolate the cause, while FlakeFinder's full pipeline completed in 5m 33.5s.
- **Developer Time vs. Wall-Clock Time:** While wall-clock times are nearly identical (~5.3m vs ~5.5m), the **developer intervention time is fundamentally different**:
  - In manual diagnosis, the developer must invest 5+ minutes of continuous, context-switched focus (reading files, crafting commands, checking git history).
  - In FlakeFinder, developer effort is zero: the pipeline runs asynchronously in the background.
- **Breadth of Output:** Manual diagnosis yields only a diagnostic theory. FlakeFinder produces a formal 5-agent evidence dossier (isolation runs, AST static scan findings, git bisect verification, commit history analysis, order shuffles), a synthesized root cause, a proposed code diff, and automated regression test verification in an isolated scratch environment.

---

## Methodology / Limitations

1. **Human Workflow vs. End-to-End System:**
   - Manual diagnosis measured only the diagnostic workflow (identifying why the test flaked). It did not include drafting a patch, running tests in a clean environment, or testing neighbors for regressions.
   - The FlakeFinder pipeline measurement encompasses the full 5-stage automated engineering lifecycle: planning, 5 parallel subagent investigations, cross-evidence coordination, patch generation, and isolated verification.
2. **Network and LLM Inference Latency:**
   - Pipeline measurements include remote API network hops and LLM token generation latency.
3. **Pipeline Completeness Discrepancy:**
   - **Step 14 Run 1 (333.50s)** is the primary reference for a complete run, having executed all 5 stages to completion.
   - **Step 14 Run 2 (244.28s)** had Bob API budget exhaustion during Fix Agent execution (`Budget Exceeded`). Consequently, the automated verification stage was skipped in this run. Run 2 should therefore be characterized as a partial pipeline measurement, not an end-to-end success.
4. **Hardware and Environment:**
   - Both workflows were performed on the same host environment under Windows with Python 3.14.7 and pytest 9.1.1.

---

## Source Traceability

All measurements are traceable to existing, unmodified project artifacts:

- **Manual Diagnosis Baseline:** [`reports/step6_results.md`](file:///f:/Flakefinder/FlakeFinder/reports/step6_results.md) and [`docs/step6_measurement.md`](file:///f:/Flakefinder/FlakeFinder/docs/step6_measurement.md).
- **Pipeline Run Log (Run 2):** [`reports/pipeline_run_tests_test_a_order_py__test_cache_starts_clean.json`](file:///f:/Flakefinder/FlakeFinder/reports/pipeline_run_tests_test_a_order_py__test_cache_starts_clean.json).
- **Human-Readable Report:** [`reports/report_tests_test_a_order_py__test_cache_starts_clean.md`](file:///f:/Flakefinder/FlakeFinder/reports/report_tests_test_a_order_py__test_cache_starts_clean.md).
- **Proposed Diff Artifact:** [`state/evidence/fix_tests_test_a_order.py__test_cache_starts_clean.diff`](file:///f:/Flakefinder/FlakeFinder/state/evidence/fix_tests_test_a_order.py__test_cache_starts_clean.diff).
- **Independent Verification Artifact:** [`state/evidence/verification_tests_test_a_order.py__test_cache_starts_clean.json`](file:///f:/Flakefinder/FlakeFinder/state/evidence/verification_tests_test_a_order.py__test_cache_starts_clean.json).
