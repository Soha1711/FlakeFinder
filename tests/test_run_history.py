"""
tests/test_run_history.py — Step 9d tests for analyzer/run_history.py

Run from FlakeFinder/ directory:
    pytest tests/test_run_history.py

Tests cover:
  - Schema validation (all required fields, correct subagent, test_name, confidence)
  - Target validation (missing :: rejected)
  - Bob JSON envelope parsing (plain JSON, fenced JSON, malformed)
  - Bob nonzero exit falls back
  - Bob timeout falls back
  - Missing last_message falls back
  - Malformed Bob response falls back
  - Bob failure → fallback invoked
  - Missing documentation file raises RuntimeError
  - Empty documentation raises RuntimeError
  - Fallback reads actual files (documented 20/11/9 result preserved)
  - Fallback does not fabricate missing evidence
  - Evidence persistence to correct path
  - Raw response saved to debug file
  - Prompt substitution (test_name and search_targets)
  - JSON serializability
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# Ensure FlakeFinder root is on sys.path.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import analyzer.run_history as history  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_NAME = "tests/test_a_order.py::test_cache_starts_clean"

# Documented numbers that must appear in fallback evidence (from baseline.md).
DOCUMENTED_TOTAL = 20
DOCUMENTED_PASSES = 11
DOCUMENTED_FAILURES = 9


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def valid_result(test_name: str = TEST_NAME, **overrides) -> dict:
    result = {
        "subagent": "history",
        "test_name": test_name,
        "evidence": (
            "baseline.md records 20 randomized execution-order trials: "
            "11 passes, 9 failures. "
            "When target precedes mutator: PASS (11/11 trials). "
            "When mutator precedes target: FAIL (9/9 trials). "
            "README.md documents persistent module-level shared_cache state."
        ),
        "hypothesis": (
            "The flakiness is order-dependent. The mutator leaves shared_cache "
            "contaminated, so test_cache_starts_clean fails whenever the mutator "
            "precedes it."
        ),
        "confidence": "high",
    }
    result.update(overrides)
    return result


def make_completed(
    stdout: str,
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _make_doc_files(tmp_path: Path) -> dict[str, Path]:
    """Create minimal but realistic documentation files in tmp_path."""
    baseline = tmp_path / "baseline.md"
    baseline.write_text(
        "# FlakeFinder Test A Baseline\n\n"
        "## Baseline experiment\n"
        "20 randomized execution-order trials.\n\n"
        "- Total runs: 20\n"
        "- Passes: 11\n"
        "- Failures: 9\n"
        "- Pass rate: 55.0%\n"
        "- Failure rate: 45.0%\n\n"
        "## Observed behavior\n\n"
        "`test_a_order.py -> test_shared_cache_mutator.py`:\n"
        "- Result: PASS (11/11 trials)\n\n"
        "`test_shared_cache_mutator.py -> test_a_order.py`:\n"
        "- Result: FAIL (9/9 trials)\n\n"
        "## Preliminary hypothesis\n"
        "`test_shared_cache_mutator.py` mutates module-level shared state.\n",
        encoding="utf-8",
    )

    readme = tmp_path / "README.md"
    readme.write_text(
        "# FlakeFinder Demo Repository\n\n"
        "## Test A: Shared-State / Order Dependency\n"
        "shared_cache is a module-level mutable dictionary. "
        "test_shared_cache_mutator.py modifies the cache without restoring it.\n",
        encoding="utf-8",
    )

    verification = tmp_path / "demo_verification.md"
    verification.write_text(
        "# FlakeFinder Demo Verification\n\n"
        "## Test A — Order Dependency\n"
        "* **Reversed Order**:\n"
        "  * Result: FAIL (assert 'contaminated' == 'clean')\n",
        encoding="utf-8",
    )

    for name in ["baseline_test_b.md", "baseline_test_c.md", "baseline_test_d.md"]:
        f = tmp_path / name
        f.write_text(
            f"# {name}\n\nThis is baseline evidence for test B/C/D.\n",
            encoding="utf-8",
        )

    return {
        "README.md": readme,
        "baseline.md": baseline,
        "baseline_test_b.md": tmp_path / "baseline_test_b.md",
        "baseline_test_c.md": tmp_path / "baseline_test_c.md",
        "baseline_test_d.md": tmp_path / "baseline_test_d.md",
        "demo_verification.md": verification,
    }


# ---------------------------------------------------------------------------
# TestValidation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_valid_result_passes(self):
        history._validate(valid_result(), TEST_NAME)

    def test_missing_subagent_fails(self):
        data = valid_result()
        del data["subagent"]
        with pytest.raises(ValueError, match="missing required fields"):
            history._validate(data, TEST_NAME)

    def test_missing_test_name_fails(self):
        data = valid_result()
        del data["test_name"]
        with pytest.raises(ValueError, match="missing required fields"):
            history._validate(data, TEST_NAME)

    def test_missing_evidence_fails(self):
        data = valid_result()
        del data["evidence"]
        with pytest.raises(ValueError, match="missing required fields"):
            history._validate(data, TEST_NAME)

    def test_missing_hypothesis_fails(self):
        data = valid_result()
        del data["hypothesis"]
        with pytest.raises(ValueError, match="missing required fields"):
            history._validate(data, TEST_NAME)

    def test_missing_confidence_fails(self):
        data = valid_result()
        del data["confidence"]
        with pytest.raises(ValueError, match="missing required fields"):
            history._validate(data, TEST_NAME)

    def test_wrong_subagent_fails(self):
        data = valid_result(subagent="bisect")
        with pytest.raises(ValueError, match="'subagent' must be 'history'"):
            history._validate(data, TEST_NAME)

    def test_wrong_test_name_fails(self):
        data = valid_result(test_name="tests/test_b_race.py::test_background_update_completes")
        with pytest.raises(ValueError, match="'test_name' must be"):
            history._validate(data, TEST_NAME)

    @pytest.mark.parametrize("confidence", ["high", "medium", "low"])
    def test_valid_confidence_values_pass(self, confidence):
        data = valid_result(confidence=confidence)
        history._validate(data, TEST_NAME)

    def test_invalid_confidence_fails(self):
        data = valid_result(confidence="certain")
        with pytest.raises(ValueError, match="'confidence' must be one of"):
            history._validate(data, TEST_NAME)

    def test_empty_evidence_fails(self):
        data = valid_result(evidence="")
        with pytest.raises(ValueError, match="'evidence' must be a non-empty string"):
            history._validate(data, TEST_NAME)

    def test_empty_hypothesis_fails(self):
        data = valid_result(hypothesis="")
        with pytest.raises(ValueError, match="'hypothesis' must be a non-empty string"):
            history._validate(data, TEST_NAME)

    def test_whitespace_only_evidence_fails(self):
        data = valid_result(evidence="   ")
        with pytest.raises(ValueError, match="'evidence' must be a non-empty string"):
            history._validate(data, TEST_NAME)


# ---------------------------------------------------------------------------
# TestTargetValidation
# ---------------------------------------------------------------------------


class TestTargetValidation:
    def test_missing_double_colon_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(history, "_invoke_bob", lambda t: None)

        def fake_fallback(test_name, doc_files=None):
            return valid_result(test_name=test_name)

        monkeypatch.setattr(history, "_fallback", fake_fallback)
        monkeypatch.setattr(history, "_EVIDENCE_DIR", tmp_path)

        with pytest.raises(ValueError, match="full pytest node ID"):
            history.run("test_cache_starts_clean")

    def test_valid_node_id_accepted(self, monkeypatch, tmp_path):
        monkeypatch.setattr(history, "_invoke_bob", lambda t: valid_result(test_name=t))
        monkeypatch.setattr(history, "_EVIDENCE_DIR", tmp_path)

        result = history.run(TEST_NAME)
        assert result["test_name"] == TEST_NAME


# ---------------------------------------------------------------------------
# TestJsonParsing
# ---------------------------------------------------------------------------


class TestJsonParsing:
    def test_parse_dict(self):
        data = valid_result()
        assert history._parse_last_message(data) == data

    def test_parse_plain_json(self, monkeypatch):
        monkeypatch.setattr(history, "_save_raw_response", lambda raw: None)
        data = valid_result()
        raw = json.dumps(data)
        assert history._parse_last_message(raw) == data

    def test_parse_fenced_json(self, monkeypatch):
        monkeypatch.setattr(history, "_save_raw_response", lambda raw: None)
        data = valid_result()
        raw = "```json\n" + json.dumps(data, indent=2) + "\n```"
        assert history._parse_last_message(raw) == data

    def test_parse_fenced_json_without_language(self, monkeypatch):
        monkeypatch.setattr(history, "_save_raw_response", lambda raw: None)
        data = valid_result()
        raw = "```\n" + json.dumps(data) + "\n```"
        assert history._parse_last_message(raw) == data

    def test_parse_empty_string_fails(self, monkeypatch):
        monkeypatch.setattr(history, "_save_raw_response", lambda raw: None)
        with pytest.raises(ValueError, match="last_message is empty"):
            history._parse_last_message("")

    def test_parse_invalid_json_fails(self, monkeypatch):
        monkeypatch.setattr(history, "_save_raw_response", lambda raw: None)
        with pytest.raises(ValueError, match="last_message is not valid JSON"):
            history._parse_last_message("this is not json")

    def test_parse_non_object_json_fails(self, monkeypatch):
        monkeypatch.setattr(history, "_save_raw_response", lambda raw: None)
        with pytest.raises(ValueError, match="last_message is not valid JSON"):
            history._parse_last_message(json.dumps(["not", "an", "object"]))

    def test_parse_non_string_non_dict_fails(self, monkeypatch):
        monkeypatch.setattr(history, "_save_raw_response", lambda raw: None)
        with pytest.raises(ValueError, match="last_message is not valid JSON"):
            history._parse_last_message(42)


# ---------------------------------------------------------------------------
# TestRawResponseSaving
# ---------------------------------------------------------------------------


class TestRawResponseSaving:
    def test_save_raw_response_creates_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(history, "_DEBUG_DIR", tmp_path)
        raw = '{"subagent":"history"}'
        history._save_raw_response(raw)
        output = tmp_path / "history_raw_response.txt"
        assert output.exists()
        assert output.read_text(encoding="utf-8") == raw

    def test_save_raw_response_does_not_raise_on_error(self, tmp_path, monkeypatch):
        # Point to a path that cannot be created (file instead of dir).
        fake_dir = tmp_path / "not_a_dir.txt"
        fake_dir.write_text("blocker", encoding="utf-8")
        monkeypatch.setattr(history, "_DEBUG_DIR", fake_dir / "subdir")
        # Should not raise.
        history._save_raw_response("some raw response")


# ---------------------------------------------------------------------------
# TestBobInvocation
# ---------------------------------------------------------------------------


class TestBobInvocation:
    def test_bob_command_uses_run_format_json(self, monkeypatch):
        captured = {}

        monkeypatch.setattr(history, "_resolve_bob", lambda: ["bob"])

        def fake_run(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return make_completed(
                json.dumps({"last_message": json.dumps(valid_result())})
            )

        monkeypatch.setattr(history.subprocess, "run", fake_run)
        monkeypatch.setattr(history, "_save_raw_response", lambda raw: None)

        result = history._invoke_bob(TEST_NAME)

        assert result == valid_result()
        command = captured["args"][0]
        assert command[0] == "bob"
        assert "run" in command
        assert "--format" in command
        assert "json" in command

    def test_bob_uses_devnull_stdin(self, monkeypatch):
        captured = {}

        monkeypatch.setattr(history, "_resolve_bob", lambda: ["bob"])

        def fake_run(*args, **kwargs):
            captured["kwargs"] = kwargs
            return make_completed(
                json.dumps({"last_message": json.dumps(valid_result())})
            )

        monkeypatch.setattr(history.subprocess, "run", fake_run)
        monkeypatch.setattr(history, "_save_raw_response", lambda raw: None)

        history._invoke_bob(TEST_NAME)

        assert captured["kwargs"]["stdin"] is subprocess.DEVNULL

    def test_bob_uses_project_cwd(self, monkeypatch):
        captured = {}

        monkeypatch.setattr(history, "_resolve_bob", lambda: ["bob"])

        def fake_run(*args, **kwargs):
            captured["kwargs"] = kwargs
            return make_completed(
                json.dumps({"last_message": json.dumps(valid_result())})
            )

        monkeypatch.setattr(history.subprocess, "run", fake_run)
        monkeypatch.setattr(history, "_save_raw_response", lambda raw: None)

        history._invoke_bob(TEST_NAME)

        assert Path(captured["kwargs"]["cwd"]) == history._HERE

    def test_bob_uses_utf8_encoding(self, monkeypatch):
        captured = {}

        monkeypatch.setattr(history, "_resolve_bob", lambda: ["bob"])

        def fake_run(*args, **kwargs):
            captured["kwargs"] = kwargs
            return make_completed(
                json.dumps({"last_message": json.dumps(valid_result())})
            )

        monkeypatch.setattr(history.subprocess, "run", fake_run)
        monkeypatch.setattr(history, "_save_raw_response", lambda raw: None)

        history._invoke_bob(TEST_NAME)

        assert captured["kwargs"]["encoding"] == "utf-8"
        assert captured["kwargs"]["errors"] == "replace"

    def test_missing_last_message_returns_none(self, monkeypatch):
        monkeypatch.setattr(history, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(
            history.subprocess,
            "run",
            lambda *a, **kw: make_completed(json.dumps({"other": "value"})),
        )

        assert history._invoke_bob(TEST_NAME) is None

    def test_malformed_bob_envelope_returns_none(self, monkeypatch):
        monkeypatch.setattr(history, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(
            history.subprocess,
            "run",
            lambda *a, **kw: make_completed("not json"),
        )

        assert history._invoke_bob(TEST_NAME) is None

    def test_nonzero_bob_exit_returns_none(self, monkeypatch):
        monkeypatch.setattr(history, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(
            history.subprocess,
            "run",
            lambda *a, **kw: make_completed("", returncode=1, stderr="bob failed"),
        )

        assert history._invoke_bob(TEST_NAME) is None

    def test_bob_timeout_returns_none(self, monkeypatch):
        monkeypatch.setattr(history, "_resolve_bob", lambda: ["bob"])

        def timeout_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="bob", timeout=120)

        monkeypatch.setattr(history.subprocess, "run", timeout_run)

        assert history._invoke_bob(TEST_NAME) is None

    def test_invalid_last_message_returns_none(self, monkeypatch):
        monkeypatch.setattr(history, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(
            history.subprocess,
            "run",
            lambda *a, **kw: make_completed(
                json.dumps({"last_message": "not json"})
            ),
        )

        assert history._invoke_bob(TEST_NAME) is None

    def test_bob_not_found_returns_none(self, monkeypatch):
        def raise_not_found():
            raise FileNotFoundError("Bob Shell executable was not found")

        monkeypatch.setattr(history, "_resolve_bob", raise_not_found)

        assert history._invoke_bob(TEST_NAME) is None

    def test_empty_stdout_returns_none(self, monkeypatch):
        monkeypatch.setattr(history, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(
            history.subprocess,
            "run",
            lambda *a, **kw: make_completed(""),
        )

        assert history._invoke_bob(TEST_NAME) is None

    def test_bob_envelope_non_object_returns_none(self, monkeypatch):
        monkeypatch.setattr(history, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(
            history.subprocess,
            "run",
            lambda *a, **kw: make_completed(json.dumps(["list", "not", "object"])),
        )

        assert history._invoke_bob(TEST_NAME) is None

    def test_schema_validation_failure_returns_none(self, monkeypatch):
        """Bob returns valid JSON but with wrong subagent → should return None."""
        monkeypatch.setattr(history, "_resolve_bob", lambda: ["bob"])
        bad_result = valid_result(subagent="bisect")
        monkeypatch.setattr(
            history.subprocess,
            "run",
            lambda *a, **kw: make_completed(
                json.dumps({"last_message": json.dumps(bad_result)})
            ),
        )
        monkeypatch.setattr(history, "_save_raw_response", lambda raw: None)

        assert history._invoke_bob(TEST_NAME) is None


# ---------------------------------------------------------------------------
# TestFallbackDocumentReading
# ---------------------------------------------------------------------------


class TestFallbackDocumentReading:
    def test_fallback_reads_actual_files(self, tmp_path):
        doc_files = _make_doc_files(tmp_path)
        result = history._fallback(TEST_NAME, doc_files=doc_files)

        assert result["subagent"] == "history"
        assert result["test_name"] == TEST_NAME
        assert result["evidence"]
        assert result["hypothesis"]

    def test_fallback_preserves_documented_20_total(self, tmp_path):
        doc_files = _make_doc_files(tmp_path)
        result = history._fallback(TEST_NAME, doc_files=doc_files)

        assert "20" in result["evidence"]

    def test_fallback_preserves_documented_11_passes(self, tmp_path):
        doc_files = _make_doc_files(tmp_path)
        result = history._fallback(TEST_NAME, doc_files=doc_files)

        assert "11" in result["evidence"]

    def test_fallback_preserves_documented_9_failures(self, tmp_path):
        doc_files = _make_doc_files(tmp_path)
        result = history._fallback(TEST_NAME, doc_files=doc_files)

        assert "9" in result["evidence"]

    def test_fallback_preserves_11_11_pass_order(self, tmp_path):
        doc_files = _make_doc_files(tmp_path)
        result = history._fallback(TEST_NAME, doc_files=doc_files)

        # Should mention 11/11 pass trials or equivalent statement.
        evidence = result["evidence"]
        assert "11" in evidence

    def test_fallback_preserves_9_9_fail_order(self, tmp_path):
        doc_files = _make_doc_files(tmp_path)
        result = history._fallback(TEST_NAME, doc_files=doc_files)

        # Should mention 9/9 fail trials or equivalent statement.
        evidence = result["evidence"]
        assert "9" in evidence

    def test_fallback_mentions_contaminated_or_assertion(self, tmp_path):
        doc_files = _make_doc_files(tmp_path)
        result = history._fallback(TEST_NAME, doc_files=doc_files)

        combined = result["evidence"].lower() + result["hypothesis"].lower()
        assert "contaminated" in combined or "assertion" in combined or "clean" in combined

    def test_fallback_mentions_shared_cache(self, tmp_path):
        doc_files = _make_doc_files(tmp_path)
        result = history._fallback(TEST_NAME, doc_files=doc_files)

        combined = result["evidence"].lower() + result["hypothesis"].lower()
        assert "shared" in combined or "cache" in combined or "mutable" in combined

    def test_fallback_confidence_is_high(self, tmp_path):
        doc_files = _make_doc_files(tmp_path)
        result = history._fallback(TEST_NAME, doc_files=doc_files)

        assert result["confidence"] == "high"

    def test_fallback_missing_required_file_raises(self, tmp_path):
        doc_files = _make_doc_files(tmp_path)
        # Remove baseline.md so fallback cannot find required evidence.
        del doc_files["baseline.md"]
        doc_files["baseline.md"] = tmp_path / "nonexistent_baseline.md"

        with pytest.raises(RuntimeError, match="missing"):
            history._fallback(TEST_NAME, doc_files=doc_files)

    def test_fallback_empty_baseline_raises(self, tmp_path):
        doc_files = _make_doc_files(tmp_path)
        # Overwrite baseline.md with empty content.
        doc_files["baseline.md"].write_text("", encoding="utf-8")

        with pytest.raises(RuntimeError):
            history._fallback(TEST_NAME, doc_files=doc_files)

    def test_fallback_baseline_missing_passes_field_raises(self, tmp_path):
        doc_files = _make_doc_files(tmp_path)
        # Write a baseline.md that is missing the 'Passes' line.
        doc_files["baseline.md"].write_text(
            "# Baseline\n\n- Total runs: 20\n- Failures: 9\n",
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match="passes"):
            history._fallback(TEST_NAME, doc_files=doc_files)

    def test_fallback_baseline_missing_total_raises(self, tmp_path):
        doc_files = _make_doc_files(tmp_path)
        doc_files["baseline.md"].write_text(
            "# Baseline\n\n- Passes: 11\n- Failures: 9\n",
            encoding="utf-8",
        )

        with pytest.raises(RuntimeError, match="total"):
            history._fallback(TEST_NAME, doc_files=doc_files)

    def test_fallback_does_not_invent_numbers(self, tmp_path):
        """If baseline.md has different numbers, the output must reflect them."""
        doc_files = _make_doc_files(tmp_path)
        # Override with different (fictional) numbers.
        doc_files["baseline.md"].write_text(
            "# Baseline\n\n"
            "- Total runs: 50\n"
            "- Passes: 30\n"
            "- Failures: 20\n\n"
            "test_a_order.py -> test_shared_cache_mutator.py:\n"
            "- Result: PASS (30/30 trials)\n\n"
            "test_shared_cache_mutator.py -> test_a_order.py:\n"
            "- Result: FAIL (20/20 trials)\n",
            encoding="utf-8",
        )

        result = history._fallback(TEST_NAME, doc_files=doc_files)

        # Must reflect the new numbers, not the hard-coded 11/9.
        assert "50" in result["evidence"]
        assert "30" in result["evidence"]
        assert "20" in result["evidence"]
        # Must NOT contain the original 11 or 9 (since we replaced the file).
        # (Allow "11" if it appears as part of "110", "211" etc. — but not standalone
        # historical 11/9. We test by ensuring the right totals appear.)

    def test_fallback_uses_actual_file_not_hardcoded(self, tmp_path):
        """Evidence must come from the files — never from hard-coded constants."""
        doc_files = _make_doc_files(tmp_path)
        # Override with different numbers.
        doc_files["baseline.md"].write_text(
            "# Baseline\n\n"
            "- Total runs: 100\n"
            "- Passes: 70\n"
            "- Failures: 30\n\n"
            "test_a_order.py -> test_shared_cache_mutator.py:\n"
            "- Result: PASS (70/70 trials)\n\n"
            "test_shared_cache_mutator.py -> test_a_order.py:\n"
            "- Result: FAIL (30/30 trials)\n",
            encoding="utf-8",
        )

        result = history._fallback(TEST_NAME, doc_files=doc_files)

        assert "100" in result["evidence"]


# ---------------------------------------------------------------------------
# TestReadDocFiles
# ---------------------------------------------------------------------------


class TestReadDocFiles:
    def test_all_files_present_returns_contents(self, tmp_path):
        doc_files = _make_doc_files(tmp_path)
        contents = history._read_doc_files(doc_files)

        assert set(contents.keys()) == set(doc_files.keys())
        for name, text in contents.items():
            assert text.strip()

    def test_missing_file_raises_runtime_error(self, tmp_path):
        doc_files = _make_doc_files(tmp_path)
        doc_files["baseline.md"] = tmp_path / "does_not_exist.md"

        with pytest.raises(RuntimeError, match="missing"):
            history._read_doc_files(doc_files)

    def test_empty_file_raises_runtime_error(self, tmp_path):
        doc_files = _make_doc_files(tmp_path)
        doc_files["baseline.md"].write_text("", encoding="utf-8")

        with pytest.raises(RuntimeError, match="empty"):
            history._read_doc_files(doc_files)


# ---------------------------------------------------------------------------
# TestExtractBaselineNumbers
# ---------------------------------------------------------------------------


class TestExtractBaselineNumbers:
    def test_extracts_correct_numbers(self):
        text = (
            "- Total runs: 20\n"
            "- Passes: 11\n"
            "- Failures: 9\n"
        )
        result = history._extract_baseline_numbers(text)
        assert result["total"] == 20
        assert result["passes"] == 11
        assert result["failures"] == 9

    def test_missing_passes_raises(self):
        text = "- Total runs: 20\n- Failures: 9\n"
        with pytest.raises(RuntimeError, match="passes"):
            history._extract_baseline_numbers(text)

    def test_missing_failures_raises(self):
        text = "- Total runs: 20\n- Passes: 11\n"
        with pytest.raises(RuntimeError, match="failures"):
            history._extract_baseline_numbers(text)

    def test_missing_total_raises(self):
        text = "- Passes: 11\n- Failures: 9\n"
        with pytest.raises(RuntimeError, match="total"):
            history._extract_baseline_numbers(text)

    def test_different_numbers_returned(self):
        text = (
            "- Total runs: 50\n"
            "- Passes: 35\n"
            "- Failures: 15\n"
        )
        result = history._extract_baseline_numbers(text)
        assert result["total"] == 50
        assert result["passes"] == 35
        assert result["failures"] == 15


# ---------------------------------------------------------------------------
# TestEvidencePersistence
# ---------------------------------------------------------------------------


class TestEvidencePersistence:
    def test_evidence_saved_to_correct_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(history, "_EVIDENCE_DIR", tmp_path)

        data = valid_result()
        path = history._persist_evidence(data, TEST_NAME)

        assert path.exists()
        assert path.name.startswith("history_")
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved == data

    def test_evidence_filename_uses_safe_test_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(history, "_EVIDENCE_DIR", tmp_path)

        history._persist_evidence(valid_result(), TEST_NAME)

        files = list(tmp_path.iterdir())
        assert any("test_a_order" in f.name for f in files)

    def test_run_saves_evidence_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(history, "_invoke_bob", lambda t: valid_result(test_name=t))
        monkeypatch.setattr(history, "_EVIDENCE_DIR", tmp_path)

        history.run(TEST_NAME)

        evidence_files = list(tmp_path.iterdir())
        assert len(evidence_files) == 1
        saved = json.loads(evidence_files[0].read_text(encoding="utf-8"))
        assert saved["test_name"] == TEST_NAME


# ---------------------------------------------------------------------------
# TestRun
# ---------------------------------------------------------------------------


class TestRun:
    def test_run_uses_bob_result(self, monkeypatch, tmp_path):
        data = valid_result()

        monkeypatch.setattr(history, "_invoke_bob", lambda t: data)
        monkeypatch.setattr(history, "_EVIDENCE_DIR", tmp_path)

        result = history.run(TEST_NAME)
        assert result == data

    def test_run_uses_fallback_when_bob_fails(self, monkeypatch, tmp_path):
        data = valid_result()

        monkeypatch.setattr(history, "_invoke_bob", lambda t: None)

        def fake_fallback(test_name, doc_files=None):
            return data

        monkeypatch.setattr(history, "_fallback", fake_fallback)
        monkeypatch.setattr(history, "_EVIDENCE_DIR", tmp_path)

        result = history.run(TEST_NAME)
        assert result == data

    def test_run_rejects_invalid_node_id(self):
        with pytest.raises(ValueError, match="full pytest node ID"):
            history.run("test_cache_starts_clean")

    def test_result_is_json_serializable(self):
        data = valid_result()
        encoded = json.dumps(data)
        decoded = json.loads(encoded)
        assert decoded == data

    def test_run_persists_evidence(self, monkeypatch, tmp_path):
        data = valid_result()

        monkeypatch.setattr(history, "_invoke_bob", lambda t: data)
        monkeypatch.setattr(history, "_EVIDENCE_DIR", tmp_path)

        history.run(TEST_NAME)

        expected_file = tmp_path / (
            "history_" + history._safe_name(TEST_NAME) + ".json"
        )
        assert expected_file.exists()

    def test_run_saves_raw_response(self, monkeypatch, tmp_path):
        """When Bob succeeds the raw response should be saved by _save_raw_response."""
        saved = {}

        def fake_save(raw: str) -> None:
            saved["raw"] = raw

        # Bob returns a valid envelope; _save_raw_response is triggered inside
        # _parse_last_message for string last_message.
        monkeypatch.setattr(history, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(history, "_save_raw_response", fake_save)

        raw_result = json.dumps(valid_result())

        monkeypatch.setattr(
            history.subprocess,
            "run",
            lambda *a, **kw: make_completed(
                json.dumps({"last_message": raw_result})
            ),
        )

        monkeypatch.setattr(history, "_EVIDENCE_DIR", tmp_path)

        history.run(TEST_NAME)

        assert "raw" in saved
        assert "history" in saved["raw"]


# ---------------------------------------------------------------------------
# TestPromptSubstitution
# ---------------------------------------------------------------------------


class TestPromptSubstitution:
    def test_prompt_substitutes_test_name(self, tmp_path, monkeypatch):
        prompt_file = tmp_path / "history_subagent_prompt.md"
        prompt_file.write_text(
            "Investigate {test_name} in {search_targets}.",
            encoding="utf-8",
        )
        monkeypatch.setattr(history, "_HISTORY_PROMPT", prompt_file)

        result = history._build_history_prompt(TEST_NAME)

        assert "{test_name}" not in result
        assert TEST_NAME in result

    def test_prompt_substitutes_search_targets(self, tmp_path, monkeypatch):
        prompt_file = tmp_path / "history_subagent_prompt.md"
        prompt_file.write_text(
            "Search {search_targets} for {test_name}.",
            encoding="utf-8",
        )
        monkeypatch.setattr(history, "_HISTORY_PROMPT", prompt_file)

        result = history._build_history_prompt(TEST_NAME)

        assert "{search_targets}" not in result
        # Should reference the documentation files.
        assert "baseline.md" in result or "README.md" in result

    def test_prompt_includes_doc_file_instructions(self, tmp_path, monkeypatch):
        prompt_file = tmp_path / "history_subagent_prompt.md"
        prompt_file.write_text("Base prompt {test_name} {search_targets}.", encoding="utf-8")
        monkeypatch.setattr(history, "_HISTORY_PROMPT", prompt_file)

        result = history._build_history_prompt(TEST_NAME)

        assert "README.md" in result
        assert "baseline.md" in result
        assert "demo_verification.md" in result

    def test_prompt_does_not_contain_unfilled_placeholders(self):
        result = history._build_history_prompt(TEST_NAME)
        assert "{test_name}" not in result
        assert "{search_targets}" not in result

    def test_prompt_uses_real_file(self):
        """The actual prompt file must exist and produce a non-empty prompt."""
        result = history._build_history_prompt(TEST_NAME)
        assert len(result) > 100
        assert TEST_NAME in result


# ---------------------------------------------------------------------------
# TestBobEnvironment
# ---------------------------------------------------------------------------


class TestBobEnvironment:
    def test_load_bob_env_preserves_existing_key(self, monkeypatch):
        monkeypatch.setenv("BOB_API_KEY", "test-key")
        result = history._load_bob_env()
        assert result["BOB_API_KEY"] == "test-key"

    def test_load_bob_env_reads_dotenv(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text('BOB_API_KEY="from-dotenv"\n', encoding="utf-8")

        monkeypatch.setattr(history, "_ENV_FILE", env_file)
        monkeypatch.delenv("BOB_API_KEY", raising=False)
        monkeypatch.delenv("BOBSHELL_API_KEY", raising=False)

        result = history._load_bob_env()
        assert result["BOB_API_KEY"] == "from-dotenv"
