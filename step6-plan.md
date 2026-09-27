# Step 6 — Measurement Plan

## Overview

Step 6 introduces `run_planner.py`, the missing Planner entry point, and measures the
time saved when using the FlakeFinder/Bob pipeline versus the manual workflow.

**Target test:** `tests/test_a_order.py::test_cache_starts_clean`
Order-dependency flakiness; 55% pass rate; known ground truth. Chosen for reproducibility.

**Scope boundary:**
- Does NOT implement Step 7.
- Does NOT modify any existing file.
- Does NOT change the five subagent scripts.
- Does NOT modify `bob_prompts/planner_prompt.md`.

---

## Architecture

```
run_planner.py  receives a pytest node ID
      |
      v
  bob on PATH?  ─── yes ──> invoke bob subprocess with planner_prompt.md + test context
      |                           |
      |              valid JSON?  ─── yes ──> parse + validate
      |                           |
      |                          no ──> log failure to stderr, activate fallback
      |
     no ──> deterministic Python fallback
                    |
                    v
          parse + validate Planner JSON
                    |
                    v
         emit JSON to stdout + state/evidence/planner_out.json
```

**Planner JSON schema** (preserved exactly from `bob_prompts/planner_prompt.md`):

```json
{
  "test_name": "tests/test_a_order.py::test_cache_starts_clean",
  "test_file": "tests/test_a_order.py",
  "hypotheses": ["order_dependency"],
  "search_targets": ["tests/test_a_order.py", "src/shared_cache.py"]
}
```

---

## Files to Create

| File | Purpose |
|---|---|
| `run_planner.py` | Planner entry point — Bob invocation, fallback, JSON validation, output |
| `tests/test_run_planner.py` | Pytest tests for run_planner.py |
| `docs/step6_measurement.md` | Exact procedure for the manual vs. pipeline timing measurement |
| `reports/step6_results.md` | Blank record table to be filled in with real measured times |

## Files That Will NOT Be Modified

- `bob_prompts/planner_prompt.md`
- All other `bob_prompts/*.md`
- `agents/scripts/run_isolated.sh`
- `agents/scripts/run_shuffled.sh`
- `agents/scripts/git_bisect_runner.sh`
- `agents/scripts/static_scan.py`
- All `demo-repo/**`
- `requirements.txt`
- `README.md`

---

## Sub-Task 1 — Create `run_planner.py`

**Status:** [x] done

**Intent:**
Introduce the Planner entry point that was specified in the architecture but has never
existed in this repository. It receives a pytest node ID, attempts to use Bob Shell for
reasoning, falls back to deterministic Python when Bob is unavailable or fails, validates
the output against the Planner JSON schema, and emits the result.

**Expected Outcomes:**
- `python run_planner.py tests/test_a_order.py::test_cache_starts_clean` runs without error.
- Stdout is valid JSON with keys: `test_name`, `test_file`, `hypotheses`, `search_targets`.
- `test_name` in output equals the input node ID verbatim.
- `state/evidence/planner_out.json` is written.
- When Bob is absent: deterministic fallback runs silently.
- When Bob is present but fails: stderr contains an explicit failure message; fallback activates.
- The output JSON contains no subagent invocation paths — it is data only.

**Todo List:**

1. Accept a single CLI argument: the full pytest node ID (`file::function` form).
2. Derive `test_file` and function name from the node ID by splitting on `::`.
3. **Bob invocation path:**
   a. Use `shutil.which("bob")` to check availability.
   b. If found, build the prompt by reading `bob_prompts/planner_prompt.md` and appending
      the test file content and last 20 lines of `git log -- <test_file>` from `demo-repo/`.
   c. Call `bob` as a subprocess with the prompt on stdin.
   d. Attempt to parse stdout as JSON and validate the schema.
   e. On any failure (non-zero exit, non-JSON, missing keys), write the failure reason to
      stderr with a clear prefix (`BOB FAILED:`) and proceed to fallback.
4. **Deterministic Python fallback:**
   a. Read the test file from `demo-repo/<test_file>`.
   b. Use `ast.parse` to detect: `import random` → `non_determinism`,
      `asyncio`/`create_task` → `race_condition`, `shared_cache` import → `order_dependency`.
   c. Read git log of that file and check for commit messages mentioning regression keywords → `regression`.
   d. `search_targets` = [test_file] + any `src/*.py` imported by the test file.
   e. Assemble the Planner JSON dict with all four required keys.
5. **Schema validation:** check that output dict has exactly `test_name`, `test_file`,
   `hypotheses` (non-empty list), `search_targets` (non-empty list); raise `ValueError` on failure.
6. Print validated JSON to stdout (compact, no markdown fences).
7. Write the same JSON to `state/evidence/planner_out.json`.
8. Exit 0 on success, 1 on unrecoverable error.

**Relevant Context:**
- `bob_prompts/planner_prompt.md` — the system prompt to send to Bob; must not be modified.
- `demo-repo/` — where test files and git history live.
- `state/evidence/` — output directory (already exists as empty dir).
- No new dependencies — use only Python stdlib: `subprocess`, `shutil`, `json`, `pathlib`, `ast`, `sys`.

---

## Sub-Task 2 — Create `tests/test_run_planner.py`

**Status:** [x] done

**Intent:**
Provide a pytest test suite that verifies every contract of `run_planner.py` without
depending on Bob being available. All tests must pass in CI (or on this laptop) where
`bob` is not on PATH.

