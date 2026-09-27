"""
tests/test_run_static_scan.py — Step 9c tests for analyzer/run_static_scan.py

Run from FlakeFinder/ directory:
    pytest tests/test_run_static_scan.py

Tests cover:
  - Schema validation (all required fields, correct subagent, test_name, confidence)
  - Target validation (empty target rejected)
  - Bob JSON envelope parsing (plain JSON, fenced JSON, malformed)
  - Bob nonzero exit falls back
  - Bob timeout falls back
  - Missing last_message falls back
  - Fallback scanner execution (command, stdin=DEVNULL, UTF-8 decoding)
  - Malformed scanner output raises RuntimeError
  - Missing finding_count raises ValueError
  - Invalid finding_count raises ValueError
  - Empty findings list is valid
  - Actual findings preserved in evidence
  - Evidence persistence to correct path
  - All four supported target paths produce a valid result
  - Prompt substitutes target correctly
  - subprocess.DEVNULL used for stdin (Bob invocation)
  - Raw response saved to debug file
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure FlakeFinder root is on sys.path.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_ANALYZER = _ROOT / "analyzer"
if str(_ANALYZER) not in sys.path:
    sys.path.insert(0, str(_ANALYZER))

import analyzer.run_static_scan as scan  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TARGET = "demo-repo/src/regression.py"

SUPPORTED_TARGETS = [
    "demo-repo/src/shared_cache.py",
    "demo-repo/src/async_worker.py",
    "demo-repo/src/nondeterministic.py",
    "demo-repo/src/regression.py",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def valid_result(target: str = TARGET, **overrides) -> dict:
    result = {
        "subagent": "static_scan",
        "test_name": target,
        "evidence": "Scanner reported 1 finding: line 2, type 'random_usage', 'imports random module'.",
        "hypothesis": "The file imports the random module without a fixed seed, introducing non-determinism.",
        "confidence": "high",
    }
    result.update(overrides)
    return result


def scanner_output(target: str = TARGET, findings: list | None = None) -> dict:
    """Return a dict shaped like static_scan.py's actual output."""
    if findings is None:
        findings = [
            {
                "file": target,
                "line": 2,
                "type": "random_usage",
                "evidence": "imports random module",
            }
        ]
    return {
        "subagent": "static_scan",
        "target": target,
        "findings": findings,
        "finding_count": len(findings),
    }


def make_completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ---------------------------------------------------------------------------
# TestValidation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_valid_result_passes(self):
        scan._validate(valid_result(), TARGET)

    def test_missing_subagent_fails(self):
        data = valid_result()
        del data["subagent"]
        with pytest.raises(ValueError, match="missing required fields"):
            scan._validate(data, TARGET)

    def test_missing_test_name_fails(self):
        data = valid_result()
        del data["test_name"]
        with pytest.raises(ValueError, match="missing required fields"):
            scan._validate(data, TARGET)

    def test_missing_evidence_fails(self):
        data = valid_result()
        del data["evidence"]
        with pytest.raises(ValueError, match="missing required fields"):
            scan._validate(data, TARGET)

    def test_missing_hypothesis_fails(self):
        data = valid_result()
        del data["hypothesis"]
        with pytest.raises(ValueError, match="missing required fields"):
            scan._validate(data, TARGET)

    def test_missing_confidence_fails(self):
        data = valid_result()
        del data["confidence"]
        with pytest.raises(ValueError, match="missing required fields"):
            scan._validate(data, TARGET)

    def test_wrong_subagent_fails(self):
        data = valid_result(subagent="bisect")
        with pytest.raises(ValueError, match="'subagent' must be 'static_scan'"):
            scan._validate(data, TARGET)

    def test_wrong_test_name_fails(self):
        data = valid_result(test_name="demo-repo/src/other.py")
        with pytest.raises(ValueError, match="'test_name' must be"):
            scan._validate(data, TARGET)

    @pytest.mark.parametrize("confidence", ["high", "medium", "low"])
    def test_valid_confidence_values_pass(self, confidence):
        data = valid_result(confidence=confidence)
        scan._validate(data, TARGET)

    def test_invalid_confidence_fails(self):
        data = valid_result(confidence="certain")
        with pytest.raises(ValueError, match="'confidence' must be one of"):
            scan._validate(data, TARGET)

    def test_empty_evidence_fails(self):
        data = valid_result(evidence="")
        with pytest.raises(ValueError, match="'evidence' must be a non-empty string"):
            scan._validate(data, TARGET)

    def test_empty_hypothesis_fails(self):
        data = valid_result(hypothesis="")
        with pytest.raises(ValueError, match="'hypothesis' must be a non-empty string"):
            scan._validate(data, TARGET)


