"""
tests/test_fix_agent.py — Unit tests for analyzer/fix_agent.py (Step 12).

Tests cover at minimum:
 1. Coordinator evidence loading
 2. Prompt construction
 3. Correct test-name substitution
 4. JSON extraction
 5. Unified diff extraction
 6. Markdown-fenced diff handling
 7. Valid schema acceptance
 8. Missing-field rejection
 9. Wrong test-name rejection
10. Invalid confidence rejection
11. Wrong subagent rejection
12. Missing diff rejection
13. Malformed JSON rejection
14. Bob failure handling
15. Fallback does not fabricate a diff
16. Forbidden verification claim rejection (mandatory)
17. Evidence persistence
18. Test A handling
19. Test D handling
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import analyzer.fix_agent as fix_agent  # noqa: E402

_TEST_A = "tests/test_a_order.py::test_cache_starts_clean"
_TEST_D = "tests/test_d_regression.py::test_calculate_total"

_SAMPLE_DIFF = """--- a/src/shared_cache.py
+++ b/src/shared_cache.py
@@ -12,3 +12,6 @@
 def reset_cache():
     shared_cache["user"] = "clean"
+
+def ensure_clean():
+    reset_cache()
"""

_SAMPLE_TEST_D_DIFF = """--- a/src/regression.py
+++ b/src/regression.py
@@ -5,14 +5,5 @@
 def calculate_total(items):
     total = sum(items)
-    # REGRESSION:
-    # Random behavior was accidentally introduced here.
-    seed = os.getenv("FLAKE_DEMO_SEED")
-    if seed is not None:
-        rng = random.Random(int(seed))
-        if rng.random() < 0.5:
-            total += 1
-    else:
-        if random.random() < 0.5:
-            total += 1
     return total
"""


def _sample_valid_json(test_name: str = _TEST_A, **overrides) -> dict[str, Any]:
    base: dict[str, Any] = {
        "subagent": "fix_agent",
        "test_name": test_name,
        "fix_summary": "Add cache reset fixture to isolate shared mutable state across test runs.",
        "justification": "Addresses the order-dependency identified in static scan and history evidence.",
        "confidence": "high",
    }
    base.update(overrides)
    return base


# ===========================================================================
# 1. Coordinator evidence loading
# ===========================================================================

class TestCoordinatorEvidenceLoading:
    def test_load_coordinator_evidence_success(self, tmp_path: Path):
        coord_file = tmp_path / f"coordinator_{fix_agent._safe_alnum(_TEST_A)}.json"
        payload = {"test_name": _TEST_A, "ranked_cause": "order_dependency"}
        coord_file.write_text(json.dumps(payload), encoding="utf-8")

        loaded = fix_agent.load_coordinator_evidence(_TEST_A, evidence_dir=tmp_path)
        assert loaded == payload

    def test_load_coordinator_evidence_regex_fallback_name(self, tmp_path: Path):
        coord_file = tmp_path / f"coordinator_{fix_agent._safe_re(_TEST_A)}.json"
        payload = {"test_name": _TEST_A, "ranked_cause": "order_dependency"}
        coord_file.write_text(json.dumps(payload), encoding="utf-8")

        loaded = fix_agent.load_coordinator_evidence(_TEST_A, evidence_dir=tmp_path)
        assert loaded == payload

    def test_load_coordinator_evidence_missing_raises_file_not_found(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError) as exc_info:
            fix_agent.load_coordinator_evidence("tests/non_existent.py::test_missing", evidence_dir=tmp_path)
        assert "Step 11 input is missing" in str(exc_info.value)

    def test_load_coordinator_evidence_corrupt_json_raises_value_error(self, tmp_path: Path):
        coord_file = tmp_path / f"coordinator_{fix_agent._safe_alnum(_TEST_A)}.json"
        coord_file.write_text("{ corrupt json ...", encoding="utf-8")

        with pytest.raises(ValueError) as exc_info:
            fix_agent.load_coordinator_evidence(_TEST_A, evidence_dir=tmp_path)
        assert "Corrupted Coordinator JSON" in str(exc_info.value)


# ===========================================================================
# 2. Prompt construction & 3. Correct test-name substitution
# ===========================================================================

class TestPromptConstruction:
    def test_prompt_construction(self, tmp_path: Path):
        coord_data = {"test_name": _TEST_A, "ranked_cause": "shared_mutable_state"}
        prompt = fix_agent.build_fix_prompt(_TEST_A, coord_data)

        assert "You are the Fix agent for FlakeFinder." in prompt
        assert "COORDINATOR OUTPUT:" in prompt
        assert '"ranked_cause": "shared_mutable_state"' in prompt
        assert "Rules:" in prompt

    def test_correct_test_name_substitution(self):
        coord_data = {"test_name": _TEST_D, "ranked_cause": "git_regression"}
        prompt = fix_agent.build_fix_prompt(_TEST_D, coord_data)

        assert "{test_name}" not in prompt
        assert _TEST_D in prompt


# ===========================================================================
# 4. JSON extraction & 5. Unified diff extraction
# ===========================================================================

class TestResponseParsing:
    def test_plain_diff_and_json_extraction(self):
        raw = f"{_SAMPLE_DIFF}\n\n{json.dumps(_sample_valid_json(_TEST_A), indent=2)}"
        diff, metadata = fix_agent.parse_fix_response(raw, _TEST_A)

        assert "--- a/src/shared_cache.py" in diff
        assert "+++ b/src/shared_cache.py" in diff
        assert metadata["subagent"] == "fix_agent"
        assert metadata["test_name"] == _TEST_A
        assert metadata["confidence"] == "high"

    def test_embedded_json_in_conversational_text(self):
        raw = f"""Here is the unified diff to fix the issue:

