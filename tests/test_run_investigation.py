"""
tests/test_run_investigation.py — Step 8 tests for analyzer/run_investigation.py

Run from FlakeFinder/ directory:
    pytest tests/test_run_investigation.py

Tests cover:
  - Valid Isolation schema accepted
  - Full pytest node ID preserved in test_name
  - Missing schema fields rejected
  - Invalid confidence value rejected
  - Wrong subagent value rejected
  - Wrong test_name rejected
  - Bob JSON envelope with last_message as a JSON string
  - Bob JSON envelope with last_message as a dict
  - Bob failure activates fallback
  - Bob invalid JSON activates fallback
  - Bob missing last_message activates fallback
  - Fallback evidence is populated from actual script output (not invented)
  - Output is JSON serialisable
  - Diagnostics are on stderr, not stdout
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure FlakeFinder root is on sys.path.
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Ensure analyzer/ is importable as a package directory (no __init__ needed).
_ANALYZER = _ROOT / "analyzer"
if str(_ANALYZER) not in sys.path:
    sys.path.insert(0, str(_ANALYZER))

import run_investigation  # noqa: E402

# ---------------------------------------------------------------------------
# Canonical test node ID used across all tests
# ---------------------------------------------------------------------------

NODE_ID = "tests/test_b_race.py::test_background_update_completes"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_valid_isolation_dict(node_id: str = NODE_ID) -> dict:
    """Return a minimal valid Isolation JSON dict."""
    return {
        "subagent": "isolation",
        "test_name": node_id,
        "evidence": "runs: 10, passes: 8, failures: 2",
        "hypothesis": "The test fails ~20% of the time in isolation indicating a race condition.",
        "confidence": "high",
    }


def _bob_envelope(isolation_dict: dict) -> str:
    """Wrap an Isolation dict in a Bob Shell JSON envelope where last_message is a JSON string."""
    return json.dumps({"last_message": json.dumps(isolation_dict)})


def _bob_envelope_dict(isolation_dict: dict) -> str:
    """Wrap an Isolation dict in a Bob Shell JSON envelope where last_message is a dict."""
    return json.dumps({"last_message": isolation_dict})


def _fake_proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


def _script_json(passes: int = 8, failures: int = 2, runs: int = 10) -> str:
    """Return a run_isolated.sh-style JSON payload as a string."""
    return json.dumps({
        "subagent": "isolation",
        "test_target": NODE_ID,
        "runs": runs,
        "passes": passes,
        "failures": failures,
    })


# ---------------------------------------------------------------------------
# Tests: Schema validation function
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    """_validate() must raise ValueError on malformed dicts."""

    def test_valid_dict_accepted(self):
        run_investigation._validate(_make_valid_isolation_dict(), NODE_ID)  # must not raise

    def test_missing_subagent_raises(self):
        d = _make_valid_isolation_dict()
        del d["subagent"]
        with pytest.raises(ValueError, match="missing keys"):
            run_investigation._validate(d, NODE_ID)

    def test_missing_test_name_raises(self):
        d = _make_valid_isolation_dict()
        del d["test_name"]
        with pytest.raises(ValueError, match="missing keys"):
            run_investigation._validate(d, NODE_ID)

    def test_missing_evidence_raises(self):
        d = _make_valid_isolation_dict()
        del d["evidence"]
        with pytest.raises(ValueError, match="missing keys"):
            run_investigation._validate(d, NODE_ID)

    def test_missing_hypothesis_raises(self):
        d = _make_valid_isolation_dict()
        del d["hypothesis"]
        with pytest.raises(ValueError, match="missing keys"):
            run_investigation._validate(d, NODE_ID)

    def test_missing_confidence_raises(self):
        d = _make_valid_isolation_dict()
        del d["confidence"]
        with pytest.raises(ValueError, match="missing keys"):
            run_investigation._validate(d, NODE_ID)

    def test_wrong_subagent_raises(self):
        d = _make_valid_isolation_dict()
        d["subagent"] = "history"
        with pytest.raises(ValueError, match="subagent"):
            run_investigation._validate(d, NODE_ID)

    def test_wrong_test_name_raises(self):
        d = _make_valid_isolation_dict()
        d["test_name"] = "tests/test_a_order.py::test_cache_starts_clean"
        with pytest.raises(ValueError, match="test_name"):
            run_investigation._validate(d, NODE_ID)

    def test_empty_evidence_raises(self):
        d = _make_valid_isolation_dict()
        d["evidence"] = "   "
        with pytest.raises(ValueError, match="evidence"):
            run_investigation._validate(d, NODE_ID)

    def test_empty_hypothesis_raises(self):
        d = _make_valid_isolation_dict()
        d["hypothesis"] = ""
        with pytest.raises(ValueError, match="hypothesis"):
            run_investigation._validate(d, NODE_ID)

    def test_invalid_confidence_raises(self):
        d = _make_valid_isolation_dict()
        d["confidence"] = "very_high"
        with pytest.raises(ValueError, match="confidence"):
            run_investigation._validate(d, NODE_ID)

    def test_confidence_low_accepted(self):
        d = _make_valid_isolation_dict()
        d["confidence"] = "low"
        run_investigation._validate(d, NODE_ID)  # must not raise

    def test_confidence_medium_accepted(self):
        d = _make_valid_isolation_dict()
        d["confidence"] = "medium"
        run_investigation._validate(d, NODE_ID)  # must not raise


# ---------------------------------------------------------------------------
# Tests: Node ID preservation
# ---------------------------------------------------------------------------


class TestNodeIdPreservation:
    """The full pytest node ID must appear verbatim in test_name."""

    def test_full_node_id_preserved_via_bob(self, capsys):
        """Bob returns a valid envelope → test_name == supplied node ID."""
        isolation = _make_valid_isolation_dict()
        envelope = _bob_envelope(isolation)
        fake_proc = _fake_proc(stdout=envelope)

        with patch("run_investigation._resolve_bob", return_value=["/usr/bin/bob"]):
            with patch("run_investigation.subprocess.run", return_value=fake_proc):
                data = run_investigation.run(NODE_ID)

        assert data["test_name"] == NODE_ID

    def test_truncated_test_name_from_bob_is_corrected(self, capsys):
        """Even if Bob returns a shortened test_name, the full node ID is injected."""
        isolation = _make_valid_isolation_dict()
        isolation["test_name"] = "test_background_update_completes"  # truncated
        envelope = _bob_envelope(isolation)
        fake_proc = _fake_proc(stdout=envelope)

        with patch("run_investigation._resolve_bob", return_value=["/usr/bin/bob"]):
            with patch("run_investigation.subprocess.run", return_value=fake_proc):
                data = run_investigation.run(NODE_ID)

        assert data["test_name"] == NODE_ID


# ---------------------------------------------------------------------------
# Tests: Bob envelope parsing
# ---------------------------------------------------------------------------


class TestBobEnvelopeParsing:
    """Bob's JSON envelope must be parsed and last_message extracted."""

    def test_last_message_as_json_string(self, capsys):
        """last_message is a JSON string → parsed and used as Isolation JSON."""
        isolation = _make_valid_isolation_dict()
        envelope = _bob_envelope(isolation)  # last_message is a JSON string
        fake_proc = _fake_proc(stdout=envelope)

        with patch("run_investigation._resolve_bob", return_value=["/usr/bin/bob"]):
            with patch("run_investigation.subprocess.run", return_value=fake_proc):
                data = run_investigation.run(NODE_ID)

        run_investigation._validate(data, NODE_ID)  # must not raise
        assert data["test_name"] == NODE_ID
        captured = capsys.readouterr()
        assert "[ISOLATION] using real Bob" in captured.err

    def test_last_message_as_dict(self, capsys):
        """last_message is already a dict → used directly as Isolation JSON."""
        isolation = _make_valid_isolation_dict()
        envelope = _bob_envelope_dict(isolation)  # last_message is a dict
        fake_proc = _fake_proc(stdout=envelope)

        with patch("run_investigation._resolve_bob", return_value=["/usr/bin/bob"]):
            with patch("run_investigation.subprocess.run", return_value=fake_proc):
                data = run_investigation.run(NODE_ID)

        run_investigation._validate(data, NODE_ID)
        assert data["test_name"] == NODE_ID
        captured = capsys.readouterr()
        assert "[ISOLATION] using real Bob" in captured.err

    def test_missing_last_message_triggers_fallback(self, capsys):
        """Envelope without 'last_message' key → fallback activates."""
        bad_envelope = json.dumps({"result": "ok", "status": "done"})
        fake_proc = _fake_proc(stdout=bad_envelope)
        script_proc = _fake_proc(stdout=_script_json())

        with patch("run_investigation._resolve_bob", return_value=["/usr/bin/bob"]):
            with patch("run_investigation.subprocess.run", side_effect=[fake_proc, script_proc]):
                data = run_investigation.run(NODE_ID)

        captured = capsys.readouterr()
        assert "[ISOLATION] BOB FAILED:" in captured.err
        assert "[ISOLATION] using fallback" in captured.err
        run_investigation._validate(data, NODE_ID)

    def test_last_message_invalid_json_string_triggers_fallback(self, capsys):
        """last_message is a non-JSON string → fallback activates."""
        bad_envelope = json.dumps({"last_message": "this is not json at all"})
        fake_proc = _fake_proc(stdout=bad_envelope)
        script_proc = _fake_proc(stdout=_script_json())

        with patch("run_investigation._resolve_bob", return_value=["/usr/bin/bob"]):
            with patch("run_investigation.subprocess.run", side_effect=[fake_proc, script_proc]):
                data = run_investigation.run(NODE_ID)

        captured = capsys.readouterr()
        assert "[ISOLATION] BOB FAILED:" in captured.err
        assert "[ISOLATION] using fallback" in captured.err
        run_investigation._validate(data, NODE_ID)

    def test_last_message_unexpected_type_triggers_fallback(self, capsys):
        """last_message is a list → fallback activates."""
        bad_envelope = json.dumps({"last_message": [1, 2, 3]})
        fake_proc = _fake_proc(stdout=bad_envelope)
        script_proc = _fake_proc(stdout=_script_json())

        with patch("run_investigation._resolve_bob", return_value=["/usr/bin/bob"]):
            with patch("run_investigation.subprocess.run", side_effect=[fake_proc, script_proc]):
                data = run_investigation.run(NODE_ID)

        captured = capsys.readouterr()
        assert "[ISOLATION] BOB FAILED:" in captured.err
        assert "[ISOLATION] using fallback" in captured.err
        run_investigation._validate(data, NODE_ID)


