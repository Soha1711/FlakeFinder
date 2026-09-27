"""
tests/test_run_planner.py — Step 6 tests for run_planner.py

Run from FlakeFinder/ directory:
    pytest tests/test_run_planner.py

Tests cover:
  - Schema validation
  - Full pytest node ID preservation
  - Fallback behavior (Bob absent / failed)
  - Bob Shell invocation: `bob run --format json "<prompt>"`
  - Bob Shell JSON envelope parsing (last_message extraction)
  - Hypothesis normalization (allowed enum values only)
  - [PLANNER] diagnostic messages on stderr
  - stdout remains valid JSON while diagnostics go to stderr
  - Bob Shell failure modes → fallback activation
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure FlakeFinder root is on sys.path so `import run_planner` works.
_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import run_planner  # noqa: E402

# ---------------------------------------------------------------------------
# Canonical test node ID used across all tests
# ---------------------------------------------------------------------------

NODE_ID = "tests/test_a_order.py::test_cache_starts_clean"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call_run(node_id: str = NODE_ID) -> dict:
    """Call run_planner.run() and return the validated dict."""
    return run_planner.run(node_id)


def _make_valid_planner_dict(node_id: str = NODE_ID) -> dict:
    """Return a minimal valid Planner JSON dict."""
    return {
        "test_name": node_id,
        "test_file": node_id.split("::")[0],
        "hypotheses": ["order_dependency"],
        "search_targets": ["tests/test_a_order.py", "src/shared_cache.py"],
    }


def _bob_envelope(planner_dict: dict) -> str:
    """Wrap a Planner dict in a Bob Shell JSON envelope with last_message."""
    return json.dumps({"last_message": json.dumps(planner_dict)})


def _bob_envelope_dict(planner_dict: dict) -> str:
    """Wrap a Planner dict in a Bob Shell JSON envelope where last_message is a dict."""
    return json.dumps({"last_message": planner_dict})


def _fake_proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


# ---------------------------------------------------------------------------
# Tests: Output schema
# ---------------------------------------------------------------------------


class TestOutputSchema:
    """The output must satisfy the Planner JSON schema exactly."""

    def test_output_schema_valid(self):
        data = _call_run()
        required = {"test_name", "test_file", "hypotheses", "search_targets"}
        assert required == set(data.keys()), (
            f"Expected keys {sorted(required)}, got {sorted(data.keys())}"
        )

    def test_hypotheses_not_empty(self):
        data = _call_run()
        assert isinstance(data["hypotheses"], list)
        assert len(data["hypotheses"]) >= 1, "hypotheses must be a non-empty list"

    def test_search_targets_not_empty(self):
        data = _call_run()
        assert isinstance(data["search_targets"], list)
        assert len(data["search_targets"]) >= 1, "search_targets must be a non-empty list"


# ---------------------------------------------------------------------------
# Tests: Node ID preservation
# ---------------------------------------------------------------------------


class TestNodeIdPreservation:
    """The full pytest node ID must be preserved verbatim in test_name."""

    def test_full_node_id_preserved(self):
        data = _call_run()
        assert data["test_name"] == NODE_ID, (
            f"Expected test_name={NODE_ID!r}, got {data['test_name']!r}"
        )

    def test_test_file_derived_correctly(self):
        data = _call_run()
        expected_file = NODE_ID.split("::")[0]
        assert data["test_file"] == expected_file, (
            f"Expected test_file={expected_file!r}, got {data['test_file']!r}"
        )

    def test_node_id_not_truncated_to_function_name(self):
        """test_name must include the file path, not just the function name."""
        data = _call_run()
        assert "/" in data["test_name"] or "\\" in data["test_name"], (
            f"test_name looks truncated (no path separator): {data['test_name']!r}"
        )
        assert "::" in data["test_name"], (
            f"test_name must contain '::' separator: {data['test_name']!r}"
        )


# ---------------------------------------------------------------------------
# Tests: Fallback behavior
# ---------------------------------------------------------------------------


class TestFallbackBehavior:
    """The fallback must produce valid output when Bob is absent."""

    def test_fallback_runs_when_bob_absent(self):
        """With bob patched off PATH, fallback runs and returns valid JSON."""
        with patch("run_planner.shutil.which", return_value=None):
            data = run_planner.run(NODE_ID)
        run_planner._validate(data)  # must not raise
        assert data["test_name"] == NODE_ID

    def test_fallback_result_is_serialisable(self):
        """Fallback output must be JSON-serialisable (no non-JSON types)."""
        with patch("run_planner.shutil.which", return_value=None):
            data = run_planner.run(NODE_ID)
        # json.dumps must not raise
        serialised = json.dumps(data)
        roundtripped = json.loads(serialised)
        assert roundtripped["test_name"] == NODE_ID


# ---------------------------------------------------------------------------
# Tests: Bob Shell invocation format
# ---------------------------------------------------------------------------


class TestBobShellInvocation:
    """_invoke_bob must call `bob run --format json "<prompt>"` (not pipe stdin)."""

    def test_bob_called_with_run_format_json(self):
        """subprocess.run must be invoked with ['bob', 'run', '--format', 'json', <prompt>]."""
        planner = _make_valid_planner_dict()
        envelope = _bob_envelope(planner)
        fake_proc = _fake_proc(stdout=envelope)

        captured_args = []

        def fake_subprocess_run(args, **kwargs):
            captured_args.append(args)
            return fake_proc

        with patch("run_planner.shutil.which", return_value="/usr/local/bin/bob"):
            with patch("run_planner.subprocess.run", side_effect=fake_subprocess_run):
                run_planner.run(NODE_ID)

        # Filter to calls that look like bob invocations (git calls also use subprocess.run)
        bob_calls = [a for a in captured_args if a and "bob" in str(a[0])]
        assert bob_calls, "No bob subprocess.run call found"
        bob_args = bob_calls[0]
        assert bob_args[1] == "run", f"Expected 'run' subcommand, got {bob_args[1]!r}"
        assert "--format" in bob_args, "'--format' flag missing from bob call"
        assert "json" in bob_args, "'json' missing from bob call"

    def test_bob_not_called_with_stdin_input(self):
        """Bob Shell must NOT be invoked with input= (stdin piping is the old approach)."""
        planner = _make_valid_planner_dict()
        envelope = _bob_envelope(planner)
        fake_proc = _fake_proc(stdout=envelope)

        captured_kwargs = []

        def fake_subprocess_run(args, **kwargs):
            if args and "bob" in str(args[0]) and "run" in args:
                captured_kwargs.append(kwargs)
            return fake_proc

        with patch("run_planner.shutil.which", return_value="/usr/local/bin/bob"):
            with patch("run_planner.subprocess.run", side_effect=fake_subprocess_run):
                run_planner.run(NODE_ID)

        for kw in captured_kwargs:
            assert "input" not in kw, "Bob Shell must not be called with stdin input="


# ---------------------------------------------------------------------------
# Tests: Bob Shell JSON envelope parsing
# ---------------------------------------------------------------------------


class TestBobEnvelopeParsing:
    """Bob's JSON envelope must be parsed and last_message extracted."""

    def test_successful_bob_response_last_message_string(self, capsys):
        """last_message as a JSON string → parsed and used as Planner JSON."""
        planner = _make_valid_planner_dict()
        envelope = _bob_envelope(planner)  # last_message is a JSON string
        fake_proc = _fake_proc(stdout=envelope)

        with patch("run_planner.shutil.which", return_value="/usr/local/bin/bob"):
            with patch("run_planner.subprocess.run", return_value=fake_proc):
                data = run_planner.run(NODE_ID)

        run_planner._validate(data)
        assert data["test_name"] == NODE_ID
        captured = capsys.readouterr()
        assert "[PLANNER] using real Bob" in captured.err

    def test_successful_bob_response_last_message_dict(self, capsys):
        """last_message as a dict → used directly as Planner JSON."""
        planner = _make_valid_planner_dict()
        envelope = _bob_envelope_dict(planner)  # last_message is a dict
        fake_proc = _fake_proc(stdout=envelope)

        with patch("run_planner.shutil.which", return_value="/usr/local/bin/bob"):
            with patch("run_planner.subprocess.run", return_value=fake_proc):
                data = run_planner.run(NODE_ID)

        run_planner._validate(data)
        assert data["test_name"] == NODE_ID
        captured = capsys.readouterr()
        assert "[PLANNER] using real Bob" in captured.err

    def test_missing_last_message_key_triggers_fallback(self, capsys):
        """Envelope with no 'last_message' key → fallback activates."""
        bad_envelope = json.dumps({"result": "something", "status": "ok"})
        fake_proc = _fake_proc(stdout=bad_envelope)

        with patch("run_planner.shutil.which", return_value="/usr/local/bin/bob"):
            with patch("run_planner.subprocess.run", return_value=fake_proc):
                data = run_planner.run(NODE_ID)

        run_planner._validate(data)
        captured = capsys.readouterr()
        assert "BOB FAILED:" in captured.err
        assert "[PLANNER] using fallback" in captured.err

    def test_last_message_invalid_json_string_triggers_fallback(self, capsys):
        """last_message is a non-JSON string → fallback activates."""
        bad_envelope = json.dumps({"last_message": "this is not json"})
        fake_proc = _fake_proc(stdout=bad_envelope)

        with patch("run_planner.shutil.which", return_value="/usr/local/bin/bob"):
            with patch("run_planner.subprocess.run", return_value=fake_proc):
                data = run_planner.run(NODE_ID)

        run_planner._validate(data)
        captured = capsys.readouterr()
        assert "BOB FAILED:" in captured.err
        assert "[PLANNER] using fallback" in captured.err

    def test_last_message_unexpected_type_triggers_fallback(self, capsys):
        """last_message is neither a string nor dict (e.g. a list) → fallback activates."""
        bad_envelope = json.dumps({"last_message": [1, 2, 3]})
        fake_proc = _fake_proc(stdout=bad_envelope)

        with patch("run_planner.shutil.which", return_value="/usr/local/bin/bob"):
            with patch("run_planner.subprocess.run", return_value=fake_proc):
                data = run_planner.run(NODE_ID)

        run_planner._validate(data)
        captured = capsys.readouterr()
        assert "BOB FAILED:" in captured.err
        assert "[PLANNER] using fallback" in captured.err

    def test_node_id_preserved_from_real_bob(self, capsys):
        """Even if Bob returns a truncated test_name, the full node ID is injected."""
        planner = _make_valid_planner_dict()
        planner["test_name"] = "test_cache_starts_clean"  # truncated — Bob error
        envelope = _bob_envelope(planner)
        fake_proc = _fake_proc(stdout=envelope)

        with patch("run_planner.shutil.which", return_value="/usr/local/bin/bob"):
            with patch("run_planner.subprocess.run", return_value=fake_proc):
                data = run_planner.run(NODE_ID)

        # After injection the full node ID must be present
        assert data["test_name"] == NODE_ID