# ---------------------------------------------------------------------------
# TestTargetValidation
# ---------------------------------------------------------------------------


class TestTargetValidation:
    def test_empty_target_raises(self, monkeypatch):
        monkeypatch.setattr(scan, "_invoke_bob", lambda t: None)
        monkeypatch.setattr(scan, "_fallback", lambda t: valid_result(t))

        with pytest.raises(ValueError, match="non-empty"):
            scan.run("")

    @pytest.mark.parametrize("target", SUPPORTED_TARGETS)
    def test_supported_targets_accepted(self, target, monkeypatch, tmp_path):
        monkeypatch.setattr(scan, "_invoke_bob", lambda t: None)
        monkeypatch.setattr(scan, "_fallback", lambda t: valid_result(t))
        monkeypatch.setattr(scan, "_EVIDENCE_DIR", tmp_path)

        result = scan.run(target)

        assert result["subagent"] == "static_scan"
        assert result["test_name"] == target


# ---------------------------------------------------------------------------
# TestJsonParsing
# ---------------------------------------------------------------------------


class TestJsonParsing:
    def test_parse_dict(self):
        data = valid_result()
        assert scan._parse_last_message(data) == data

    def test_parse_plain_json(self, monkeypatch):
        monkeypatch.setattr(scan, "_save_raw_response", lambda raw: None)
        data = valid_result()
        raw = json.dumps(data)
        assert scan._parse_last_message(raw) == data

    def test_parse_fenced_json(self, monkeypatch):
        monkeypatch.setattr(scan, "_save_raw_response", lambda raw: None)
        data = valid_result()
        raw = "```json\n" + json.dumps(data, indent=2) + "\n```"
        assert scan._parse_last_message(raw) == data

    def test_parse_fenced_json_without_language(self, monkeypatch):
        monkeypatch.setattr(scan, "_save_raw_response", lambda raw: None)
        data = valid_result()
        raw = "```\n" + json.dumps(data) + "\n```"
        assert scan._parse_last_message(raw) == data

    def test_parse_empty_string_fails(self, monkeypatch):
        monkeypatch.setattr(scan, "_save_raw_response", lambda raw: None)
        with pytest.raises(ValueError, match="last_message is empty"):
            scan._parse_last_message("")

    def test_parse_invalid_json_fails(self, monkeypatch):
        monkeypatch.setattr(scan, "_save_raw_response", lambda raw: None)
        with pytest.raises(ValueError, match="last_message is not valid JSON"):
            scan._parse_last_message("this is not json")

    def test_parse_non_object_json_fails(self, monkeypatch):
        monkeypatch.setattr(scan, "_save_raw_response", lambda raw: None)
        with pytest.raises(ValueError, match="last_message is not valid JSON"):
            scan._parse_last_message(json.dumps(["not", "an", "object"]))

    def test_parse_non_string_non_dict_fails(self, monkeypatch):
        monkeypatch.setattr(scan, "_save_raw_response", lambda raw: None)
        with pytest.raises(ValueError, match="last_message is not valid JSON"):
            scan._parse_last_message(42)


# ---------------------------------------------------------------------------
# TestRawResponseSaving
# ---------------------------------------------------------------------------