{_SAMPLE_DIFF}

And here is the required metadata:

{json.dumps(_sample_valid_json(_TEST_A), indent=2)}

Thank you.
"""
        diff, metadata = fix_agent.parse_fix_response(raw, _TEST_A)
        assert "--- a/src/shared_cache.py" in diff
        assert metadata["subagent"] == "fix_agent"
        assert metadata["confidence"] == "high"


# ===========================================================================
# 6. Markdown-fenced diff handling
# ===========================================================================

class TestMarkdownFencedDiffHandling:
    def test_fenced_diff_and_fenced_json(self):
        raw = f"""```diff
{_SAMPLE_DIFF}
```

```json
{json.dumps(_sample_valid_json(_TEST_A), indent=2)}
```"""
        diff, metadata = fix_agent.parse_fix_response(raw, _TEST_A)

        assert not diff.startswith("```")
        assert not diff.endswith("```")
        assert "--- a/src/shared_cache.py" in diff
        assert metadata["subagent"] == "fix_agent"

    def test_generic_fence_diff(self):
        raw = f"""```
{_SAMPLE_DIFF}
```

```json
{json.dumps(_sample_valid_json(_TEST_A), indent=2)}
```"""
        diff, metadata = fix_agent.parse_fix_response(raw, _TEST_A)
        assert not diff.startswith("```")
        assert "--- a/src/shared_cache.py" in diff


# ===========================================================================
# 7. Valid schema acceptance
# ===========================================================================

class TestValidSchemaAcceptance:
    @pytest.mark.parametrize("conf", ["high", "medium", "low"])
    def test_valid_schema_accepted(self, conf: str):
        payload = _sample_valid_json(_TEST_A, confidence=conf)
        # Should not raise
        fix_agent.validate_fix_schema(payload, _TEST_A)


# ===========================================================================
# 8. Missing-field rejection
# ===========================================================================

class TestMissingFieldRejection:
    @pytest.mark.parametrize("field", [
        "subagent",
        "test_name",
        "fix_summary",
        "justification",
        "confidence",
    ])
    def test_missing_required_field_raises(self, field: str):
        payload = _sample_valid_json(_TEST_A)
        del payload[field]

        with pytest.raises(ValueError) as exc_info:
            fix_agent.validate_fix_schema(payload, _TEST_A)
        assert "missing required keys" in str(exc_info.value)


# ===========================================================================
# 9. Wrong test-name rejection
# ===========================================================================

class TestWrongTestNameRejection:
    def test_wrong_test_name_raises(self):
        payload = _sample_valid_json("tests/other.py::test_other")
        with pytest.raises(ValueError) as exc_info:
            fix_agent.validate_fix_schema(payload, _TEST_A)
        assert "Expected test_name" in str(exc_info.value)


# ===========================================================================
# 10. Invalid confidence rejection
# ===========================================================================

class TestInvalidConfidenceRejection:
    @pytest.mark.parametrize("bad_conf", ["very high", "certain", "unknown", 1, None])
    def test_invalid_confidence_raises(self, bad_conf: Any):
        payload = _sample_valid_json(_TEST_A, confidence=bad_conf)
        with pytest.raises(ValueError) as exc_info:
            fix_agent.validate_fix_schema(payload, _TEST_A)
        assert "Invalid confidence" in str(exc_info.value)


# ===========================================================================
# 11. Wrong subagent rejection
# ===========================================================================

class TestWrongSubagentRejection:
    @pytest.mark.parametrize("bad_subagent", ["coordinator", "planner", "fix", "subagent"])
    def test_wrong_subagent_raises(self, bad_subagent: str):
        payload = _sample_valid_json(_TEST_A, subagent=bad_subagent)
        with pytest.raises(ValueError) as exc_info:
            fix_agent.validate_fix_schema(payload, _TEST_A)
        assert "Expected subagent 'fix_agent'" in str(exc_info.value)


# ===========================================================================
# 12. Missing diff rejection
# ===========================================================================

class TestMissingDiffRejection:
    def test_response_with_only_json_raises(self):
        raw = json.dumps(_sample_valid_json(_TEST_A), indent=2)
        with pytest.raises(ValueError) as exc_info:
            fix_agent.parse_fix_response(raw, _TEST_A)
        assert "Missing unified diff" in str(exc_info.value)

    def test_response_with_non_diff_text_raises(self):
        raw = f"I suggest editing src/shared_cache.py to reset state.\n\n{json.dumps(_sample_valid_json(_TEST_A), indent=2)}"
        with pytest.raises(ValueError) as exc_info:
            fix_agent.parse_fix_response(raw, _TEST_A)
        assert "Invalid diff" in str(exc_info.value)


# ===========================================================================
# 13. Malformed JSON rejection
# ===========================================================================

class TestMalformedJsonRejection:
    def test_malformed_json_block_raises(self):
        raw = f"{_SAMPLE_DIFF}\n\n{{ 'subagent': 'fix_agent', invalid json ... }}"
        with pytest.raises(ValueError) as exc_info:
            fix_agent.parse_fix_response(raw, _TEST_A)
        assert "no valid JSON block" in str(exc_info.value) or "Malformed" in str(exc_info.value)


# ===========================================================================
# 14. Bob failure handling & 15. Fallback does not fabricate a diff
# ===========================================================================

class TestBobFailureAndFallback:
    def test_fallback_does_not_fabricate_diff(self, tmp_path: Path):
        fallback = fix_agent.fallback_fix(_TEST_A, reason="Testing fallback behavior")
        assert fallback["subagent"] == "fix_agent"
        assert fallback["test_name"] == _TEST_A
        assert fallback["confidence"] == "low"
        assert "NO FIX GENERATED" in fallback["fix_summary"]
        assert "human review required" in fallback["fix_summary"].lower()

        # Check no diff is created
        diff_path = tmp_path / f"fix_{fix_agent._safe_alnum(_TEST_A)}.diff"
        assert not diff_path.exists()

    @patch("analyzer.fix_agent._invoke_bob")
    def test_bob_failure_activates_fallback_in_runner(self, mock_invoke: MagicMock, tmp_path: Path):
        mock_invoke.return_value = (None, "Subprocess timed out")

        # Create coordinator evidence in tmp_path
        coord_file = tmp_path / f"coordinator_{fix_agent._safe_alnum(_TEST_A)}.json"
        coord_file.write_text(json.dumps({"test_name": _TEST_A}), encoding="utf-8")

        result = fix_agent.run_fix_agent(_TEST_A, evidence_dir=tmp_path)
        assert result["subagent"] == "fix_agent"
        assert result["confidence"] == "low"
        assert "NO FIX GENERATED" in result["fix_summary"]

        # Ensure no .diff file was written
        diff_path = tmp_path / f"fix_{fix_agent._safe_alnum(_TEST_A)}.diff"
        assert not diff_path.exists()

        # Ensure .json file was written
        json_path = tmp_path / f"fix_{fix_agent._safe_alnum(_TEST_A)}.json"
        assert json_path.exists()

    @patch("subprocess.run")
    @patch("analyzer.fix_agent._resolve_bob")
    def test_invoke_bob_handles_nonzero_exit(self, mock_resolve: MagicMock, mock_run: MagicMock):
        mock_resolve.return_value = ["bob"]
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "Error: Bob API key is required."
        mock_run.return_value = mock_proc

        result, reason = fix_agent._invoke_bob("test prompt", _TEST_A)
        assert result is None
        assert "Bob exited with code 1" in str(reason)

    @patch("subprocess.run")
    @patch("analyzer.fix_agent._resolve_bob")
    def test_invoke_bob_handles_timeout(self, mock_resolve: MagicMock, mock_run: MagicMock):
        mock_resolve.return_value = ["bob"]
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["bob"], timeout=120)

        result, reason = fix_agent._invoke_bob("test prompt", _TEST_A)
        assert result is None
        assert "timed out" in str(reason)


# ===========================================================================
# 16. Forbidden verification claim rejection (MANDATORY)
# ===========================================================================

class TestForbiddenVerificationClaimRejection:
    @pytest.mark.parametrize("forbidden_phrase", [
        "confirmed",
        "verified",
        "tested and works",
        "guaranteed",
        "fix works",
        "tests pass",
        "test passes",
        "tests passed",
        "issue resolved",
        "successfully fixes",
        "successfully fixed",
    ])
    def test_forbidden_claim_in_fix_summary_rejected(self, forbidden_phrase: str):
        payload = _sample_valid_json(
            _TEST_A,
            fix_summary=f"This change is {forbidden_phrase} by testing.",
        )
        with pytest.raises(ValueError) as exc_info:
            fix_agent.validate_fix_schema(payload, _TEST_A)
        assert "Forbidden verification claim detected" in str(exc_info.value)
        assert forbidden_phrase in str(exc_info.value)

    @pytest.mark.parametrize("forbidden_phrase", [
        "confirmed",
        "verified",
        "tested and works",
        "guaranteed",
        "fix works",
        "tests pass",
        "issue resolved",
        "successfully fixes",
    ])
    def test_forbidden_claim_in_justification_rejected(self, forbidden_phrase: str):
        payload = _sample_valid_json(
            _TEST_A,
            justification=f"Because the fix is {forbidden_phrase} under all conditions.",
        )
        with pytest.raises(ValueError) as exc_info:
            fix_agent.validate_fix_schema(payload, _TEST_A)
        assert "Forbidden verification claim detected" in str(exc_info.value)

    def test_forbidden_claim_in_parse_fix_response_rejected(self):
        invalid_json = _sample_valid_json(
            _TEST_A,
            fix_summary="This verified fix resolves the issue.",
        )
        raw = f"{_SAMPLE_DIFF}\n\n{json.dumps(invalid_json, indent=2)}"
        with pytest.raises(ValueError) as exc_info:
            fix_agent.parse_fix_response(raw, _TEST_A)
        assert "Forbidden verification claim detected" in str(exc_info.value)


# ===========================================================================
# 17. Evidence persistence
# ===========================================================================

class TestEvidencePersistence:
    def test_persist_diff_and_json(self, tmp_path: Path):
        diff_path = fix_agent._persist_diff(_SAMPLE_DIFF, _TEST_A, evidence_dir=tmp_path)
        assert diff_path.exists()
        assert diff_path.name == f"fix_{fix_agent._safe_alnum(_TEST_A)}.diff"
        assert diff_path.read_text(encoding="utf-8") == _SAMPLE_DIFF

        data = _sample_valid_json(_TEST_A)
        json_path = fix_agent._persist_evidence(data, _TEST_A, evidence_dir=tmp_path)
        assert json_path.exists()
        assert json_path.name == f"fix_{fix_agent._safe_alnum(_TEST_A)}.json"
        loaded = json.loads(json_path.read_text(encoding="utf-8"))
        assert loaded["subagent"] == "fix_agent"

    def test_stale_diff_removed_on_fallback(self, tmp_path: Path):
        stale_diff = tmp_path / f"fix_{fix_agent._safe_alnum(_TEST_A)}.diff"
        stale_diff.write_text("stale diff content", encoding="utf-8")
        assert stale_diff.exists()

        fix_agent._remove_stale_diff(_TEST_A, evidence_dir=tmp_path)
        assert not stale_diff.exists()


# ===========================================================================
# 18. Test A handling
# ===========================================================================

class TestTestAHandling:
    @patch("analyzer.fix_agent._invoke_bob")
    def test_test_a_successful_fix_proposal(self, mock_invoke: MagicMock, tmp_path: Path):
        coord_file = tmp_path / f"coordinator_{fix_agent._safe_alnum(_TEST_A)}.json"
        coord_file.write_text(json.dumps({
            "test_name": _TEST_A,
            "ranked_cause": "Shared mutable module-level state creates order dependency.",
            "winning_evidence": "11/11 passes when target runs first, 9/9 fails when mutator runs first.",
            "convergence_count": 4,
            "confidence": "high",
        }), encoding="utf-8")

        metadata = _sample_valid_json(
            _TEST_A,
            fix_summary="Add clean_cache autouse fixture in conftest.py to reset state before and after each test.",
            justification="Addresses module-level mutable state in shared_cache.py identified by coordinator.",
        )
        mock_invoke.return_value = ((_SAMPLE_DIFF, metadata), None)

        result = fix_agent.run_fix_agent(_TEST_A, evidence_dir=tmp_path)

        assert result["test_name"] == _TEST_A
        assert result["subagent"] == "fix_agent"
        assert result["confidence"] == "high"

        # Verify both diff and json were written
        diff_file = tmp_path / f"fix_{fix_agent._safe_alnum(_TEST_A)}.diff"
        json_file = tmp_path / f"fix_{fix_agent._safe_alnum(_TEST_A)}.json"
        assert diff_file.exists()
        assert json_file.exists()
        assert "shared_cache" in diff_file.read_text(encoding="utf-8")


# ===========================================================================
# 19. Test D handling
# ===========================================================================

class TestTestDHandling:
    @patch("analyzer.fix_agent._invoke_bob")
    def test_test_d_successful_fix_proposal(self, mock_invoke: MagicMock, tmp_path: Path):
        coord_file = tmp_path / f"coordinator_{fix_agent._safe_alnum(_TEST_D)}.json"
        coord_file.write_text(json.dumps({
            "test_name": _TEST_D,
            "ranked_cause": "Git regression introduced non-deterministic +1 off-by-one flakiness.",
            "winning_evidence": "Git bisect identified commit 04805c1 as first bad commit.",
            "convergence_count": 4,
            "confidence": "high",
        }), encoding="utf-8")

        metadata = _sample_valid_json(
            _TEST_D,
            fix_summary="Revert flaky random calculation in regression.py introduced by commit 04805c1.",
            justification="Addresses git bisect evidence identifying commit 04805c1 as the defect origin.",
        )
        mock_invoke.return_value = ((_SAMPLE_TEST_D_DIFF, metadata), None)

        result = fix_agent.run_fix_agent(_TEST_D, evidence_dir=tmp_path)

        assert result["test_name"] == _TEST_D
        assert result["subagent"] == "fix_agent"
        assert result["confidence"] == "high"

        diff_file = tmp_path / f"fix_{fix_agent._safe_alnum(_TEST_D)}.diff"
        json_file = tmp_path / f"fix_{fix_agent._safe_alnum(_TEST_D)}.json"
        assert diff_file.exists()
        assert json_file.exists()
        assert "calculate_total" in diff_file.read_text(encoding="utf-8")


# ===========================================================================
# Node ID validation & CLI
# ===========================================================================

class TestNodeIdAndCli:
    def test_node_id_without_double_colon_raises(self, tmp_path: Path):
        with pytest.raises(ValueError) as exc_info:
            fix_agent.run_fix_agent("tests/test_a_order.py", evidence_dir=tmp_path)
        assert "full pytest node ID" in str(exc_info.value)

    def test_cli_usage_error_on_missing_args(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
        monkeypatch.setattr(sys, "argv", ["analyzer/fix_agent.py"])
        ret = fix_agent.main()
        assert ret == 2
        captured = capsys.readouterr()
        assert "Usage: python analyzer/fix_agent.py" in captured.err

    def test_cli_error_on_missing_coordinator(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path):
        monkeypatch.setattr(sys, "argv", ["analyzer/fix_agent.py", "tests/missing.py::test_missing"])
        with patch.object(fix_agent, "_EVIDENCE_DIR", tmp_path):
            ret = fix_agent.main()
        assert ret == 1
        captured = capsys.readouterr()
        assert "Step 11 input is missing" in captured.err


# ===========================================================================
# 20. Diff sanity check (git apply --check on scratch copies)
# ===========================================================================

class TestDiffSanityCheck:
    def _create_scratch_git_repo(self, tmp_path: Path) -> Path:
        demo_repo = _ROOT / "demo-repo"
        scratch = tmp_path / "scratch_demo_repo"
        shutil.copytree(demo_repo, scratch, ignore=shutil.ignore_patterns(".git"))
        subprocess.run(["git", "init"], cwd=str(scratch), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(scratch), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(scratch), capture_output=True, check=True)
        subprocess.run(["git", "add", "."], cwd=str(scratch), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(scratch), capture_output=True, check=True)
        return scratch

    def test_test_a_diff_git_apply_check_on_scratch_copy(self, tmp_path: Path):
        demo_repo = _ROOT / "demo-repo"
        if not demo_repo.exists():
            pytest.skip("demo-repo directory not available")

        scratch = self._create_scratch_git_repo(tmp_path)

        diff_a = (
            "diff --git a/conftest.py b/conftest.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/conftest.py\n"
            "@@ -0,0 +1,9 @@\n"
            "+import pytest\n"
            "+from src.shared_cache import reset_cache\n"
            "+\n"
            "+\n"
            "+@pytest.fixture(autouse=True)\n"
            "+def clean_cache():\n"
            "+    reset_cache()\n"
            "+    yield\n"
            "+    reset_cache()\n"
        )

        res = subprocess.run(
            ["git", "apply", "--check", "-"],
            input=diff_a,
            text=True,
            cwd=str(scratch),
            capture_output=True,
        )
        assert res.returncode == 0, f"git apply --check failed: {res.stderr}"

    def test_test_d_diff_git_apply_check_on_scratch_copy(self, tmp_path: Path):
        demo_repo = _ROOT / "demo-repo"
        if not demo_repo.exists():
            pytest.skip("demo-repo directory not available")

        scratch = self._create_scratch_git_repo(tmp_path)

        diff_d = (
            "diff --git a/src/regression.py b/src/regression.py\n"
            "--- a/src/regression.py\n"
            "+++ b/src/regression.py\n"
            "@@ -5,15 +5,5 @@\n"
            " def calculate_total(items):\n"
            "     total = sum(items)\n"
            " \n"
            "-    # REGRESSION:\n"
            "-    # Random behavior was accidentally introduced here.\n"
            "-    seed = os.getenv(\"FLAKE_DEMO_SEED\")\n"
            "-    if seed is not None:\n"
            "-        rng = random.Random(int(seed))\n"
            "-        if rng.random() < 0.5:\n"
            "-            total += 1\n"
            "-    else:\n"
            "-        if random.random() < 0.5:\n"
            "-            total += 1\n"
            " \n"
            "     return total\n"
        )

        res = subprocess.run(
            ["git", "apply", "--check", "--ignore-whitespace", "-"],
            input=diff_d,
            text=True,
            cwd=str(scratch),
            capture_output=True,
        )
        assert res.returncode == 0, f"git apply --check failed: {res.stderr}"

