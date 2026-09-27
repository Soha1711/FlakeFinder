"""
tests/test_coordinator.py — Step 11 tests for analyzer/coordinator.py

Run from FlakeFinder/ directory:
    pytest tests/test_coordinator.py -v

Covers:
  - Schema validation (all required fields, types, ranges)
  - Evidence loading (file resolution per subagent convention)
  - Bisect handling: N/A for A/B/C, loaded for D
  - Prompt construction (template substitution)
  - Fallback coordinator (conservative, no fabricated cause)
  - Bob invocation (Bob success, Bob failure, schema coercion)
  - Evidence persistence
  - run_coordinator end-to-end (Bob path, fallback path)
  - CLI argument validation
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import analyzer.coordinator as coord  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEST_A = "tests/test_a_order.py::test_cache_starts_clean"
_TEST_B = "tests/test_b_race.py::test_background_update_completes"
_TEST_C = "tests/test_c_random.py::test_value_is_valid"
_TEST_D = "tests/test_d_regression.py::test_calculate_total"

_ALL_TESTS = [_TEST_A, _TEST_B, _TEST_C, _TEST_D]

_AGENTS = ("isolation", "shuffle", "bisect", "static_scan", "history")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_result(test_name: str = _TEST_A, **overrides) -> dict[str, Any]:
    base: dict[str, Any] = {
        "test_name": test_name,
        "ranked_cause": "The test fails due to module-level mutable state (order-dependency).",
        "winning_evidence": "10/10 isolated passes + history confirms order-dependency.",
        "convergence_count": 4,
        "all_evidence_summary": [
            {"subagent": "isolation",   "verdict": "Passes 10/10 in isolation.",         "supports_winning_cause": True},
            {"subagent": "shuffle",     "verdict": "6/10 failures under shuffled order.", "supports_winning_cause": True},
            {"subagent": "bisect",      "verdict": "NOT APPLICABLE for this test.",        "supports_winning_cause": False},
            {"subagent": "static_scan", "verdict": "module_mutable_state found.",          "supports_winning_cause": True},
            {"subagent": "history",     "verdict": "11/11 passes when target runs first.", "supports_winning_cause": True},
        ],
        "confidence": "high",
    }
    base.update(overrides)
    return base


def _make_bundle(test_name: str = _TEST_A) -> dict[str, Any]:
    """Return a minimal plausible bundle."""
    return {
        "isolation": {
            "subagent": "isolation",
            "test_name": test_name,
            "evidence": "runs: 10, passes: 10, failures: 0",
            "hypothesis": "Order-dependent.",
            "confidence": "high",
        },
        "shuffle": {
            "subagent": "shuffle",
            "test_name": test_name,
            "evidence": "6 passed, 4 failed.",
            "hypothesis": "Order-dependent.",
            "confidence": "high",
        },
        "bisect": None,
        "static_scan": {
            "subagent": "static_scan",
            "test_name": "demo-repo/src/shared_cache.py",
            "evidence": "module_mutable_state at line 3.",
            "hypothesis": "Mutable state leaks between tests.",
            "confidence": "high",
        },
        "history": {
            "subagent": "history",
            "test_name": test_name,
            "evidence": "20 runs: 11 passes, 9 failures.",
            "hypothesis": "Order-dependent.",
            "confidence": "high",
        },
    }


# ---------------------------------------------------------------------------
# TestSchemaValidation
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_valid_result_passes(self):
        coord.validate_coordinator_schema(_valid_result(), _TEST_A)

    def test_missing_test_name_fails(self):
        r = _valid_result()
        del r["test_name"]
        with pytest.raises(ValueError, match="missing keys"):
            coord.validate_coordinator_schema(r, _TEST_A)

    def test_missing_ranked_cause_fails(self):
        r = _valid_result()
        del r["ranked_cause"]
        with pytest.raises(ValueError, match="missing keys"):
            coord.validate_coordinator_schema(r, _TEST_A)

    def test_missing_winning_evidence_fails(self):
        r = _valid_result()
        del r["winning_evidence"]
        with pytest.raises(ValueError, match="missing keys"):
            coord.validate_coordinator_schema(r, _TEST_A)

    def test_missing_convergence_count_fails(self):
        r = _valid_result()
        del r["convergence_count"]
        with pytest.raises(ValueError, match="missing keys"):
            coord.validate_coordinator_schema(r, _TEST_A)

    def test_missing_all_evidence_summary_fails(self):
        r = _valid_result()
        del r["all_evidence_summary"]
        with pytest.raises(ValueError, match="missing keys"):
            coord.validate_coordinator_schema(r, _TEST_A)

    def test_missing_confidence_fails(self):
        r = _valid_result()
        del r["confidence"]
        with pytest.raises(ValueError, match="missing keys"):
            coord.validate_coordinator_schema(r, _TEST_A)

    def test_wrong_test_name_fails(self):
        r = _valid_result(test_name="tests/other.py::test_x")
        with pytest.raises(ValueError, match="test_name"):
            coord.validate_coordinator_schema(r, _TEST_A)

    @pytest.mark.parametrize("cc", [1, 2, 3, 4, 5])
    def test_valid_convergence_count(self, cc):
        r = _valid_result(convergence_count=cc)
        coord.validate_coordinator_schema(r, _TEST_A)

    def test_convergence_count_zero_fails(self):
        r = _valid_result(convergence_count=0)
        with pytest.raises(ValueError, match="convergence_count"):
            coord.validate_coordinator_schema(r, _TEST_A)

    def test_convergence_count_six_fails(self):
        r = _valid_result(convergence_count=6)
        with pytest.raises(ValueError, match="convergence_count"):
            coord.validate_coordinator_schema(r, _TEST_A)

    def test_convergence_count_string_fails(self):
        r = _valid_result(convergence_count="4")
        with pytest.raises(ValueError, match="convergence_count"):
            coord.validate_coordinator_schema(r, _TEST_A)

    @pytest.mark.parametrize("conf", ["high", "medium", "low"])
    def test_valid_confidence_values(self, conf):
        r = _valid_result(confidence=conf)
        coord.validate_coordinator_schema(r, _TEST_A)

    def test_invalid_confidence_fails(self):
        r = _valid_result(confidence="certain")
        with pytest.raises(ValueError, match="confidence"):
            coord.validate_coordinator_schema(r, _TEST_A)

    def test_all_evidence_summary_wrong_length_fails(self):
        r = _valid_result()
        r["all_evidence_summary"] = r["all_evidence_summary"][:4]
        with pytest.raises(ValueError, match="exactly 5"):
            coord.validate_coordinator_schema(r, _TEST_A)

    def test_all_evidence_summary_duplicate_subagent_fails(self):
        r = _valid_result()
        r["all_evidence_summary"][1]["subagent"] = "isolation"  # duplicate
        with pytest.raises(ValueError, match="duplicate"):
            coord.validate_coordinator_schema(r, _TEST_A)

    def test_all_evidence_summary_unknown_subagent_fails(self):
        r = _valid_result()
        r["all_evidence_summary"][0]["subagent"] = "unknown_agent"
        with pytest.raises(ValueError, match="unknown subagent"):
            coord.validate_coordinator_schema(r, _TEST_A)

    def test_supports_winning_cause_must_be_bool(self):
        r = _valid_result()
        r["all_evidence_summary"][0]["supports_winning_cause"] = "true"
        with pytest.raises(ValueError, match="boolean"):
            coord.validate_coordinator_schema(r, _TEST_A)

    def test_verdict_must_be_non_empty(self):
        r = _valid_result()
        r["all_evidence_summary"][0]["verdict"] = ""
        with pytest.raises(ValueError, match="verdict"):
            coord.validate_coordinator_schema(r, _TEST_A)

    def test_all_five_subagent_names_required(self):
        r = _valid_result()
        # Replace bisect with a second isolation.
        r["all_evidence_summary"][2] = {
            "subagent": "isolation",
            "verdict": "duplicate",
            "supports_winning_cause": True,
        }
        with pytest.raises(ValueError):
            coord.validate_coordinator_schema(r, _TEST_A)


# ---------------------------------------------------------------------------
# TestSafeNameHelpers
# ---------------------------------------------------------------------------

class TestSafeNameHelpers:
    def test_safe_re_replaces_dots_and_slashes(self):
        result = coord._safe_re("tests/test_a.py::test_foo")
        assert "." not in result
        assert "/" not in result
        assert "::" not in result

    def test_safe_alnum_preserves_dots_and_hyphens(self):
        result = coord._safe_alnum("demo-repo/src/shared_cache.py")
        assert "." in result       # dot preserved
        assert "-" in result       # hyphen preserved
        assert "/" not in result   # slash replaced

    def test_isolation_filename_matches_runner(self):
        """Verify the filename matches what run_investigation.py actually writes."""
        test_name = _TEST_A
        expected = f"isolation_{coord._safe_re(test_name)}.json"
        assert expected == "isolation_tests_test_a_order_py__test_cache_starts_clean.json"

    def test_history_filename_matches_runner(self):
        """Verify the filename matches what run_history.py actually writes."""
        test_name = _TEST_A
        expected = f"history_{coord._safe_alnum(test_name)}.json"
        assert expected == "history_tests_test_a_order.py__test_cache_starts_clean.json"

    def test_bisect_filename_matches_runner(self):
        test_name = _TEST_D
        expected = f"bisect_{coord._safe_alnum(test_name)}.json"
        assert expected == "bisect_tests_test_d_regression.py__test_calculate_total.json"

    def test_static_filename_matches_runner(self):
        source = "demo-repo/src/shared_cache.py"
        expected = f"static_{coord._safe_alnum(source)}.json"
        assert expected == "static_demo-repo_src_shared_cache.py.json"


# ---------------------------------------------------------------------------
# TestEvidenceLoading
# ---------------------------------------------------------------------------

class TestEvidenceLoading:
    def test_load_returns_dict_with_five_keys(self, tmp_path):
        bundle = coord.load_evidence_bundle(_TEST_A, evidence_dir=tmp_path)
        assert set(bundle.keys()) == {"isolation", "shuffle", "bisect", "static_scan", "history"}

    def test_missing_files_return_none(self, tmp_path):
        bundle = coord.load_evidence_bundle(_TEST_A, evidence_dir=tmp_path)
        for v in bundle.values():
            assert v is None

    def test_existing_isolation_file_loaded(self, tmp_path):
        data = {"subagent": "isolation", "test_name": _TEST_A, "evidence": "ok", "hypothesis": "h", "confidence": "high"}
        path = tmp_path / f"isolation_{coord._safe_re(_TEST_A)}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        bundle = coord.load_evidence_bundle(_TEST_A, evidence_dir=tmp_path)
        assert bundle["isolation"] == data

    def test_existing_history_file_loaded(self, tmp_path):
        data = {"subagent": "history", "test_name": _TEST_A, "evidence": "ok", "hypothesis": "h", "confidence": "high"}
        path = tmp_path / f"history_{coord._safe_alnum(_TEST_A)}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        bundle = coord.load_evidence_bundle(_TEST_A, evidence_dir=tmp_path)
        assert bundle["history"] == data

    def test_existing_bisect_file_loaded_for_test_d(self, tmp_path):
        data = {"subagent": "bisect", "test_name": _TEST_D, "evidence": "commit abc", "hypothesis": "regression", "confidence": "high"}
        path = tmp_path / f"bisect_{coord._safe_alnum(_TEST_D)}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        bundle = coord.load_evidence_bundle(_TEST_D, evidence_dir=tmp_path)
        assert bundle["bisect"] == data

    def test_bisect_none_for_test_a_when_file_absent(self, tmp_path):
        bundle = coord.load_evidence_bundle(_TEST_A, evidence_dir=tmp_path)
        assert bundle["bisect"] is None

    def test_static_scan_uses_source_file_mapping(self, tmp_path):
        # Test A → demo-repo/src/shared_cache.py
        source = "demo-repo/src/shared_cache.py"
        data = {"subagent": "static_scan", "test_name": source, "evidence": "found", "hypothesis": "h", "confidence": "high"}
        path = tmp_path / f"static_{coord._safe_alnum(source)}.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        bundle = coord.load_evidence_bundle(_TEST_A, evidence_dir=tmp_path)
        assert bundle["static_scan"] == data

    @pytest.mark.parametrize("test_name", _ALL_TESTS)
    def test_load_returns_five_keys_for_all_tests(self, test_name, tmp_path):
        bundle = coord.load_evidence_bundle(test_name, evidence_dir=tmp_path)
        assert len(bundle) == 5


# ---------------------------------------------------------------------------
# TestBisectHandling
# ---------------------------------------------------------------------------

class TestBisectHandling:
    @pytest.mark.parametrize("test_name", [_TEST_A, _TEST_B, _TEST_C])
    def test_bisect_not_applicable_for_abc(self, test_name):
        assert test_name not in coord._BISECT_APPLICABLE_TESTS

    def test_bisect_applicable_for_test_d(self):
        assert _TEST_D in coord._BISECT_APPLICABLE_TESTS

    def test_prompt_contains_not_applicable_for_test_a(self):
        bundle = _make_bundle(_TEST_A)
        bundle["bisect"] = None
        prompt = coord.build_coordinator_prompt(_TEST_A, bundle)
        assert "NOT APPLICABLE" in prompt

    def test_prompt_does_not_penalize_bisect_for_test_a(self):
        """Verify prompt includes the 'do not penalize convergence_count' instruction."""
        bundle = _make_bundle(_TEST_A)
        bundle["bisect"] = None
        prompt = coord.build_coordinator_prompt(_TEST_A, bundle)
        assert "penalize" in prompt.lower() or "does not reduce" in prompt.lower()

    def test_prompt_uses_bisect_evidence_for_test_d(self, tmp_path):
        bundle = _make_bundle(_TEST_D)
        commit = "04805c176d22dc3b07ed581cfa99fc1e7b5dd37e"
        bundle["bisect"] = {
            "subagent": "bisect",
            "test_name": _TEST_D,
            "evidence": f"First bad commit: {commit}.",
            "hypothesis": "Regression introduced by commit.",
            "confidence": "high",
        }
        prompt = coord.build_coordinator_prompt(_TEST_D, bundle)
        assert commit in prompt

    def test_fallback_marks_bisect_not_applicable_for_abc(self):
        bundle = _make_bundle(_TEST_A)
        bundle["bisect"] = None
        result = coord.fallback_coordinator(_TEST_A, bundle)
        bisect_entry = next(
            e for e in result["all_evidence_summary"] if e["subagent"] == "bisect"
        )
        assert "NOT APPLICABLE" in bisect_entry["verdict"]

    def test_fallback_uses_bisect_evidence_for_test_d(self):
        bundle = _make_bundle(_TEST_D)
        bundle["bisect"] = {
            "subagent": "bisect",
            "test_name": _TEST_D,
            "evidence": "commit abc.",
            "hypothesis": "Regression introduced.",
            "confidence": "high",
        }
        result = coord.fallback_coordinator(_TEST_D, bundle)
        bisect_entry = next(
            e for e in result["all_evidence_summary"] if e["subagent"] == "bisect"
        )
        assert "Regression" in bisect_entry["verdict"] or "regression" in bisect_entry["verdict"]


# ---------------------------------------------------------------------------
# TestPromptConstruction
# ---------------------------------------------------------------------------

class TestPromptConstruction:
    def test_prompt_contains_test_name(self):
        bundle = _make_bundle(_TEST_A)
        prompt = coord.build_coordinator_prompt(_TEST_A, bundle)
        assert _TEST_A in prompt

    def test_prompt_contains_all_subagent_labels(self):
        bundle = _make_bundle(_TEST_A)
        bundle["bisect"] = None
        prompt = coord.build_coordinator_prompt(_TEST_A, bundle)
        for agent in _AGENTS:
            assert agent.upper() in prompt

    def test_prompt_contains_evidence_text(self):
        bundle = _make_bundle(_TEST_A)
        prompt = coord.build_coordinator_prompt(_TEST_A, bundle)
        # isolation evidence text must appear in the prompt
        assert "runs: 10, passes: 10, failures: 0" in prompt

    def test_prompt_no_unfilled_placeholders(self):
        bundle = _make_bundle(_TEST_A)
        bundle["bisect"] = None
        prompt = coord.build_coordinator_prompt(_TEST_A, bundle)
        assert "{test_name}" not in prompt
        assert "{evidence_bundle}" not in prompt

    def test_prompt_template_loaded_from_file(self, tmp_path, monkeypatch):
        fake_prompt = tmp_path / "coordinator_prompt.md"
        fake_prompt.write_text(
            "Synthesize {test_name}\n{evidence_bundle}",
            encoding="utf-8",
        )
        monkeypatch.setattr(coord, "_COORDINATOR_PROMPT", fake_prompt)
        bundle = _make_bundle(_TEST_A)
        prompt = coord.build_coordinator_prompt(_TEST_A, bundle)
        assert _TEST_A in prompt
        assert "ISOLATION" in prompt


# ---------------------------------------------------------------------------
# TestFallbackCoordinator
# ---------------------------------------------------------------------------

class TestFallbackCoordinator:
    def test_fallback_returns_valid_schema(self):
        bundle = _make_bundle(_TEST_A)
        bundle["bisect"] = None
        result = coord.fallback_coordinator(_TEST_A, bundle)
        coord.validate_coordinator_schema(result, _TEST_A)

    def test_fallback_has_low_confidence(self):
        bundle = _make_bundle(_TEST_A)
        bundle["bisect"] = None
        result = coord.fallback_coordinator(_TEST_A, bundle)
        assert result["confidence"] == "low"

    def test_fallback_ranked_cause_is_not_fabricated(self):
        bundle = _make_bundle(_TEST_A)
        bundle["bisect"] = None
        result = coord.fallback_coordinator(_TEST_A, bundle)
        # Must contain the sentinel phrase — not a real root cause
        assert "UNABLE TO SYNTHESIZE" in result["ranked_cause"]
        assert "human review" in result["ranked_cause"]

    def test_fallback_has_five_summary_entries(self):
        bundle = _make_bundle(_TEST_A)
        bundle["bisect"] = None
        result = coord.fallback_coordinator(_TEST_A, bundle)
        assert len(result["all_evidence_summary"]) == 5

    def test_fallback_all_five_subagents_present(self):
        bundle = _make_bundle(_TEST_A)
        bundle["bisect"] = None
        result = coord.fallback_coordinator(_TEST_A, bundle)
        present = {e["subagent"] for e in result["all_evidence_summary"]}
        assert present == set(_AGENTS)

    def test_fallback_uses_hypothesis_from_evidence(self):
        bundle = _make_bundle(_TEST_A)
        bundle["bisect"] = None
        result = coord.fallback_coordinator(_TEST_A, bundle)
        iso = next(e for e in result["all_evidence_summary"] if e["subagent"] == "isolation")
        assert "Order-dependent" in iso["verdict"]

    def test_fallback_handles_missing_evidence_gracefully(self):
        bundle: dict[str, Any] = {
            "isolation": None, "shuffle": None, "bisect": None,
            "static_scan": None, "history": None,
        }
        result = coord.fallback_coordinator(_TEST_A, bundle)
        coord.validate_coordinator_schema(result, _TEST_A)
        assert result["confidence"] == "low"


# ---------------------------------------------------------------------------
# TestBobInvocation
# ---------------------------------------------------------------------------

class TestBobInvocation:
    def test_bob_not_found_returns_none(self, monkeypatch):
        monkeypatch.setattr(coord, "_resolve_bob", lambda: None)
        bundle = _make_bundle(_TEST_A)
        bundle["bisect"] = None
        result = coord._invoke_bob(_TEST_A, bundle)
        assert result is None

    def test_bob_nonzero_exit_returns_none(self, monkeypatch):
        monkeypatch.setattr(coord, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(coord, "_save_raw_response", lambda s: None)

        def fake_run(*a, **kw):
            return subprocess.CompletedProcess(
                args=a[0], returncode=1, stdout="", stderr="error"
            )

        monkeypatch.setattr(coord.subprocess, "run", fake_run)
        bundle = _make_bundle(_TEST_A)
        assert coord._invoke_bob(_TEST_A, bundle) is None

    def test_bob_empty_stdout_returns_none(self, monkeypatch):
        monkeypatch.setattr(coord, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(coord, "_save_raw_response", lambda s: None)

        def fake_run(*a, **kw):
            return subprocess.CompletedProcess(
                args=a[0], returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr(coord.subprocess, "run", fake_run)
        bundle = _make_bundle(_TEST_A)
        assert coord._invoke_bob(_TEST_A, bundle) is None

    def test_bob_invalid_json_envelope_returns_none(self, monkeypatch):
        monkeypatch.setattr(coord, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(coord, "_save_raw_response", lambda s: None)

        def fake_run(*a, **kw):
            return subprocess.CompletedProcess(
                args=a[0], returncode=0, stdout="not json", stderr=""
            )

        monkeypatch.setattr(coord.subprocess, "run", fake_run)
        bundle = _make_bundle(_TEST_A)
        assert coord._invoke_bob(_TEST_A, bundle) is None

    def test_bob_missing_last_message_returns_none(self, monkeypatch):
        monkeypatch.setattr(coord, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(coord, "_save_raw_response", lambda s: None)

        def fake_run(*a, **kw):
            return subprocess.CompletedProcess(
                args=a[0], returncode=0,
                stdout=json.dumps({"no_last_message": True}),
                stderr="",
            )

        monkeypatch.setattr(coord.subprocess, "run", fake_run)
        bundle = _make_bundle(_TEST_A)
        assert coord._invoke_bob(_TEST_A, bundle) is None

    def test_bob_success_returns_validated_dict(self, monkeypatch):
        monkeypatch.setattr(coord, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(coord, "_save_raw_response", lambda s: None)

        expected = _valid_result(_TEST_A)

        def fake_run(*a, **kw):
            return subprocess.CompletedProcess(
                args=a[0], returncode=0,
                stdout=json.dumps({"last_message": json.dumps(expected)}),
                stderr="",
            )

        monkeypatch.setattr(coord.subprocess, "run", fake_run)
        bundle = _make_bundle(_TEST_A)
        result = coord._invoke_bob(_TEST_A, bundle)
        assert result is not None
        assert result["test_name"] == _TEST_A
        assert result["confidence"] == "high"

    def test_bob_coerces_convergence_count_string(self, monkeypatch):
        """Bob sometimes returns convergence_count as a string; must be coerced."""
        monkeypatch.setattr(coord, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(coord, "_save_raw_response", lambda s: None)

        r = _valid_result(_TEST_A)
        r["convergence_count"] = "4"  # string from Bob

        def fake_run(*a, **kw):
            return subprocess.CompletedProcess(
                args=a[0], returncode=0,
                stdout=json.dumps({"last_message": json.dumps(r)}),
                stderr="",
            )

        monkeypatch.setattr(coord.subprocess, "run", fake_run)
        bundle = _make_bundle(_TEST_A)
        result = coord._invoke_bob(_TEST_A, bundle)
        assert result is not None
        assert result["convergence_count"] == 4

    def test_bob_coerces_supports_winning_cause_string(self, monkeypatch):
        """Bob sometimes returns supports_winning_cause as 'true' string; must be coerced."""
        monkeypatch.setattr(coord, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(coord, "_save_raw_response", lambda s: None)

        r = _valid_result(_TEST_A)
        # Poison some entries with string booleans.
        for entry in r["all_evidence_summary"]:
            entry["supports_winning_cause"] = "true"

        def fake_run(*a, **kw):
            return subprocess.CompletedProcess(
                args=a[0], returncode=0,
                stdout=json.dumps({"last_message": json.dumps(r)}),
                stderr="",
            )

        monkeypatch.setattr(coord.subprocess, "run", fake_run)
        bundle = _make_bundle(_TEST_A)
        result = coord._invoke_bob(_TEST_A, bundle)
        assert result is not None
        for entry in result["all_evidence_summary"]:
            assert isinstance(entry["supports_winning_cause"], bool)

    def test_bob_uses_devnull_stdin(self, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(coord, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(coord, "_save_raw_response", lambda s: None)

        def fake_run(*a, **kw):
            captured["stdin"] = kw.get("stdin")
            return subprocess.CompletedProcess(
                args=a[0], returncode=0,
                stdout=json.dumps({"last_message": json.dumps(_valid_result(_TEST_A))}),
                stderr="",
            )

        monkeypatch.setattr(coord.subprocess, "run", fake_run)
        coord._invoke_bob(_TEST_A, _make_bundle(_TEST_A))
        assert captured["stdin"] is subprocess.DEVNULL

    def test_bob_schema_invalid_falls_back_to_none(self, monkeypatch):
        monkeypatch.setattr(coord, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(coord, "_save_raw_response", lambda s: None)

        bad = {"test_name": _TEST_A}  # missing required fields

        def fake_run(*a, **kw):
            return subprocess.CompletedProcess(
                args=a[0], returncode=0,
                stdout=json.dumps({"last_message": json.dumps(bad)}),
                stderr="",
            )

        monkeypatch.setattr(coord.subprocess, "run", fake_run)
        bundle = _make_bundle(_TEST_A)
        assert coord._invoke_bob(_TEST_A, bundle) is None


# ---------------------------------------------------------------------------
# TestEvidencePersistence
# ---------------------------------------------------------------------------

class TestEvidencePersistence:
    def test_persist_creates_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(coord, "_EVIDENCE_DIR", tmp_path)
        data = _valid_result()
        path = coord._persist_evidence(data, _TEST_A)
        assert path.exists()

    def test_persist_filename_uses_safe_alnum(self, tmp_path, monkeypatch):
        monkeypatch.setattr(coord, "_EVIDENCE_DIR", tmp_path)
        coord._persist_evidence(_valid_result(), _TEST_A)
        files = list(tmp_path.iterdir())
        assert any(f.name.startswith("coordinator_") for f in files)

    def test_persist_content_is_valid_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(coord, "_EVIDENCE_DIR", tmp_path)
        data = _valid_result()
        path = coord._persist_evidence(data, _TEST_A)
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["test_name"] == _TEST_A


# ---------------------------------------------------------------------------
# TestRunCoordinator (end-to-end, Bob path)
# ---------------------------------------------------------------------------

class TestRunCoordinator:
    def test_run_coordinator_uses_bob_result(self, monkeypatch, tmp_path):
        expected = _valid_result(_TEST_A)

        monkeypatch.setattr(coord, "_invoke_bob", lambda tn, bundle: expected)
        monkeypatch.setattr(coord, "_EVIDENCE_DIR", tmp_path)

        result = coord.run_coordinator(_TEST_A, evidence_dir=tmp_path)
        assert result["test_name"] == _TEST_A
        assert result["confidence"] == "high"

    def test_run_coordinator_uses_fallback_when_bob_fails(self, monkeypatch, tmp_path):
        monkeypatch.setattr(coord, "_invoke_bob", lambda tn, bundle: None)
        monkeypatch.setattr(coord, "_EVIDENCE_DIR", tmp_path)

        result = coord.run_coordinator(_TEST_A, evidence_dir=tmp_path)
        assert result["test_name"] == _TEST_A
        assert result["confidence"] == "low"
        assert "UNABLE TO SYNTHESIZE" in result["ranked_cause"]

    def test_run_coordinator_persists_evidence(self, monkeypatch, tmp_path):
        expected = _valid_result(_TEST_A)
        monkeypatch.setattr(coord, "_invoke_bob", lambda tn, bundle: expected)
        monkeypatch.setattr(coord, "_EVIDENCE_DIR", tmp_path)

        coord.run_coordinator(_TEST_A, evidence_dir=tmp_path)

        files = [f for f in tmp_path.iterdir() if f.name.startswith("coordinator_")]
        assert len(files) == 1

    def test_run_coordinator_rejects_invalid_node_id(self):
        with pytest.raises(ValueError, match="pytest node ID"):
            coord.run_coordinator("tests/test_a_order.py")

    def test_run_coordinator_output_is_json_serializable(self, monkeypatch, tmp_path):
        expected = _valid_result(_TEST_A)
        monkeypatch.setattr(coord, "_invoke_bob", lambda tn, bundle: expected)
        monkeypatch.setattr(coord, "_EVIDENCE_DIR", tmp_path)

        result = coord.run_coordinator(_TEST_A, evidence_dir=tmp_path)
        encoded = json.dumps(result)
        decoded = json.loads(encoded)
        assert decoded["test_name"] == _TEST_A

    @pytest.mark.parametrize("test_name", _ALL_TESTS)
    def test_run_coordinator_works_for_all_four_tests(self, test_name, monkeypatch, tmp_path):
        expected = _valid_result(test_name)
        monkeypatch.setattr(coord, "_invoke_bob", lambda tn, bundle: expected)
        monkeypatch.setattr(coord, "_EVIDENCE_DIR", tmp_path)

        result = coord.run_coordinator(test_name, evidence_dir=tmp_path)
        assert result["test_name"] == test_name


# ---------------------------------------------------------------------------
# TestJsonParsing
# ---------------------------------------------------------------------------

class TestJsonParsing:
    def test_parse_dict_passthrough(self):
        data = {"key": "value"}
        assert coord._parse_last_message(data) == data

    def test_parse_plain_json_string(self):
        data = {"key": "value"}
        assert coord._parse_last_message(json.dumps(data)) == data

    def test_parse_fenced_json(self):
        data = {"key": "value"}
        raw = "```json\n" + json.dumps(data) + "\n```"
        assert coord._parse_last_message(raw) == data

    def test_parse_json_embedded_in_text(self):
        data = {"key": "value"}
        raw = f"Here is the result: {json.dumps(data)} — end."
        assert coord._parse_last_message(raw) == data

    def test_parse_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            coord._parse_last_message("")

    def test_parse_invalid_raises(self):
        with pytest.raises(ValueError):
            coord._parse_last_message("not json at all")

    def test_parse_non_string_non_dict_raises(self):
        with pytest.raises(ValueError):
            coord._parse_last_message(42)