# ---------------------------------------------------------------------------
# Tests: Bob failure modes → fallback
# ---------------------------------------------------------------------------


class TestBobFailureLogging:
    """Bob failures must be explicit on stderr, never silent."""

    def test_bob_failure_nonzero_exit_logs_to_stderr(self, capsys):
        """When bob returns non-zero exit, stderr must contain 'BOB FAILED:'."""
        fake_proc = _fake_proc(stdout="", stderr="some internal error", returncode=1)

        with patch("run_planner.shutil.which", return_value="/usr/local/bin/bob"):
            with patch("run_planner.subprocess.run", return_value=fake_proc):
                data = run_planner.run(NODE_ID)

        captured = capsys.readouterr()
        assert "BOB FAILED:" in captured.err, (
            f"Expected 'BOB FAILED:' in stderr. Got: {captured.err!r}"
        )
        # Fallback must have activated: result is still valid
        run_planner._validate(data)

    def test_bob_failure_invalid_json_logs_to_stderr(self, capsys):
        """When bob returns non-JSON stdout, stderr must contain 'BOB FAILED:'."""
        fake_proc = _fake_proc(stdout="This is not JSON at all.", stderr="", returncode=0)

        with patch("run_planner.shutil.which", return_value="/usr/local/bin/bob"):
            with patch("run_planner.subprocess.run", return_value=fake_proc):
                data = run_planner.run(NODE_ID)

        captured = capsys.readouterr()
        assert "BOB FAILED:" in captured.err, (
            f"Expected 'BOB FAILED:' in stderr. Got: {captured.err!r}"
        )
        run_planner._validate(data)

    def test_bob_absent_does_not_log_bob_failed_to_stderr(self, capsys):
        """When bob is simply not on PATH, 'BOB FAILED:' must NOT appear on stderr."""
        with patch("run_planner.shutil.which", return_value=None):
            run_planner.run(NODE_ID)

        captured = capsys.readouterr()
        assert "BOB FAILED:" not in captured.err, (
            "bob not on PATH is not a failure — 'BOB FAILED:' must not appear on stderr"
        )

    def test_bob_subprocess_exception_triggers_fallback(self, capsys):
        """If subprocess.run raises, fallback must activate and 'BOB FAILED:' logged."""
        with patch("run_planner.shutil.which", return_value="/usr/local/bin/bob"):
            with patch("run_planner.subprocess.run", side_effect=OSError("file not found")):
                data = run_planner.run(NODE_ID)

        captured = capsys.readouterr()
        assert "BOB FAILED:" in captured.err
        assert "[PLANNER] using fallback" in captured.err
        run_planner._validate(data)

    def test_bob_nonzero_exit_triggers_fallback(self, capsys):
        """Non-zero exit → fallback path is taken."""
        fake_proc = _fake_proc(stdout="", stderr="crashed", returncode=2)

        with patch("run_planner.shutil.which", return_value="/usr/local/bin/bob"):
            with patch("run_planner.subprocess.run", return_value=fake_proc):
                data = run_planner.run(NODE_ID)

        captured = capsys.readouterr()
        assert "[PLANNER] using fallback" in captured.err
        run_planner._validate(data)