class TestRawResponseSaving:
    def test_save_raw_response_creates_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scan, "_DEBUG_DIR", tmp_path)
        raw = '{"subagent":"static_scan"}'
        scan._save_raw_response(raw)
        output = tmp_path / "static_scan_raw_response.txt"
        assert output.exists()
        assert output.read_text(encoding="utf-8") == raw


# ---------------------------------------------------------------------------
# TestBobInvocation
# ---------------------------------------------------------------------------


class TestBobInvocation:
    def test_bob_command_uses_run_format_json(self, monkeypatch):
        captured = {}

        monkeypatch.setattr(scan, "_resolve_bob", lambda: ["bob"])

        def fake_run(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return make_completed(
                json.dumps({"last_message": json.dumps(valid_result())})
            )

        monkeypatch.setattr(scan.subprocess, "run", fake_run)
        monkeypatch.setattr(scan, "_save_raw_response", lambda raw: None)

        result = scan._invoke_bob(TARGET)

        assert result == valid_result()
        command = captured["args"][0]
        assert command[0] == "bob"
        assert "run" in command
        assert "--format" in command
        assert "json" in command

    def test_bob_uses_devnull_stdin(self, monkeypatch):
        captured = {}

        monkeypatch.setattr(scan, "_resolve_bob", lambda: ["bob"])

        def fake_run(*args, **kwargs):
            captured["kwargs"] = kwargs
            return make_completed(
                json.dumps({"last_message": json.dumps(valid_result())})
            )

        monkeypatch.setattr(scan.subprocess, "run", fake_run)
        monkeypatch.setattr(scan, "_save_raw_response", lambda raw: None)

        scan._invoke_bob(TARGET)

        assert captured["kwargs"]["stdin"] is subprocess.DEVNULL

    def test_bob_uses_utf8_encoding(self, monkeypatch):
        captured = {}

        monkeypatch.setattr(scan, "_resolve_bob", lambda: ["bob"])

        def fake_run(*args, **kwargs):
            captured["kwargs"] = kwargs
            return make_completed(
                json.dumps({"last_message": json.dumps(valid_result())})
            )

        monkeypatch.setattr(scan.subprocess, "run", fake_run)
        monkeypatch.setattr(scan, "_save_raw_response", lambda raw: None)

        scan._invoke_bob(TARGET)

        assert captured["kwargs"]["encoding"] == "utf-8"
        assert captured["kwargs"]["errors"] == "replace"

    def test_bob_uses_project_cwd(self, monkeypatch):
        captured = {}

        monkeypatch.setattr(scan, "_resolve_bob", lambda: ["bob"])

        def fake_run(*args, **kwargs):
            captured["kwargs"] = kwargs
            return make_completed(
                json.dumps({"last_message": json.dumps(valid_result())})
            )

        monkeypatch.setattr(scan.subprocess, "run", fake_run)
        monkeypatch.setattr(scan, "_save_raw_response", lambda raw: None)

        scan._invoke_bob(TARGET)

        assert Path(captured["kwargs"]["cwd"]) == scan._HERE

    def test_missing_last_message_returns_none(self, monkeypatch):
        monkeypatch.setattr(scan, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(
            scan.subprocess,
            "run",
            lambda *a, **kw: make_completed(json.dumps({"other": "value"})),
        )

        assert scan._invoke_bob(TARGET) is None

    def test_malformed_bob_envelope_returns_none(self, monkeypatch):
        monkeypatch.setattr(scan, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(
            scan.subprocess,
            "run",
            lambda *a, **kw: make_completed("not json"),
        )

        assert scan._invoke_bob(TARGET) is None

    def test_nonzero_bob_exit_returns_none(self, monkeypatch):
        monkeypatch.setattr(scan, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(
            scan.subprocess,
            "run",
            lambda *a, **kw: make_completed("", returncode=1, stderr="bob failed"),
        )

        assert scan._invoke_bob(TARGET) is None

    def test_bob_timeout_returns_none(self, monkeypatch):
        monkeypatch.setattr(scan, "_resolve_bob", lambda: ["bob"])

        def timeout_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="bob", timeout=120)

        monkeypatch.setattr(scan.subprocess, "run", timeout_run)

        assert scan._invoke_bob(TARGET) is None

    def test_invalid_last_message_returns_none(self, monkeypatch):
        monkeypatch.setattr(scan, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(
            scan.subprocess,
            "run",
            lambda *a, **kw: make_completed(
                json.dumps({"last_message": "not json"})
            ),
        )

        assert scan._invoke_bob(TARGET) is None

    def test_bob_not_found_returns_none(self, monkeypatch):
        def raise_not_found():
            raise FileNotFoundError("Bob Shell executable was not found")

        monkeypatch.setattr(scan, "_resolve_bob", raise_not_found)

        assert scan._invoke_bob(TARGET) is None

    def test_empty_stdout_returns_none(self, monkeypatch):
        monkeypatch.setattr(scan, "_resolve_bob", lambda: ["bob"])
        monkeypatch.setattr(
            scan.subprocess,
            "run",
            lambda *a, **kw: make_completed(""),
        )

        assert scan._invoke_bob(TARGET) is None


# ---------------------------------------------------------------------------
# TestScannerOutputNormalization
# ---------------------------------------------------------------------------


class TestScannerOutputNormalization:
    def test_single_finding_preserved_in_evidence(self):
        data = scanner_output()
        result = scan._scanner_output_to_normalized(data, TARGET)

        assert result["subagent"] == "static_scan"
        assert result["test_name"] == TARGET
        assert "random_usage" in result["evidence"]
        assert result["confidence"] == "high"

    def test_empty_findings_is_valid(self):
        data = scanner_output(findings=[])
        result = scan._scanner_output_to_normalized(data, TARGET)

        assert result["subagent"] == "static_scan"
        assert result["test_name"] == TARGET
        assert "0" in result["evidence"]
        assert result["confidence"] == "low"

    def test_missing_finding_count_raises(self):
        data = {"subagent": "static_scan", "target": TARGET, "findings": []}
        with pytest.raises(ValueError, match="finding_count"):
            scan._scanner_output_to_normalized(data, TARGET)

    def test_invalid_finding_count_negative_raises(self):
        data = scanner_output()
        data["finding_count"] = -1
        with pytest.raises(ValueError, match="finding_count"):
            scan._scanner_output_to_normalized(data, TARGET)

    def test_invalid_finding_count_string_raises(self):
        data = scanner_output()
        data["finding_count"] = "one"
        with pytest.raises(ValueError, match="finding_count"):
            scan._scanner_output_to_normalized(data, TARGET)

    def test_finding_count_not_invented(self):
        """The normalized output must reflect the actual finding count."""
        findings = [
            {"file": TARGET, "line": 2, "type": "random_usage", "evidence": "imports random module"},
            {"file": TARGET, "line": 5, "type": "module_mutable_state", "evidence": "module-level mutable object: x"},
        ]
        data = scanner_output(findings=findings)
        result = scan._scanner_output_to_normalized(data, TARGET)
        assert "2" in result["evidence"]

    def test_random_usage_reflected_in_hypothesis(self):
        findings = [
            {"file": TARGET, "line": 2, "type": "random_usage", "evidence": "imports random module"}
        ]
        data = scanner_output(findings=findings)
        result = scan._scanner_output_to_normalized(data, TARGET)
        assert "random" in result["hypothesis"].lower()

    def test_async_task_reflected_in_hypothesis(self):
        findings = [
            {"file": TARGET, "line": 10, "type": "async_task", "evidence": "calls asyncio.create_task"}
        ]
        data = scanner_output(findings=findings)
        result = scan._scanner_output_to_normalized(data, TARGET)
        assert "async" in result["hypothesis"].lower() or "race" in result["hypothesis"].lower()

    def test_module_mutable_state_reflected_in_hypothesis(self):
        findings = [
            {
                "file": TARGET,
                "line": 3,
                "type": "module_mutable_state",
                "evidence": "module-level mutable object: cache",
            }
        ]
        data = scanner_output(findings=findings)
        result = scan._scanner_output_to_normalized(data, TARGET)
        assert "mutable" in result["hypothesis"].lower() or "leak" in result["hypothesis"].lower()

    def test_time_usage_reflected_in_hypothesis(self):
        findings = [
            {"file": TARGET, "line": 1, "type": "time_usage", "evidence": "imports time module"}
        ]
        data = scanner_output(findings=findings)
        result = scan._scanner_output_to_normalized(data, TARGET)
        assert "time" in result["hypothesis"].lower()

    def test_unknown_finding_type_still_generates_hypothesis(self):
        findings = [
            {"file": TARGET, "line": 5, "type": "some_unknown_type", "evidence": "something"}
        ]
        data = scanner_output(findings=findings)
        result = scan._scanner_output_to_normalized(data, TARGET)
        assert result["hypothesis"]
        assert result["confidence"] == "high"


# ---------------------------------------------------------------------------
# TestFallback
# ---------------------------------------------------------------------------


class TestFallback:
    def test_fallback_calls_actual_scanner(self, monkeypatch, tmp_path):
        script = tmp_path / "static_scan.py"
        script.write_text("", encoding="utf-8")

        monkeypatch.setattr(scan, "_SCANNER_SCRIPT", script)
        monkeypatch.setattr(scan, "_resolve_python", lambda: sys.executable)

        captured = {}

        def fake_run(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return make_completed(json.dumps(scanner_output()))

        monkeypatch.setattr(scan.subprocess, "run", fake_run)

        result = scan._fallback(TARGET)

        assert result["subagent"] == "static_scan"
        assert result["test_name"] == TARGET
        command = captured["args"][0]
        assert str(script) in command
        assert TARGET in command

    def test_fallback_uses_devnull_stdin(self, monkeypatch, tmp_path):
        script = tmp_path / "static_scan.py"
        script.write_text("", encoding="utf-8")

        monkeypatch.setattr(scan, "_SCANNER_SCRIPT", script)
        monkeypatch.setattr(scan, "_resolve_python", lambda: sys.executable)

        captured = {}

        def fake_run(*args, **kwargs):
            captured["kwargs"] = kwargs
            return make_completed(json.dumps(scanner_output()))

        monkeypatch.setattr(scan.subprocess, "run", fake_run)

        scan._fallback(TARGET)

        assert captured["kwargs"]["stdin"] is subprocess.DEVNULL

    def test_fallback_uses_utf8_encoding(self, monkeypatch, tmp_path):
        script = tmp_path / "static_scan.py"
        script.write_text("", encoding="utf-8")

        monkeypatch.setattr(scan, "_SCANNER_SCRIPT", script)
        monkeypatch.setattr(scan, "_resolve_python", lambda: sys.executable)

        captured = {}

        def fake_run(*args, **kwargs):
            captured["kwargs"] = kwargs
            return make_completed(json.dumps(scanner_output()))

        monkeypatch.setattr(scan.subprocess, "run", fake_run)

        scan._fallback(TARGET)

        assert captured["kwargs"]["encoding"] == "utf-8"
        assert captured["kwargs"]["errors"] == "replace"

    def test_fallback_missing_script_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(scan, "_SCANNER_SCRIPT", tmp_path / "nonexistent.py")

        with pytest.raises(RuntimeError, match="does not exist"):
            scan._fallback(TARGET)

    def test_fallback_nonzero_exit_raises(self, monkeypatch, tmp_path):
        script = tmp_path / "static_scan.py"
        script.write_text("", encoding="utf-8")

        monkeypatch.setattr(scan, "_SCANNER_SCRIPT", script)
        monkeypatch.setattr(scan, "_resolve_python", lambda: sys.executable)
        monkeypatch.setattr(
            scan.subprocess,
            "run",
            lambda *a, **kw: make_completed("", returncode=1, stderr="scan failed"),
        )

        with pytest.raises(RuntimeError, match="exited with code 1"):
            scan._fallback(TARGET)

    def test_fallback_empty_output_raises(self, monkeypatch, tmp_path):
        script = tmp_path / "static_scan.py"
        script.write_text("", encoding="utf-8")

        monkeypatch.setattr(scan, "_SCANNER_SCRIPT", script)
        monkeypatch.setattr(scan, "_resolve_python", lambda: sys.executable)
        monkeypatch.setattr(
            scan.subprocess,
            "run",
            lambda *a, **kw: make_completed(""),
        )

        with pytest.raises(RuntimeError, match="no output"):
            scan._fallback(TARGET)

    def test_fallback_malformed_json_raises(self, monkeypatch, tmp_path):
        script = tmp_path / "static_scan.py"
        script.write_text("", encoding="utf-8")

        monkeypatch.setattr(scan, "_SCANNER_SCRIPT", script)
        monkeypatch.setattr(scan, "_resolve_python", lambda: sys.executable)
        monkeypatch.setattr(
            scan.subprocess,
            "run",
            lambda *a, **kw: make_completed("not json"),
        )

        with pytest.raises(RuntimeError, match="not valid JSON"):
            scan._fallback(TARGET)

    def test_fallback_non_object_json_raises(self, monkeypatch, tmp_path):
        script = tmp_path / "static_scan.py"
        script.write_text("", encoding="utf-8")

        monkeypatch.setattr(scan, "_SCANNER_SCRIPT", script)
        monkeypatch.setattr(scan, "_resolve_python", lambda: sys.executable)
        monkeypatch.setattr(
            scan.subprocess,
            "run",
            lambda *a, **kw: make_completed(json.dumps(["list"])),
        )

        with pytest.raises(RuntimeError, match="must be an object"):
            scan._fallback(TARGET)

    def test_fallback_missing_finding_count_raises(self, monkeypatch, tmp_path):
        script = tmp_path / "static_scan.py"
        script.write_text("", encoding="utf-8")

        monkeypatch.setattr(scan, "_SCANNER_SCRIPT", script)
        monkeypatch.setattr(scan, "_resolve_python", lambda: sys.executable)

        bad_output = {"subagent": "static_scan", "target": TARGET, "findings": []}

        monkeypatch.setattr(
            scan.subprocess,
            "run",
            lambda *a, **kw: make_completed(json.dumps(bad_output)),
        )

        with pytest.raises((RuntimeError, ValueError), match="finding_count"):
            scan._fallback(TARGET)

    def test_fallback_preserves_actual_findings(self, monkeypatch, tmp_path):
        script = tmp_path / "static_scan.py"
        script.write_text("", encoding="utf-8")

        monkeypatch.setattr(scan, "_SCANNER_SCRIPT", script)
        monkeypatch.setattr(scan, "_resolve_python", lambda: sys.executable)

        findings = [
            {"file": TARGET, "line": 2, "type": "random_usage", "evidence": "imports random module"},
            {"file": TARGET, "line": 7, "type": "module_mutable_state", "evidence": "module-level mutable object: cache"},
        ]
        output = {"subagent": "static_scan", "target": TARGET, "findings": findings, "finding_count": 2}

        monkeypatch.setattr(
            scan.subprocess,
            "run",
            lambda *a, **kw: make_completed(json.dumps(output)),
        )

        result = scan._fallback(TARGET)

        assert "random_usage" in result["evidence"]
        assert "module_mutable_state" in result["evidence"]
        assert "2" in result["evidence"]


# ---------------------------------------------------------------------------
# TestEvidencePersistence
# ---------------------------------------------------------------------------


class TestEvidencePersistence:
    def test_evidence_saved_to_correct_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scan, "_EVIDENCE_DIR", tmp_path)

        data = valid_result()
        path = scan._persist_evidence(data, TARGET)

        assert path.exists()
        assert path.name.startswith("static_")
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved == data

    def test_evidence_filename_uses_safe_target_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scan, "_EVIDENCE_DIR", tmp_path)

        scan._persist_evidence(valid_result(), "demo-repo/src/regression.py")

        files = list(tmp_path.iterdir())
        assert any("regression" in f.name for f in files)

    @pytest.mark.parametrize("target", SUPPORTED_TARGETS)
    def test_evidence_persisted_for_all_supported_targets(self, target, tmp_path, monkeypatch):
        monkeypatch.setattr(scan, "_EVIDENCE_DIR", tmp_path)

        data = valid_result(target=target)
        path = scan._persist_evidence(data, target)

        assert path.exists()
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["test_name"] == target


# ---------------------------------------------------------------------------
# TestRun
# ---------------------------------------------------------------------------


class TestRun:
    def test_run_uses_bob_result(self, monkeypatch, tmp_path):
        data = valid_result()

        monkeypatch.setattr(scan, "_invoke_bob", lambda t: data)
        monkeypatch.setattr(scan, "_EVIDENCE_DIR", tmp_path)

        result = scan.run(TARGET)
        assert result == data

        evidence_files = list(tmp_path.iterdir())
        assert len(evidence_files) == 1
        saved = json.loads(evidence_files[0].read_text(encoding="utf-8"))
        assert saved == data

    def test_run_uses_fallback_when_bob_fails(self, monkeypatch, tmp_path):
        data = valid_result()

        monkeypatch.setattr(scan, "_invoke_bob", lambda t: None)
        monkeypatch.setattr(scan, "_fallback", lambda t: data)
        monkeypatch.setattr(scan, "_EVIDENCE_DIR", tmp_path)

        result = scan.run(TARGET)
        assert result == data

    def test_run_rejects_empty_target(self, monkeypatch):
        monkeypatch.setattr(scan, "_invoke_bob", lambda t: None)
        monkeypatch.setattr(scan, "_fallback", lambda t: valid_result(t))

        with pytest.raises(ValueError, match="non-empty"):
            scan.run("")

    def test_result_is_json_serializable(self):
        data = valid_result()
        encoded = json.dumps(data)
        decoded = json.loads(encoded)
        assert decoded == data


# ---------------------------------------------------------------------------
# TestPrompt
# ---------------------------------------------------------------------------


class TestPrompt:
    def test_prompt_substitutes_target_for_test_file(self, tmp_path, monkeypatch):
        prompt_file = tmp_path / "static_prompt.md"
        prompt_file.write_text(
            "Scan {test_file} and {test_name}.",
            encoding="utf-8",
        )
        monkeypatch.setattr(scan, "_STATIC_PROMPT", prompt_file)

        result = scan._build_static_scan_prompt(TARGET)

        assert "{test_file}" not in result
        assert "{test_name}" not in result
        assert TARGET in result

    def test_prompt_includes_scanner_command(self, tmp_path, monkeypatch):
        prompt_file = tmp_path / "static_prompt.md"
        prompt_file.write_text("Scan {test_file}.", encoding="utf-8")
        monkeypatch.setattr(scan, "_STATIC_PROMPT", prompt_file)

        result = scan._build_static_scan_prompt(TARGET)

        assert "static_scan.py" in result
        assert TARGET in result

    def test_prompt_does_not_contain_unfilled_placeholders(self):
        result = scan._build_static_scan_prompt(TARGET)
        assert "{test_file}" not in result
        assert "{test_name}" not in result


# ---------------------------------------------------------------------------
# TestBobEnvironment
# ---------------------------------------------------------------------------


class TestBobEnvironment:
    def test_load_bob_env_preserves_existing_key(self, monkeypatch):
        monkeypatch.setenv("BOB_API_KEY", "test-key")
        result = scan._load_bob_env()
        assert result["BOB_API_KEY"] == "test-key"

    def test_load_bob_env_reads_dotenv(self, monkeypatch, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text('BOB_API_KEY="from-dotenv"\n', encoding="utf-8")

        monkeypatch.setattr(scan, "_ENV_FILE", env_file)
        monkeypatch.delenv("BOB_API_KEY", raising=False)
        monkeypatch.delenv("BOBSHELL_API_KEY", raising=False)

        result = scan._load_bob_env()
        assert result["BOB_API_KEY"] == "from-dotenv"