**Expected Outcomes:**
- All tests pass with `pytest tests/test_run_planner.py` from the `FlakeFinder/` directory.
- Tests exercise both the fallback path and schema contracts.
- Tests confirm full node ID is preserved (not truncated to function name only).
- Tests confirm hypotheses do not appear to control subagent execution.

**Todo List:**

1. Write `test_full_node_id_preserved`: call `run_planner` with a known node ID and assert
   the output `test_name` equals the input exactly (e.g. `tests/test_a_order.py::test_cache_starts_clean`).
2. Write `test_output_schema_valid`: assert output JSON has exactly the four required keys.
3. Write `test_hypotheses_not_empty`: assert `hypotheses` list has at least one entry.
4. Write `test_search_targets_not_empty`: assert `search_targets` list has at least one entry.
5. Write `test_fallback_runs_when_bob_absent`: monkeypatch `shutil.which` to return `None`;
   assert the function runs and returns valid JSON without raising.
6. Write `test_bob_failure_logs_to_stderr`: monkeypatch `shutil.which` to return a fake path
   and the subprocess call to return a non-zero exit code; assert stderr contains `BOB FAILED:`.
7. Write `test_hypotheses_do_not_control_subagent_execution`: assert that the string values
   in `hypotheses` are plain hypothesis labels (no file paths, no `.sh`/`.py` subagent paths).
8. Each test imports `run_planner` as a module (not via subprocess) to enable patching.
   Expose a `run(node_id: str) -> dict` function from `run_planner.py` to make this clean.

**Relevant Context:**
- Tests live at `FlakeFinder/tests/test_run_planner.py` (new directory under FlakeFinder root).
- `run_planner.py` lives at `FlakeFinder/run_planner.py`.
- `demo-repo/tests/test_a_order.py` is the canonical test file for the target node ID.

---

## Sub-Task 3 — Create `docs/step6_measurement.md`

**Status:** [x] done

**Intent:**
Document the exact, repeatable procedure for performing the manual-vs-pipeline timing
comparison. The procedure defines what "start" and "stop" mean for each workflow so
elapsed times are comparable and honest.

**Expected Outcomes:**
- Anyone can follow the procedure and record a real, comparable timing.
- The procedure specifies the exact commands for the pipeline workflow.
- The procedure specifies the exact manual steps (no tooling assistance).
- No numbers are fabricated — the document contains only the procedure, not results.

**Todo List:**

1. State the measurement target: `tests/test_a_order.py::test_cache_starts_clean`, commit `c6565b2`.
2. Define **Manual workflow**:
   - Clock starts: when the tester first observes a flaky failure in CI/terminal output.
   - Steps: read the failing test, run pytest manually several times, inspect git log by hand,
     read source files by hand, reason about cause.
   - Clock stops: when the tester writes down the root cause on paper/screen.
3. Define **FlakeFinder/Bob pipeline workflow**:
   - Clock starts: when the tester runs `python run_planner.py tests/test_a_order.py::test_cache_starts_clean`.
   - Steps (exact commands, in order):
     ```
     # Step 1 — Planner
     python run_planner.py tests/test_a_order.py::test_cache_starts_clean

     # Step 2 — Isolation subagent
     bash agents/scripts/run_isolated.sh tests/test_a_order.py::test_cache_starts_clean 10

     # Step 3 — Order-Shuffle subagent
     bash agents/scripts/run_shuffled.sh tests/test_a_order.py::test_cache_starts_clean 10

     # Step 4 — Git-Bisect subagent
     bash agents/scripts/git_bisect_runner.sh tests/test_a_order.py::test_cache_starts_clean 5 2

     # Step 5 — Static-Pattern subagent
     python agents/scripts/static_scan.py demo-repo/tests/test_a_order.py

     # Step 6 — History subagent (manual: search demo-repo docs and README for prior mentions)
     grep -r "test_cache_starts_clean\|shared_cache\|order" demo-repo/baseline.md demo-repo/README.md
     ```
   - All six commands must complete before the clock stops.
   - Clock stops: when Planner JSON + all five subagent evidence outputs are in hand
     (stdout captured or saved to files for review).
4. Add a "Recording Rules" section:
   - Record wall-clock elapsed time in mm:ss format.
   - Do not round to the nearest minute.
   - Do not fabricate, estimate, or average. Record the real measurement.
   - Re-run if interrupted.
5. Add blank result table (values left empty — to be filled in `reports/step6_results.md`).

**Relevant Context:**
- This file is reference documentation; it does not run any code.

---

## Sub-Task 4 — Create `reports/step6_results.md`

**Status:** [x] done

**Intent:**
Provide a structured record where real measured elapsed times are recorded after the
procedure in `docs/step6_measurement.md` has been performed. This file starts as a
template and is filled in by the human performing the measurement.

**Expected Outcomes:**
- File exists with clearly labeled blank fields.
- No times are pre-filled or invented.
- File structure matches the measurement procedure in `docs/step6_measurement.md`.

**Todo List:**

1. Write a header: "Step 6 Measurement Results".
2. Record: test diagnosed, commit hash at time of measurement.
3. Provide a table with columns: Workflow | Elapsed Time | Root Cause Identified | Notes
4. Two rows: Manual and FlakeFinder Pipeline — times left blank.
5. Add a "Conclusion" section stub: "Time saved: [to be calculated]" and
   "Pipeline identified correct root cause: [yes/no]".

**Relevant Context:**
- Must not contain invented numbers. All measurement fields are empty until filled.
- Commit at time of measurement is `c6565b2`.
