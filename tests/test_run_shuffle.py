"""
tests/test_run_shuffle.py — Step 9a tests for analyzer/run_shuffle.py
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_ANALYZER = _ROOT / "analyzer"
if str(_ANALYZER) not in sys.path:
    sys.path.insert(0, str(_ANALYZER))

import run_shuffle  # noqa: E402


NODE_ID = "tests/test_a_order.py::test_cache_starts_clean"


def _make_valid_shuffle_dict(node_id: str = NODE_ID) -> dict:
    return {
        "subagent": "shuffle",
        "test_name": node_id,
        "evidence": "10 runs executed. 6 passed, 4 failed.",
        "hypothesis": (
            "The test is order-dependent due to persistent module-level "
            "state. Immediate predecessors are not necessarily the cause."
        ),
        "confidence": "high",
    }


def _bob_envelope(shuffle_dict: dict) -> str:
    return json.dumps({"last_message": json.dumps(shuffle_dict)})


def _bob_envelope_dict(shuffle_dict: dict) -> str:
    return json.dumps({"last_message": shuffle_dict})


def _fake_proc(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> MagicMock:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


def _script_json(
    passes: int = 6,
    failures: int = 4,
    runs: int = 10,
) -> str:
    return json.dumps(
        {
            "runs": runs,
            "passes": passes,
            "failures": failures,
        }
    )


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidation:

    def test_valid_dict_accepted(self):
        run_shuffle._validate(_make_valid_shuffle_dict(), NODE_ID)

    def test_missing_subagent_raises(self):
        data = _make_valid_shuffle_dict()
        del data["subagent"]

        with pytest.raises(ValueError, match="missing keys"):
            run_shuffle._validate(data, NODE_ID)

    def test_missing_test_name_raises(self):
        data = _make_valid_shuffle_dict()
        del data["test_name"]

        with pytest.raises(ValueError, match="missing keys"):
            run_shuffle._validate(data, NODE_ID)

    def test_missing_evidence_raises(self):
        data = _make_valid_shuffle_dict()
        del data["evidence"]

        with pytest.raises(ValueError, match="missing keys"):
            run_shuffle._validate(data, NODE_ID)

    def test_missing_hypothesis_raises(self):
        data = _make_valid_shuffle_dict()
        del data["hypothesis"]

        with pytest.raises(ValueError, match="missing keys"):
            run_shuffle._validate(data, NODE_ID)

    def test_missing_confidence_raises(self):
        data = _make_valid_shuffle_dict()
        del data["confidence"]

        with pytest.raises(ValueError, match="missing keys"):
            run_shuffle._validate(data, NODE_ID)

    def test_wrong_subagent_raises(self):
        data = _make_valid_shuffle_dict()
        data["subagent"] = "isolation"

        with pytest.raises(ValueError, match="subagent"):
            run_shuffle._validate(data, NODE_ID)

    def test_wrong_test_name_raises(self):
        data = _make_valid_shuffle_dict()
        data["test_name"] = "wrong::test"

        with pytest.raises(ValueError, match="test_name"):
            run_shuffle._validate(data, NODE_ID)

    def test_empty_evidence_raises(self):
        data = _make_valid_shuffle_dict()
        data["evidence"] = " "

        with pytest.raises(ValueError, match="evidence"):
            run_shuffle._validate(data, NODE_ID)

    def test_empty_hypothesis_raises(self):
        data = _make_valid_shuffle_dict()
        data["hypothesis"] = ""

        with pytest.raises(ValueError, match="hypothesis"):
            run_shuffle._validate(data, NODE_ID)

    def test_invalid_confidence_raises(self):
        data = _make_valid_shuffle_dict()
        data["confidence"] = "very_high"

        with pytest.raises(ValueError, match="confidence"):
            run_shuffle._validate(data, NODE_ID)


# ---------------------------------------------------------------------------
# Bob envelope parsing
# ---------------------------------------------------------------------------


class TestBobEnvelopeParsing:

    def test_last_message_as_json_string(self, capsys):
        shuffle = _make_valid_shuffle_dict()
        envelope = _bob_envelope(shuffle)
        fake_proc = _fake_proc(stdout=envelope)

        with patch(
            "run_shuffle._resolve_bob",
            return_value=["/usr/bin/bob"],
        ):
            with patch(
                "run_shuffle.subprocess.run",
                return_value=fake_proc,
            ):
                data = run_shuffle.run(NODE_ID)

        run_shuffle._validate(data, NODE_ID)

        captured = capsys.readouterr()

        assert data["test_name"] == NODE_ID
        assert "[SHUFFLE] using real Bob" in captured.err

    def test_last_message_as_dict(self, capsys):
        shuffle = _make_valid_shuffle_dict()
        envelope = _bob_envelope_dict(shuffle)
        fake_proc = _fake_proc(stdout=envelope)

        with patch(
            "run_shuffle._resolve_bob",
            return_value=["/usr/bin/bob"],
        ):
            with patch(
                "run_shuffle.subprocess.run",
                return_value=fake_proc,
            ):
                data = run_shuffle.run(NODE_ID)

        run_shuffle._validate(data, NODE_ID)

        captured = capsys.readouterr()

        assert data["test_name"] == NODE_ID
        assert "[SHUFFLE] using real Bob" in captured.err

    def test_fenced_json_is_accepted(self):
        shuffle = _make_valid_shuffle_dict()

        fenced = "```json\n" + json.dumps(shuffle, indent=2) + "\n```"
        envelope = json.dumps({"last_message": fenced})
        fake_proc = _fake_proc(stdout=envelope)

        with patch(
            "run_shuffle._resolve_bob",
            return_value=["/usr/bin/bob"],
        ):
            with patch(
                "run_shuffle.subprocess.run",
                return_value=fake_proc,
            ):
                data = run_shuffle.run(NODE_ID)

        assert data["subagent"] == "shuffle"
        assert data["test_name"] == NODE_ID

    def test_missing_last_message_triggers_fallback(self, capsys):
        bad_envelope = json.dumps({"status": "done"})
        bob_proc = _fake_proc(stdout=bad_envelope)
        script_proc = _fake_proc(stdout=_script_json())

        with patch(
            "run_shuffle._resolve_bob",
            return_value=["/usr/bin/bob"],
        ):
            with patch(
                "run_shuffle.subprocess.run",
                side_effect=[bob_proc, script_proc],
            ):
                data = run_shuffle.run(NODE_ID)

        captured = capsys.readouterr()

        assert "[SHUFFLE] BOB FAILED:" in captured.err
        assert "[SHUFFLE] using fallback" in captured.err
        run_shuffle._validate(data, NODE_ID)

    def test_invalid_last_message_triggers_fallback(self, capsys):
        bad_envelope = json.dumps(
            {"last_message": "this is not JSON"}
        )
        bob_proc = _fake_proc(stdout=bad_envelope)
        script_proc = _fake_proc(stdout=_script_json())

        with patch(
            "run_shuffle._resolve_bob",
            return_value=["/usr/bin/bob"],
        ):
            with patch(
                "run_shuffle.subprocess.run",
                side_effect=[bob_proc, script_proc],
            ):
                data = run_shuffle.run(NODE_ID)

        captured = capsys.readouterr()

        assert "[SHUFFLE] BOB FAILED:" in captured.err
        assert "[SHUFFLE] using fallback" in captured.err
        run_shuffle._validate(data, NODE_ID)


# ---------------------------------------------------------------------------
# Bob failure → fallback
# ---------------------------------------------------------------------------


class TestBobFailureFallback:

    def test_bob_absent_activates_fallback(self, capsys):
        script_proc = _fake_proc(stdout=_script_json())

        with patch(
            "run_shuffle._resolve_bob",
            return_value=None,
        ):
            with patch(
                "run_shuffle.subprocess.run",
                return_value=script_proc,
            ):
                data = run_shuffle.run(NODE_ID)

        captured = capsys.readouterr()

        assert "[SHUFFLE] using fallback" in captured.err
        run_shuffle._validate(data, NODE_ID)

    def test_bob_nonzero_exit_activates_fallback(self, capsys):
        bob_proc = _fake_proc(
            stderr="crash",
            returncode=1,
        )
        script_proc = _fake_proc(stdout=_script_json())

        with patch(
            "run_shuffle._resolve_bob",
            return_value=["/usr/bin/bob"],
        ):
            with patch(
                "run_shuffle.subprocess.run",
                side_effect=[bob_proc, script_proc],
            ):
                data = run_shuffle.run(NODE_ID)

        captured = capsys.readouterr()

        assert "[SHUFFLE] BOB FAILED:" in captured.err
        assert "[SHUFFLE] using fallback" in captured.err
        run_shuffle._validate(data, NODE_ID)

    def test_bob_invalid_json_activates_fallback(self, capsys):
        bob_proc = _fake_proc(stdout="not JSON")
        script_proc = _fake_proc(stdout=_script_json())

        with patch(
            "run_shuffle._resolve_bob",
            return_value=["/usr/bin/bob"],
        ):
            with patch(
                "run_shuffle.subprocess.run",
                side_effect=[bob_proc, script_proc],
            ):
                data = run_shuffle.run(NODE_ID)

        captured = capsys.readouterr()

        assert "[SHUFFLE] BOB FAILED:" in captured.err
        assert "[SHUFFLE] using fallback" in captured.err
        run_shuffle._validate(data, NODE_ID)


# ---------------------------------------------------------------------------
# Fallback evidence — never invent counts
# ---------------------------------------------------------------------------


class TestFallbackEvidence:

    def test_fallback_uses_actual_script_counts(self):
        script_proc = _fake_proc(
            stdout=_script_json(
                passes=7,
                failures=3,
                runs=10,
            )
        )

        with patch(
            "run_shuffle._resolve_bob",
            return_value=None,
        ):
            with patch(
                "run_shuffle.subprocess.run",
                return_value=script_proc,
            ):
                data = run_shuffle.run(NODE_ID)

        assert "7" in data["evidence"]
        assert "3" in data["evidence"]
        assert "10" in data["evidence"]

    def test_fallback_missing_runs_raises(self):
        script_proc = _fake_proc(
            stdout=json.dumps(
                {"passes": 7, "failures": 3}
            )
        )

        with patch(
            "run_shuffle.subprocess.run",
            return_value=script_proc,
        ):
            with pytest.raises(
                RuntimeError,
                match="missing required keys",
            ):
                run_shuffle._fallback(NODE_ID)

    def test_fallback_missing_passes_raises(self):
        script_proc = _fake_proc(
            stdout=json.dumps(
                {"runs": 10, "failures": 3}
            )
        )

        with patch(
            "run_shuffle.subprocess.run",
            return_value=script_proc,
        ):
            with pytest.raises(
                RuntimeError,
                match="missing required keys",
            ):
                run_shuffle._fallback(NODE_ID)

    def test_fallback_missing_failures_raises(self):
        script_proc = _fake_proc(
            stdout=json.dumps(
                {"runs": 10, "passes": 7}
            )
        )

        with patch(
            "run_shuffle.subprocess.run",
            return_value=script_proc,
        ):
            with pytest.raises(
                RuntimeError,
                match="missing required keys",
            ):
                run_shuffle._fallback(NODE_ID)

    def test_fallback_empty_output_raises(self):
        script_proc = _fake_proc(stdout="")

        with patch(
            "run_shuffle.subprocess.run",
            return_value=script_proc,
        ):
            with pytest.raises(
                RuntimeError,
                match="no output",
            ):
                run_shuffle._fallback(NODE_ID)

    def test_fallback_invalid_json_raises(self):
        script_proc = _fake_proc(
            stdout="not valid JSON"
        )

        with patch(
            "run_shuffle.subprocess.run",
            return_value=script_proc,
        ):
            with pytest.raises(
                RuntimeError,
                match="not valid JSON",
            ):
                run_shuffle._fallback(NODE_ID)


# ---------------------------------------------------------------------------
# Bob subprocess contract
# ---------------------------------------------------------------------------


class TestBobSubprocessContract:

    def test_bob_called_with_run_format_json(self):
        shuffle = _make_valid_shuffle_dict()
        envelope = _bob_envelope(shuffle)
        fake_proc = _fake_proc(stdout=envelope)

        captured = []

        def fake_run(args, **kwargs):
            captured.append((args, kwargs))
            return fake_proc

        with patch(
            "run_shuffle._resolve_bob",
            return_value=["/usr/bin/bob"],
        ):
            with patch(
                "run_shuffle.subprocess.run",
                side_effect=fake_run,
            ):
                run_shuffle.run(NODE_ID)

        bob_calls = [
            item for item in captured
            if item[0] and "bob" in str(item[0][0])
        ]

        assert bob_calls

        args = bob_calls[0][0]

        assert "run" in args
        assert "--format" in args
        assert "json" in args

    def test_bob_uses_devnull_stdin(self):
        shuffle = _make_valid_shuffle_dict()
        envelope = _bob_envelope(shuffle)
        fake_proc = _fake_proc(stdout=envelope)

        captured_kwargs = []

        def fake_run(args, **kwargs):
            if args and "bob" in str(args[0]) and "run" in args:
                captured_kwargs.append(kwargs)
            return fake_proc

        with patch(
            "run_shuffle._resolve_bob",
            return_value=["/usr/bin/bob"],
        ):
            with patch(
                "run_shuffle.subprocess.run",
                side_effect=fake_run,
            ):
                run_shuffle.run(NODE_ID)

        assert captured_kwargs

        for kwargs in captured_kwargs:
            assert kwargs.get("stdin") is subprocess.DEVNULL
            assert "input" not in kwargs


# ---------------------------------------------------------------------------
# Windows Git Bash
# ---------------------------------------------------------------------------


class TestFindBash:

    def test_windows_prefers_git_bash(self):
        fake_git_bash = MagicMock(spec=Path)
        fake_git_bash.exists.return_value = True
        fake_git_bash.__str__ = MagicMock(
            return_value="C:/Program Files/Git/bin/bash.exe"
        )

        with patch("run_shuffle.os.name", "nt"):
            with patch(
                "run_shuffle._GIT_BASH_DEFAULT",
                fake_git_bash,
            ):
                result = run_shuffle._find_bash()

        assert result == "C:/Program Files/Git/bin/bash.exe"

    def test_windows_missing_git_bash_uses_which(self):
        fake_git_bash = MagicMock(spec=Path)
        fake_git_bash.exists.return_value = False

        with patch("run_shuffle.os.name", "nt"):
            with patch(
                "run_shuffle._GIT_BASH_DEFAULT",
                fake_git_bash,
            ):
                with patch(
                    "run_shuffle.shutil.which",
                    return_value="/some/bash",
                ):
                    assert run_shuffle._find_bash() == "/some/bash"


# ---------------------------------------------------------------------------
# Node ID validation
# ---------------------------------------------------------------------------


class TestNodeIdValidation:

    def test_missing_double_colon_raises(self):
        with pytest.raises(
            ValueError,
            match="full pytest node ID",
        ):
            run_shuffle.run("tests/test_a_order.py")

    def test_bare_function_name_raises(self):
        with pytest.raises(
            ValueError,
            match="full pytest node ID",
        ):
            run_shuffle.run("test_cache_starts_clean")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


class TestOutput:

    def test_result_is_json_serialisable(self):
        shuffle = _make_valid_shuffle_dict()
        envelope = _bob_envelope(shuffle)
        fake_proc = _fake_proc(stdout=envelope)

        with patch(
            "run_shuffle._resolve_bob",
            return_value=["/usr/bin/bob"],
        ):
            with patch(
                "run_shuffle.subprocess.run",
                return_value=fake_proc,
            ):
                data = run_shuffle.run(NODE_ID)

        roundtripped = json.loads(json.dumps(data))

        assert roundtripped["subagent"] == "shuffle"
        assert roundtripped["test_name"] == NODE_ID