# ---------------------------------------------------------------------------
# Tests: Diagnostic message separation (stdout vs stderr)
# ---------------------------------------------------------------------------


class TestStdoutStderrSeparation:
    """[PLANNER] diagnostics go to stderr; stdout must remain machine-readable JSON."""

    def test_planner_using_fallback_on_stderr_not_stdout(self, capsys):
        """'[PLANNER] using fallback' must go to stderr, not stdout."""
        with patch("run_planner.shutil.which", return_value=None):
            data = run_planner.run(NODE_ID)

        captured = capsys.readouterr()
        assert "[PLANNER] using fallback" in captured.err
        assert "[PLANNER]" not in captured.out

    def test_planner_using_real_bob_on_stderr_not_stdout(self, capsys):
        """'[PLANNER] using real Bob' must go to stderr, not stdout."""
        planner = _make_valid_planner_dict()
        envelope = _bob_envelope(planner)
        fake_proc = _fake_proc(stdout=envelope)

        with patch("run_planner.shutil.which", return_value="/usr/local/bin/bob"):
            with patch("run_planner.subprocess.run", return_value=fake_proc):
                run_planner.run(NODE_ID)

        captured = capsys.readouterr()
        assert "[PLANNER] using real Bob" in captured.err
        assert "[PLANNER]" not in captured.out

    def test_run_return_value_is_valid_json_serialisable(self, capsys):
        """run() return value must serialise cleanly to JSON (stdout-safe)."""
        with patch("run_planner.shutil.which", return_value=None):
            data = run_planner.run(NODE_ID)

        serialised = json.dumps(data)
        roundtripped = json.loads(serialised)
        assert roundtripped["test_name"] == NODE_ID