# ---------------------------------------------------------------------------
# Tests: Bob failure modes → fallback
# ---------------------------------------------------------------------------


class TestBobFailureFallback:
    """All Bob failure modes must activate the fallback."""

    def test_bob_absent_activates_fallback(self, capsys):
        """Bob not on PATH → fallback activates silently (no BOB FAILED message)."""
        script_proc = _fake_proc(stdout=_script_json())

        with patch("run_investigation._resolve_bob", return_value=None):
            with patch("run_investigation.subprocess.run", return_value=script_proc):
                data = run_investigation.run(NODE_ID)

        captured = capsys.readouterr()
        assert "[ISOLATION] using fallback" in captured.err
        assert "[ISOLATION] BOB FAILED:" not in captured.err
        run_investigation._validate(data, NODE_ID)

    def test_bob_nonzero_exit_activates_fallback(self, capsys):
        """Bob returns non-zero exit code → fallback activates."""
        bob_proc = _fake_proc(stdout="", stderr="crash", returncode=1)
        script_proc = _fake_proc(stdout=_script_json())

        with patch("run_investigation._resolve_bob", return_value=["/usr/bin/bob"]):
            with patch("run_investigation.subprocess.run", side_effect=[bob_proc, script_proc]):
                data = run_investigation.run(NODE_ID)

        captured = capsys.readouterr()
        assert "[ISOLATION] BOB FAILED:" in captured.err
        assert "[ISOLATION] using fallback" in captured.err
        run_investigation._validate(data, NODE_ID)

    def test_bob_invalid_json_activates_fallback(self, capsys):
        """Bob returns non-JSON stdout → fallback activates."""
        bob_proc = _fake_proc(stdout="Not JSON at all.", returncode=0)
        script_proc = _fake_proc(stdout=_script_json())

        with patch("run_investigation._resolve_bob", return_value=["/usr/bin/bob"]):
            with patch("run_investigation.subprocess.run", side_effect=[bob_proc, script_proc]):
                data = run_investigation.run(NODE_ID)

        captured = capsys.readouterr()
        assert "[ISOLATION] BOB FAILED:" in captured.err
        assert "[ISOLATION] using fallback" in captured.err
        run_investigation._validate(data, NODE_ID)

    def test_bob_subprocess_exception_activates_fallback(self, capsys):
        """subprocess.run raises for Bob → fallback activates."""
        script_proc = _fake_proc(stdout=_script_json())

        call_count = [0]

        def side_effect(args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise OSError("file not found")
            return script_proc

        with patch("run_investigation._resolve_bob", return_value=["/usr/bin/bob"]):
            with patch("run_investigation.subprocess.run", side_effect=side_effect):
                data = run_investigation.run(NODE_ID)

        captured = capsys.readouterr()
        assert "[ISOLATION] BOB FAILED:" in captured.err
        assert "[ISOLATION] using fallback" in captured.err
        run_investigation._validate(data, NODE_ID)

    def test_bob_schema_validation_failure_activates_fallback(self, capsys):
        """Bob returns valid JSON but wrong subagent → schema validation fails → fallback."""
        bad_isolation = _make_valid_isolation_dict()
        bad_isolation["subagent"] = "planner"  # wrong subagent
        envelope = _bob_envelope(bad_isolation)
        bob_proc = _fake_proc(stdout=envelope)
        script_proc = _fake_proc(stdout=_script_json())

        with patch("run_investigation._resolve_bob", return_value=["/usr/bin/bob"]):
            with patch("run_investigation.subprocess.run", side_effect=[bob_proc, script_proc]):
                data = run_investigation.run(NODE_ID)

        captured = capsys.readouterr()
        assert "[ISOLATION] BOB FAILED:" in captured.err
        assert "[ISOLATION] using fallback" in captured.err
        run_investigation._validate(data, NODE_ID)


# ---------------------------------------------------------------------------
# Tests: Fallback evidence is populated from actual script output
# ---------------------------------------------------------------------------


class TestFallbackEvidence:
    """Fallback must parse the real script JSON — never invent counts."""

    def test_fallback_evidence_reflects_script_output(self, capsys):
        """Evidence string must include the real passes/failures from the script."""
        script_proc = _fake_proc(stdout=_script_json(passes=7, failures=3, runs=10))

        with patch("run_investigation._resolve_bob", return_value=None):
            with patch("run_investigation.subprocess.run", return_value=script_proc):
                data = run_investigation.run(NODE_ID)

        assert "7" in data["evidence"], f"passes=7 not reflected in evidence: {data['evidence']!r}"
        assert "3" in data["evidence"], f"failures=3 not reflected in evidence: {data['evidence']!r}"
        assert "10" in data["evidence"], f"runs=10 not reflected in evidence: {data['evidence']!r}"

    def test_fallback_evidence_all_pass(self, capsys):
        """All-pass result → hypothesis reflects stable-in-isolation finding."""
        script_proc = _fake_proc(stdout=_script_json(passes=10, failures=0, runs=10))

        with patch("run_investigation._resolve_bob", return_value=None):
            with patch("run_investigation.subprocess.run", return_value=script_proc):
                data = run_investigation.run(NODE_ID)

        assert data["confidence"] == "high"
        assert "10" in data["evidence"]
        assert data["hypothesis"] != ""

    def test_fallback_evidence_all_fail(self, capsys):
        """All-fail result → hypothesis reflects intrinsic defect."""
        script_proc = _fake_proc(stdout=_script_json(passes=0, failures=10, runs=10))

        with patch("run_investigation._resolve_bob", return_value=None):
            with patch("run_investigation.subprocess.run", return_value=script_proc):
                data = run_investigation.run(NODE_ID)

        assert data["confidence"] == "high"
        assert data["hypothesis"] != ""

    def test_fallback_evidence_is_non_empty(self, capsys):
        """Fallback evidence must be a non-empty string."""
        script_proc = _fake_proc(stdout=_script_json())

        with patch("run_investigation._resolve_bob", return_value=None):
            with patch("run_investigation.subprocess.run", return_value=script_proc):
                data = run_investigation.run(NODE_ID)

        assert isinstance(data["evidence"], str)
        assert data["evidence"].strip() != ""


# ---------------------------------------------------------------------------
# Tests: Output JSON serialisability
# ---------------------------------------------------------------------------


class TestOutputSerialisability:
    """run() return value must be JSON-serialisable."""

    def test_bob_result_is_json_serialisable(self, capsys):
        isolation = _make_valid_isolation_dict()
        envelope = _bob_envelope(isolation)
        fake_proc = _fake_proc(stdout=envelope)

        with patch("run_investigation._resolve_bob", return_value=["/usr/bin/bob"]):
            with patch("run_investigation.subprocess.run", return_value=fake_proc):
                data = run_investigation.run(NODE_ID)

        serialised = json.dumps(data)
        roundtripped = json.loads(serialised)
        assert roundtripped["test_name"] == NODE_ID

    def test_fallback_result_is_json_serialisable(self):
        script_proc = _fake_proc(stdout=_script_json())

        with patch("run_investigation._resolve_bob", return_value=None):
            with patch("run_investigation.subprocess.run", return_value=script_proc):
                data = run_investigation.run(NODE_ID)

        serialised = json.dumps(data)
        roundtripped = json.loads(serialised)
        assert roundtripped["test_name"] == NODE_ID


# ---------------------------------------------------------------------------
# Tests: Diagnostics on stderr, stdout stays clean
# ---------------------------------------------------------------------------


class TestStderrStdoutSeparation:
    """[ISOLATION] diagnostics must go to stderr; stdout must stay machine-readable."""

    def test_using_real_bob_on_stderr_not_stdout(self, capsys):
        isolation = _make_valid_isolation_dict()
        envelope = _bob_envelope(isolation)
        fake_proc = _fake_proc(stdout=envelope)

        with patch("run_investigation._resolve_bob", return_value=["/usr/bin/bob"]):
            with patch("run_investigation.subprocess.run", return_value=fake_proc):
                run_investigation.run(NODE_ID)

        captured = capsys.readouterr()
        assert "[ISOLATION] using real Bob" in captured.err
        assert "[ISOLATION]" not in captured.out

    def test_using_fallback_on_stderr_not_stdout(self, capsys):
        script_proc = _fake_proc(stdout=_script_json())

        with patch("run_investigation._resolve_bob", return_value=None):
            with patch("run_investigation.subprocess.run", return_value=script_proc):
                run_investigation.run(NODE_ID)

        captured = capsys.readouterr()
        assert "[ISOLATION] using fallback" in captured.err
        assert "[ISOLATION]" not in captured.out

    def test_run_return_value_has_no_stderr_content(self, capsys):
        """run() return value is a plain dict — not a string with diagnostic text."""
        script_proc = _fake_proc(stdout=_script_json())

        with patch("run_investigation._resolve_bob", return_value=None):
            with patch("run_investigation.subprocess.run", return_value=script_proc):
                data = run_investigation.run(NODE_ID)

        assert isinstance(data, dict)
        assert "[ISOLATION]" not in json.dumps(data)


# ---------------------------------------------------------------------------
# Tests: Bob subprocess call contract
# ---------------------------------------------------------------------------


class TestBobSubprocessContract:
    """The Bob subprocess must use stdin=DEVNULL and `bob run --format json`."""

    def test_bob_called_with_run_format_json(self):
        isolation = _make_valid_isolation_dict()
        envelope = _bob_envelope(isolation)
        fake_proc = _fake_proc(stdout=envelope)

        captured_args = []

        def fake_subprocess_run(args, **kwargs):
            captured_args.append(args)
            return fake_proc

        with patch("run_investigation._resolve_bob", return_value=["/usr/bin/bob"]):
            with patch("run_investigation.subprocess.run", side_effect=fake_subprocess_run):
                run_investigation.run(NODE_ID)

        bob_calls = [a for a in captured_args if a and "bob" in str(a[0])]
        assert bob_calls, "No bob subprocess.run call found"
        bob_args = bob_calls[0]
        assert "run" in bob_args, "'run' subcommand missing from bob call"
        assert "--format" in bob_args, "'--format' flag missing"
        assert "json" in bob_args, "'json' value missing"

    def test_bob_called_with_devnull_stdin(self):
        isolation = _make_valid_isolation_dict()
        envelope = _bob_envelope(isolation)
        fake_proc = _fake_proc(stdout=envelope)

        captured_kwargs = []

        def fake_subprocess_run(args, **kwargs):
            if args and "bob" in str(args[0]) and "run" in args:
                captured_kwargs.append(kwargs)
            return fake_proc

        with patch("run_investigation._resolve_bob", return_value=["/usr/bin/bob"]):
            with patch("run_investigation.subprocess.run", side_effect=fake_subprocess_run):
                run_investigation.run(NODE_ID)

        assert captured_kwargs, "No bob call captured"
        for kw in captured_kwargs:
            assert kw.get("stdin") is subprocess.DEVNULL, (
                f"Expected stdin=subprocess.DEVNULL, got {kw.get('stdin')!r}"
            )

    def test_bob_not_called_with_input_kwarg(self):
        isolation = _make_valid_isolation_dict()
        envelope = _bob_envelope(isolation)
        fake_proc = _fake_proc(stdout=envelope)

        captured_kwargs = []

        def fake_subprocess_run(args, **kwargs):
            if args and "bob" in str(args[0]) and "run" in args:
                captured_kwargs.append(kwargs)
            return fake_proc

        with patch("run_investigation._resolve_bob", return_value=["/usr/bin/bob"]):
            with patch("run_investigation.subprocess.run", side_effect=fake_subprocess_run):
                run_investigation.run(NODE_ID)

        for kw in captured_kwargs:
            assert "input" not in kw, "Bob must not be called with input= kwarg"


# ---------------------------------------------------------------------------
# Tests: run() rejects invalid node ID
# ---------------------------------------------------------------------------


class TestNodeIdValidation:
    """run() must raise ValueError when node_id is not a full pytest node ID."""

    def test_missing_double_colon_raises(self):
        with pytest.raises(ValueError, match="full pytest node ID"):
            run_investigation.run("tests/test_b_race.py")

    def test_bare_function_name_raises(self):
        with pytest.raises(ValueError, match="full pytest node ID"):
            run_investigation.run("test_background_update_completes")


# ---------------------------------------------------------------------------
# Tests: _fallback() raises on bad script output (never invents counts)
# ---------------------------------------------------------------------------


class TestFallbackRaisesOnBadScriptOutput:
    """_fallback() must raise RuntimeError instead of fabricating defaults."""

    def test_subprocess_exception_raises_runtime_error(self):
        """If subprocess.run raises (e.g. FileNotFoundError), RuntimeError is raised."""
        with patch("run_investigation.subprocess.run", side_effect=OSError("bash not found")):
            with pytest.raises(RuntimeError, match="fallback script failed to run"):
                run_investigation._fallback(NODE_ID)

    def test_nonzero_exit_code_raises_runtime_error(self):
        """Non-zero exit from run_isolated.sh → RuntimeError, no invented counts."""
        bad_proc = _fake_proc(stdout="", stderr="script crashed", returncode=1)
        with patch("run_investigation.subprocess.run", return_value=bad_proc):
            with pytest.raises(RuntimeError, match="fallback script exited with code 1"):
                run_investigation._fallback(NODE_ID)

    def test_empty_stdout_raises_runtime_error(self):
        """Script produces no output → RuntimeError, not silent default."""
        empty_proc = _fake_proc(stdout="", returncode=0)
        with patch("run_investigation.subprocess.run", return_value=empty_proc):
            with pytest.raises(RuntimeError, match="no output"):
                run_investigation._fallback(NODE_ID)

    def test_invalid_json_stdout_raises_runtime_error(self):
        """Script output is not valid JSON → RuntimeError with descriptive message."""
        bad_proc = _fake_proc(stdout="not valid json {{", returncode=0)
        with patch("run_investigation.subprocess.run", return_value=bad_proc):
            with pytest.raises(RuntimeError, match="not valid JSON"):
                run_investigation._fallback(NODE_ID)

    def test_missing_runs_key_raises_runtime_error(self):
        """Script JSON missing 'runs' key → RuntimeError, no defaulting."""
        incomplete = json.dumps({"passes": 8, "failures": 2})  # no 'runs'
        bad_proc = _fake_proc(stdout=incomplete, returncode=0)
        with patch("run_investigation.subprocess.run", return_value=bad_proc):
            with pytest.raises(RuntimeError, match="missing required keys"):
                run_investigation._fallback(NODE_ID)

    def test_missing_passes_key_raises_runtime_error(self):
        """Script JSON missing 'passes' key → RuntimeError."""
        incomplete = json.dumps({"runs": 10, "failures": 2})  # no 'passes'
        bad_proc = _fake_proc(stdout=incomplete, returncode=0)
        with patch("run_investigation.subprocess.run", return_value=bad_proc):
            with pytest.raises(RuntimeError, match="missing required keys"):
                run_investigation._fallback(NODE_ID)

    def test_missing_failures_key_raises_runtime_error(self):
        """Script JSON missing 'failures' key → RuntimeError."""
        incomplete = json.dumps({"runs": 10, "passes": 8})  # no 'failures'
        bad_proc = _fake_proc(stdout=incomplete, returncode=0)
        with patch("run_investigation.subprocess.run", return_value=bad_proc):
            with pytest.raises(RuntimeError, match="missing required keys"):
                run_investigation._fallback(NODE_ID)

    def test_empty_json_object_raises_runtime_error(self):
        """Script returns '{}' (all keys missing) → RuntimeError."""
        bad_proc = _fake_proc(stdout="{}", returncode=0)
        with patch("run_investigation.subprocess.run", return_value=bad_proc):
            with pytest.raises(RuntimeError, match="missing required keys"):
                run_investigation._fallback(NODE_ID)

    def test_timeout_raises_runtime_error(self):
        """Subprocess timeout → RuntimeError propagated."""
        with patch(
            "run_investigation.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="bash", timeout=180),
        ):
            with pytest.raises(RuntimeError, match="fallback script failed to run"):
                run_investigation._fallback(NODE_ID)


# ---------------------------------------------------------------------------
# Tests: _find_bash() — Git Bash selection on Windows vs non-Windows
# ---------------------------------------------------------------------------


class TestFindBash:
    """_find_bash() must prefer Git Bash on Windows and fall back correctly."""

    def test_windows_git_bash_exists_is_preferred(self, tmp_path):
        """On Windows, if the Git Bash exe exists it must be returned."""
        fake_git_bash = MagicMock(spec=Path)
        fake_git_bash.exists.return_value = True
        fake_git_bash.__str__ = MagicMock(return_value="C:/Program Files/Git/bin/bash.exe")

        with patch("run_investigation.os.name", "nt"):
            with patch("run_investigation._GIT_BASH_DEFAULT", fake_git_bash):
                result = run_investigation._find_bash()

        assert result == "C:/Program Files/Git/bin/bash.exe"

    def test_windows_git_bash_missing_falls_back_to_which(self):
        """On Windows, if Git Bash is absent, shutil.which('bash') is used."""
        fake_git_bash = MagicMock(spec=Path)
        fake_git_bash.exists.return_value = False

        with patch("run_investigation.os.name", "nt"):
            with patch("run_investigation._GIT_BASH_DEFAULT", fake_git_bash):
                with patch("run_investigation.shutil.which", return_value="/some/other/bash"):
                    result = run_investigation._find_bash()

        assert result == "/some/other/bash"

    def test_windows_git_bash_missing_and_no_which_returns_bare_bash(self):
        """On Windows, if Git Bash absent and shutil.which returns None, return 'bash'."""
        fake_git_bash = MagicMock(spec=Path)
        fake_git_bash.exists.return_value = False

        with patch("run_investigation.os.name", "nt"):
            with patch("run_investigation._GIT_BASH_DEFAULT", fake_git_bash):
                with patch("run_investigation.shutil.which", return_value=None):
                    result = run_investigation._find_bash()

        assert result == "bash"

    def test_non_windows_uses_which(self):
        """On non-Windows, shutil.which('bash') result is returned directly."""
        with patch("run_investigation.os.name", "posix"):
            with patch("run_investigation.shutil.which", return_value="/usr/bin/bash"):
                result = run_investigation._find_bash()

        assert result == "/usr/bin/bash"

    def test_non_windows_which_returns_none_falls_back_to_bare_bash(self):
        """On non-Windows, if shutil.which returns None, return 'bash'."""
        with patch("run_investigation.os.name", "posix"):
            with patch("run_investigation.shutil.which", return_value=None):
                result = run_investigation._find_bash()

        assert result == "bash"

    def test_find_bash_result_used_in_fallback_subprocess_call(self):
        """_fallback() must pass _find_bash()'s return value as the first arg to subprocess.run."""
        script_proc = _fake_proc(stdout=_script_json())
        captured_args = []

        def fake_run(args, **kwargs):
            captured_args.append(args)
            return script_proc

        with patch("run_investigation._find_bash", return_value="/custom/bash"):
            with patch("run_investigation.subprocess.run", side_effect=fake_run):
                run_investigation._fallback(NODE_ID)

        assert captured_args, "subprocess.run was not called"
        assert captured_args[0][0] == "/custom/bash", (
            f"Expected '/custom/bash' as first arg, got {captured_args[0][0]!r}"
        )

    def test_windows_git_bash_not_used_when_os_is_posix(self):
        """On POSIX, the Windows Git Bash path must never be selected."""
        with patch("run_investigation.os.name", "posix"):
            with patch("run_investigation.shutil.which", return_value="/usr/bin/bash"):
                result = run_investigation._find_bash()

        assert "Program Files" not in result
