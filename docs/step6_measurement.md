# Step 6 Measurement Procedure

**Purpose:** Record real wall-clock elapsed time for diagnosing one seeded flaky test
using two workflows: manual investigation and the FlakeFinder/Bob pipeline.

**Target test:** `tests/test_a_order.py::test_cache_starts_clean`
**Repository commit at time of measurement:** `c6565b2`
**Root cause (known ground truth):** Order-dependency — `test_shared_cache_mutator.py`
mutates module-level `shared_cache["user"]` without teardown; when it runs before
`test_a_order.py`, the assertion `assert shared_cache["user"] == "clean"` fails.

---

## Recording Rules

- Record wall-clock elapsed time in **mm:ss** format.
- Use a stopwatch or terminal timestamp. Do not estimate or round.
- Do not fabricate, average, or invent numbers.
- If the run is interrupted, discard it and re-run from the beginning.
- Record the time as observed, even if it seems long or short.

---

## Workflow 1 — Manual Investigation

### Clock starts
When you first observe a flaky failure in terminal output (i.e., the moment you decide
to investigate why `test_cache_starts_clean` is not reliably passing).

### Steps (no tooling assistance)

1. Run the test suite and observe the flaky failure:
   ```
   cd demo-repo
   python -m pytest -q
   ```

2. Run the target test in isolation several times to characterise the flakiness:
   ```
   python -m pytest tests/test_a_order.py -q
   python -m pytest tests/test_a_order.py -q
   python -m pytest tests/test_a_order.py -q
   ```

3. Inspect the test file by hand:
   ```
   cat tests/test_a_order.py
   cat src/shared_cache.py
   ```

4. Check git log for recent changes to the test file:
   ```
   git log --oneline -20 -- tests/test_a_order.py
   git log --oneline -20 -- src/shared_cache.py
   ```

5. Search the test suite for other tests that touch `shared_cache`:
   ```
   grep -r "shared_cache" tests/
   ```

6. Read the contaminating test:
   ```
   cat tests/test_shared_cache_mutator.py
   ```

7. Form a hypothesis and write it down (pen/paper or text file).

### Clock stops
When you have written down the root cause (e.g. "test_shared_cache_mutator.py mutates
shared_cache without teardown; order dependency").

---

## Workflow 2 — FlakeFinder/Bob Pipeline

### Prerequisites

- Working directory: `FlakeFinder/` (the repository root, one level above `demo-repo/`).
- Python environment with `pytest` available (`venv/` or system Python).
- `demo-repo/` submodule present and on `main` branch.

### Clock starts
Immediately before running the first command below.

### Commands (run in order — all must complete before the clock stops)

```bash
# Step 1 — Planner: Bob reasoning (or deterministic fallback)
python run_planner.py tests/test_a_order.py::test_cache_starts_clean

# Step 2 — Isolation subagent: run target test alone N times
bash agents/scripts/run_isolated.sh tests/test_a_order.py::test_cache_starts_clean 10

# Step 3 — Order-Shuffle subagent: run full suite with random order N times
bash agents/scripts/run_shuffled.sh tests/test_a_order.py::test_cache_starts_clean 10

# Step 4 — Git-Bisect subagent: identify introducing commit
bash agents/scripts/git_bisect_runner.sh tests/test_a_order.py::test_cache_starts_clean 5 2

# Step 5 — Static-Pattern subagent: AST scan for flakiness indicators
python agents/scripts/static_scan.py demo-repo/tests/test_a_order.py

# Step 6 — History subagent: search project docs for prior mentions
grep -r "test_cache_starts_clean\|shared_cache\|order" demo-repo/baseline.md demo-repo/README.md
```

### Clock stops
When all six commands above have completed and the following outputs are available:

| Output | Source |
|---|---|
| Planner JSON | `state/evidence/planner_out.json` (also printed to stdout) |
| Isolation evidence JSON | stdout of `run_isolated.sh` |
| Shuffle evidence JSON | stdout of `run_shuffled.sh` |
| Bisect evidence | stdout of `git_bisect_runner.sh` |
| Static-Pattern evidence JSON | stdout of `static_scan.py` |
| History evidence | stdout of `grep` command |

---

## Result Table

Record your measurements in `reports/step6_results.md`.

| Workflow | Elapsed Time | Root Cause Identified | Notes |
|---|---|---|---|
| Manual | _(fill in)_ | _(yes/no)_ | |
| FlakeFinder Pipeline | _(fill in)_ | _(yes/no)_ | |