# ---------------------------------------------------------------------------
# Tests: Hypothesis normalization
# ---------------------------------------------------------------------------


class TestHypothesisNormalization:
    """_normalize_hypotheses must filter to the allowed enum only."""

    def test_known_labels_pass_through(self):
        allowed = ["order_dependency", "race_condition", "non_determinism", "regression"]
        assert run_planner._normalize_hypotheses(allowed) == allowed

    def test_unknown_labels_are_dropped(self):
        raw = ["order_dependency", "some_unknown_label", "race_condition", "foobar"]
        result = run_planner._normalize_hypotheses(raw)
        assert result == ["order_dependency", "race_condition"]

    def test_duplicates_are_deduplicated(self):
        raw = ["order_dependency", "order_dependency", "regression"]
        result = run_planner._normalize_hypotheses(raw)
        assert result == ["order_dependency", "regression"]

    def test_all_unknown_returns_empty(self):
        raw = ["bad_label", "another_bad", "script.py"]
        result = run_planner._normalize_hypotheses(raw)
        assert result == []

    def test_bob_response_with_unknown_hypotheses_triggers_fallback(self, capsys):
        """If Bob returns only unknown hypotheses, validation fails → fallback."""
        planner = _make_valid_planner_dict()
        planner["hypotheses"] = ["bad_hypothesis", "another_unknown"]
        envelope = _bob_envelope(planner)
        fake_proc = _fake_proc(stdout=envelope)

        with patch("run_planner.shutil.which", return_value="/usr/local/bin/bob"):
            with patch("run_planner.subprocess.run", return_value=fake_proc):
                data = run_planner.run(NODE_ID)

        captured = capsys.readouterr()
        # After normalization, hypotheses is empty → schema validation fails → fallback
        assert "BOB FAILED:" in captured.err
        assert "[PLANNER] using fallback" in captured.err
        run_planner._validate(data)

    def test_bob_response_hypotheses_mixed_normalized(self, capsys):
        """Bob response with some valid + some unknown → only valid ones kept."""
        planner = _make_valid_planner_dict()
        planner["hypotheses"] = ["order_dependency", "unknown_label", "regression"]
        envelope = _bob_envelope(planner)
        fake_proc = _fake_proc(stdout=envelope)

        with patch("run_planner.shutil.which", return_value="/usr/local/bin/bob"):
            with patch("run_planner.subprocess.run", return_value=fake_proc):
                data = run_planner.run(NODE_ID)

        assert data["hypotheses"] == ["order_dependency", "regression"]
        assert "[PLANNER] using real Bob" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Tests: Subagent independence
# ---------------------------------------------------------------------------


class TestSubagentIndependence:
    """Planner hypotheses must be data labels only — not execution paths."""

    def test_hypotheses_are_plain_labels_not_paths(self):
        """Hypothesis values must not contain file paths or script references."""
        data = _call_run()
        for h in data["hypotheses"]:
            assert ".sh" not in h, f"hypothesis contains .sh path: {h!r}"
            assert ".py" not in h, f"hypothesis contains .py path: {h!r}"
            assert "/" not in h, f"hypothesis contains path separator: {h!r}"
            assert "\\" not in h, f"hypothesis contains path separator: {h!r}"

    def test_hypotheses_values_are_known_labels(self):
        """Hypothesis values must be drawn from the four documented labels."""
        known = {"order_dependency", "race_condition", "non_determinism", "regression"}
        data = _call_run()
        for h in data["hypotheses"]:
            assert h in known, (
                f"Unknown hypothesis label {h!r}. Must be one of {sorted(known)}"
            )


# ---------------------------------------------------------------------------
# Tests: Schema validation function
# ---------------------------------------------------------------------------


class TestSchemaValidationFunction:
    """_validate() must raise ValueError on malformed dicts."""

    def test_validate_rejects_missing_keys(self):
        with pytest.raises(ValueError, match="missing keys"):
            run_planner._validate({"test_name": NODE_ID})

    def test_validate_rejects_empty_hypotheses(self):
        with pytest.raises(ValueError):
            run_planner._validate({
                "test_name": NODE_ID,
                "test_file": "tests/test_a_order.py",
                "hypotheses": [],
                "search_targets": ["tests/test_a_order.py"],
            })

    def test_validate_rejects_empty_search_targets(self):
        with pytest.raises(ValueError):
            run_planner._validate({
                "test_name": NODE_ID,
                "test_file": "tests/test_a_order.py",
                "hypotheses": ["order_dependency"],
                "search_targets": [],
            })

    def test_validate_rejects_truncated_node_id(self):
        with pytest.raises(ValueError, match="full pytest node ID"):
            run_planner._validate({
                "test_name": "test_cache_starts_clean",  # no file path
                "test_file": "tests/test_a_order.py",
                "hypotheses": ["order_dependency"],
                "search_targets": ["tests/test_a_order.py"],
            })

    def test_validate_accepts_valid_dict(self):
        run_planner._validate({
            "test_name": NODE_ID,
            "test_file": "tests/test_a_order.py",
            "hypotheses": ["order_dependency"],
            "search_targets": ["tests/test_a_order.py", "src/shared_cache.py"],
        })  # must not raise


# ---------------------------------------------------------------------------
# Tests: Windows Bob executable resolution (_resolve_bob)
# ---------------------------------------------------------------------------


class TestResolveBob:
    """_resolve_bob must return the correct argv prefix for each platform scenario."""

    def test_returns_none_when_bob_not_on_path(self):
        """If shutil.which finds no bob, _resolve_bob returns None."""
        with patch("run_planner.shutil.which", return_value=None):
            assert run_planner._resolve_bob() is None

    def test_unix_exe_returned_directly(self):
        """On a non-.cmd path, the exe is returned as a single-element list."""
        with patch("run_planner.shutil.which", return_value="/usr/local/bin/bob"):
            with patch("run_planner.sys.platform", "linux"):
                result = run_planner._resolve_bob()
        assert result == ["/usr/local/bin/bob"]

    def test_windows_cmd_resolved_via_node(self, tmp_path):
        """On Windows, a .CMD path is replaced with [node, bob.js]."""
        # Build a fake npm directory with the expected bob.js
        npm_dir = tmp_path / "npm"
        bob_js_dir = npm_dir / "node_modules" / "bobshell" / "dist"
        bob_js_dir.mkdir(parents=True)
        bob_js = bob_js_dir / "bob.js"
        bob_js.write_text("// fake bob.js")
        fake_cmd = str(npm_dir / "bob.CMD")
        fake_node = "/fake/node"

        def fake_which(name):
            if name == "bob":
                return fake_cmd
            if name == "node":
                return fake_node
            return None

        with patch("run_planner.shutil.which", side_effect=fake_which):
            with patch("run_planner.sys.platform", "win32"):
                result = run_planner._resolve_bob()

        assert result == [fake_node, str(bob_js)]

    def test_windows_cmd_returns_none_if_node_missing(self, tmp_path):
        """On Windows, if node is not on PATH, returns None even if bob.CMD exists."""
        npm_dir = tmp_path / "npm"
        bob_js_dir = npm_dir / "node_modules" / "bobshell" / "dist"
        bob_js_dir.mkdir(parents=True)
        (bob_js_dir / "bob.js").write_text("// fake")
        fake_cmd = str(npm_dir / "bob.CMD")

        def fake_which(name):
            if name == "bob":
                return fake_cmd
            return None  # node not found

        with patch("run_planner.shutil.which", side_effect=fake_which):
            with patch("run_planner.sys.platform", "win32"):
                result = run_planner._resolve_bob()

        assert result is None

    def test_windows_cmd_returns_none_if_bobjs_missing(self, tmp_path):
        """On Windows, if bob.js is not in expected location, returns None."""
        npm_dir = tmp_path / "npm"
        npm_dir.mkdir()
        fake_cmd = str(npm_dir / "bob.CMD")

        def fake_which(name):
            if name == "bob":
                return fake_cmd
            if name == "node":
                return "/fake/node"
            return None

        with patch("run_planner.shutil.which", side_effect=fake_which):
            with patch("run_planner.sys.platform", "win32"):
                result = run_planner._resolve_bob()

        assert result is None

    def test_windows_bat_also_resolved_via_node(self, tmp_path):
        """A .BAT extension is treated the same as .CMD on Windows."""
        npm_dir = tmp_path / "npm"
        bob_js_dir = npm_dir / "node_modules" / "bobshell" / "dist"
        bob_js_dir.mkdir(parents=True)
        bob_js = bob_js_dir / "bob.js"
        bob_js.write_text("// fake")
        fake_bat = str(npm_dir / "bob.BAT")
        fake_node = "/fake/node"

        def fake_which(name):
            if name == "bob":
                return fake_bat
            if name == "node":
                return fake_node
            return None

        with patch("run_planner.shutil.which", side_effect=fake_which):
            with patch("run_planner.sys.platform", "win32"):
                result = run_planner._resolve_bob()

        assert result == [fake_node, str(bob_js)]

    def test_fallback_activates_when_resolve_bob_returns_none(self, capsys):
        """run() falls back gracefully when _resolve_bob returns None."""
        with patch("run_planner._resolve_bob", return_value=None):
            data = run_planner.run(NODE_ID)
        run_planner._validate(data)
        assert "[PLANNER] using fallback" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Tests: Bob subprocess — stdin=DEVNULL enforced
# ---------------------------------------------------------------------------


class TestBobSubprocessStdin:
    """The Bob subprocess must always be called with stdin=DEVNULL."""

    def test_bob_subprocess_uses_devnull_stdin(self):
        """subprocess.run for bob must pass stdin=subprocess.DEVNULL."""
        planner = _make_valid_planner_dict()
        envelope = _bob_envelope(planner)
        fake_proc = _fake_proc(stdout=envelope)

        captured_kwargs = []

        def fake_subprocess_run(args, **kwargs):
            if args and "run" in args and "--format" in args:
                captured_kwargs.append(kwargs)
            return fake_proc

        with patch("run_planner.shutil.which", return_value="/usr/local/bin/bob"):
            with patch("run_planner.subprocess.run", side_effect=fake_subprocess_run):
                run_planner.run(NODE_ID)

        assert captured_kwargs, "No bob subprocess.run call captured"
        for kw in captured_kwargs:
            assert kw.get("stdin") is subprocess.DEVNULL, (
                f"Expected stdin=subprocess.DEVNULL, got stdin={kw.get('stdin')!r}"
            )


# ---------------------------------------------------------------------------
# Tests: _load_bob_env
# ---------------------------------------------------------------------------


class TestLoadBobEnv:
    """_load_bob_env must inject BOB_API_KEY from .env when not already set."""

    def test_returns_env_unchanged_when_key_already_set(self):
        """If BOB_API_KEY is in the environment, no .env reading is needed."""
        with patch.dict("os.environ", {"BOB_API_KEY": "already-set"}, clear=False):
            env = run_planner._load_bob_env()
        assert env["BOB_API_KEY"] == "already-set"

    def test_loads_key_from_dotenv_file(self, tmp_path, monkeypatch):
        """BOB_API_KEY absent from environment is loaded from .env."""
        dotenv = tmp_path / ".env"
        dotenv.write_text("BOB_API_KEY=test-key-from-dotenv\n", encoding="utf-8")
        monkeypatch.setattr(run_planner, "_DOTENV_FILE", dotenv)
        env_without_key = {k: v for k, v in __import__("os").environ.items() if k != "BOB_API_KEY"}
        with patch.dict("os.environ", env_without_key, clear=True):
            env = run_planner._load_bob_env()
        assert env.get("BOB_API_KEY") == "test-key-from-dotenv"

    def test_loads_bobshell_api_key_as_fallback(self, tmp_path, monkeypatch):
        """BOBSHELL_API_KEY in .env is also accepted and aliased to BOB_API_KEY."""
        dotenv = tmp_path / ".env"
        dotenv.write_text("BOBSHELL_API_KEY=legacy-key\n", encoding="utf-8")
        monkeypatch.setattr(run_planner, "_DOTENV_FILE", dotenv)
        env_without_key = {k: v for k, v in __import__("os").environ.items()
                           if k not in ("BOB_API_KEY", "BOBSHELL_API_KEY")}
        with patch.dict("os.environ", env_without_key, clear=True):
            env = run_planner._load_bob_env()
        assert env.get("BOB_API_KEY") == "legacy-key"

    def test_returns_env_unchanged_when_no_dotenv_file(self, tmp_path, monkeypatch):
        """If .env does not exist, environment is returned as-is (no crash)."""
        missing = tmp_path / "nonexistent.env"
        monkeypatch.setattr(run_planner, "_DOTENV_FILE", missing)
        env_without_key = {k: v for k, v in __import__("os").environ.items() if k != "BOB_API_KEY"}
        with patch.dict("os.environ", env_without_key, clear=True):
            env = run_planner._load_bob_env()
        assert "BOB_API_KEY" not in env

    def test_ignores_comments_and_blank_lines(self, tmp_path, monkeypatch):
        """Comments and blank lines in .env must not confuse the parser."""
        dotenv = tmp_path / ".env"
        dotenv.write_text(
            "# This is a comment\n\nIBM_CLOUD_API_KEY=other\nBOB_API_KEY=real-key\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(run_planner, "_DOTENV_FILE", dotenv)
        env_without_key = {k: v for k, v in __import__("os").environ.items() if k != "BOB_API_KEY"}
        with patch.dict("os.environ", env_without_key, clear=True):
            env = run_planner._load_bob_env()
        assert env.get("BOB_API_KEY") == "real-key"

    def test_strips_quotes_from_value(self, tmp_path, monkeypatch):
        """Values wrapped in quotes are stripped before use."""
        dotenv = tmp_path / ".env"
        dotenv.write_text('BOB_API_KEY="quoted-key"\n', encoding="utf-8")
        monkeypatch.setattr(run_planner, "_DOTENV_FILE", dotenv)
        env_without_key = {k: v for k, v in __import__("os").environ.items() if k != "BOB_API_KEY"}
        with patch.dict("os.environ", env_without_key, clear=True):
            env = run_planner._load_bob_env()
        assert env.get("BOB_API_KEY") == "quoted-key"
