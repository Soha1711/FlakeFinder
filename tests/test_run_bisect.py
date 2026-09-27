from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import analyzer.run_bisect as bisect


TEST_NAME = "tests/test_d_regression.py::test_calculate_total"
EXPECTED_FIRST_BAD = "04805c176d22dc3b07ed581cfa99fc1e7b5dd37e"


def valid_result(**overrides):
    result = {
        "subagent": "bisect",
        "test_name": TEST_NAME,
        "evidence": (
            "First bad commit: "
            f"{EXPECTED_FIRST_BAD}. "
            "Commit introduced the regression in src/regression.py."
        ),
        "hypothesis": (
            "The regression was introduced by the first bad commit."
        ),
        "confidence": "high",
    }
    result.update(overrides)
    return result


class TestValidation:
    def test_valid_result_passes(self):
        data = valid_result()

        bisect._validate(data, TEST_NAME)

    def test_missing_subagent_fails(self):
        data = valid_result()
        del data["subagent"]

        with pytest.raises(ValueError, match="missing required fields"):
            bisect._validate(data, TEST_NAME)

    def test_missing_test_name_fails(self):
        data = valid_result()
        del data["test_name"]

        with pytest.raises(ValueError, match="missing required fields"):
            bisect._validate(data, TEST_NAME)

    def test_missing_evidence_fails(self):
        data = valid_result()
        del data["evidence"]

        with pytest.raises(ValueError, match="missing required fields"):
            bisect._validate(data, TEST_NAME)

    def test_missing_hypothesis_fails(self):
        data = valid_result()
        del data["hypothesis"]

        with pytest.raises(ValueError, match="missing required fields"):
            bisect._validate(data, TEST_NAME)

    def test_missing_confidence_fails(self):
        data = valid_result()
        del data["confidence"]

        with pytest.raises(ValueError, match="missing required fields"):
            bisect._validate(data, TEST_NAME)

    def test_wrong_subagent_fails(self):
        data = valid_result(subagent="shuffle")

        with pytest.raises(
            ValueError,
            match="'subagent' must be 'bisect'",
        ):
            bisect._validate(data, TEST_NAME)

    def test_wrong_test_name_fails(self):
        data = valid_result(
            test_name="tests/test_a_order.py::test_cache_starts_clean"
        )

        with pytest.raises(
            ValueError,
            match="'test_name' must be",
        ):
            bisect._validate(data, TEST_NAME)

    @pytest.mark.parametrize(
        "confidence",
        ["high", "medium", "low"],
    )
    def test_valid_confidence_values_pass(self, confidence):
        data = valid_result(confidence=confidence)

        bisect._validate(data, TEST_NAME)

    def test_invalid_confidence_fails(self):
        data = valid_result(confidence="certain")

        with pytest.raises(
            ValueError,
            match="'confidence' must be one of",
        ):
            bisect._validate(data, TEST_NAME)

    def test_empty_evidence_fails(self):
        data = valid_result(evidence="")

        with pytest.raises(
            ValueError,
            match="'evidence' must be a non-empty string",
        ):
            bisect._validate(data, TEST_NAME)

    def test_empty_hypothesis_fails(self):
        data = valid_result(hypothesis="")

        with pytest.raises(
            ValueError,
            match="'hypothesis' must be a non-empty string",
        ):
            bisect._validate(data, TEST_NAME)

    def test_missing_expected_first_bad_commit_fails(self):
        data = valid_result(
            evidence="The regression was found by git bisect."
        )

        with pytest.raises(
            ValueError,
            match="expected first bad commit",
        ):
            bisect._validate(data, TEST_NAME)


class TestPrompt:
    def test_prompt_substitutes_test_name(self, tmp_path, monkeypatch):
        prompt = tmp_path / "bisect_prompt.md"
        prompt.write_text(
            "Investigate {test_name} now.",
            encoding="utf-8",
        )

        monkeypatch.setattr(
            bisect,
            "_BISECT_PROMPT",
            prompt,
        )

        result = bisect._build_bisect_prompt(TEST_NAME)

        assert "{test_name}" not in result
        assert TEST_NAME in result

    def test_prompt_does_not_leave_placeholder(self):
        result = bisect._build_bisect_prompt(TEST_NAME)

        assert "{{TEST_NAME}}" not in result
        assert "{test_name}" not in result
        assert TEST_NAME in result


class TestJsonParsing:
    def test_parse_dict(self):
        data = valid_result()

        assert bisect._parse_last_message(data) == data

    def test_parse_plain_json(self):
        data = valid_result()
        raw = json.dumps(data)

        assert bisect._parse_last_message(raw) == data

    def test_parse_fenced_json(self):
        data = valid_result()
        raw = (
            "```json\n"
            + json.dumps(data, indent=2)
            + "\n```"
        )

        assert bisect._parse_last_message(raw) == data

    def test_parse_fenced_json_without_language(self):
        data = valid_result()
        raw = (
            "```\n"
            + json.dumps(data)
            + "\n```"
        )

        assert bisect._parse_last_message(raw) == data

    def test_parse_empty_string_fails(self):
        with pytest.raises(
            ValueError,
            match="last_message is empty",
        ):
            bisect._parse_last_message("")

    def test_parse_invalid_json_fails(self):
        with pytest.raises(
            ValueError,
            match="last_message is not valid JSON",
        ):
            bisect._parse_last_message("this is not json")

    def test_parse_non_object_json_fails(self):
        with pytest.raises(
            ValueError,
            match="last_message is not valid JSON",
        ):
            bisect._parse_last_message(
                json.dumps(["not", "an", "object"])
            )


class TestRawResponse:
    def test_save_raw_last_message(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            bisect,
            "_DEBUG_DIR",
            tmp_path,
        )

        raw = '{"subagent":"bisect"}'

        bisect._save_raw_last_message(raw)

        output = tmp_path / "bisect_raw_response.txt"

        assert output.exists()
        assert output.read_text(
            encoding="utf-8"
        ) == raw


class TestBobInvocation:
    def test_bob_command_uses_run_format_json(
        self,
        monkeypatch,
    ):
        captured = {}

        monkeypatch.setattr(
            bisect,
            "_resolve_bob",
            lambda: ["bob"],
        )

        def fake_run(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

            return subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout=json.dumps(
                    {
                        "last_message": json.dumps(
                            valid_result()
                        )
                    }
                ),
                stderr="",
            )

        monkeypatch.setattr(
            bisect.subprocess,
            "run",
            fake_run,
        )

        monkeypatch.setattr(
            bisect,
            "_save_raw_last_message",
            lambda raw: None,
        )

        result = bisect._invoke_bob(TEST_NAME)

        assert result == valid_result()

        command = captured["args"][0]

        assert command[0] == "bob"
        assert command[1] == "run"
        assert command[2] == "--format"
        assert command[3] == "json"

    def test_bob_uses_devnull_stdin(
        self,
        monkeypatch,
    ):
        captured = {}

        monkeypatch.setattr(
            bisect,
            "_resolve_bob",
            lambda: ["bob"],
        )

        def fake_run(*args, **kwargs):
            captured["kwargs"] = kwargs

            return subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout=json.dumps(
                    {
                        "last_message": json.dumps(
                            valid_result()
                        )
                    }
                ),
                stderr="",
            )

        monkeypatch.setattr(
            bisect.subprocess,
            "run",
            fake_run,
        )

        monkeypatch.setattr(
            bisect,
            "_save_raw_last_message",
            lambda raw: None,
        )

        result = bisect._invoke_bob(TEST_NAME)

        assert result == valid_result()
        assert captured["kwargs"]["stdin"] is subprocess.DEVNULL

    def test_bob_uses_project_working_directory(
        self,
        monkeypatch,
    ):
        captured = {}

        monkeypatch.setattr(
            bisect,
            "_resolve_bob",
            lambda: ["bob"],
        )

        def fake_run(*args, **kwargs):
            captured["kwargs"] = kwargs

            return subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout=json.dumps(
                    {
                        "last_message": json.dumps(
                            valid_result()
                        )
                    }
                ),
                stderr="",
            )

        monkeypatch.setattr(
            bisect.subprocess,
            "run",
            fake_run,
        )

        monkeypatch.setattr(
            bisect,
            "_save_raw_last_message",
            lambda raw: None,
        )

        bisect._invoke_bob(TEST_NAME)

        assert captured["kwargs"]["cwd"] == bisect._HERE

    def test_missing_last_message_falls_back(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(
            bisect,
            "_resolve_bob",
            lambda: ["bob"],
        )

        monkeypatch.setattr(
            bisect.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout=json.dumps(
                    {"other": "value"}
                ),
                stderr="",
            ),
        )

        assert bisect._invoke_bob(TEST_NAME) is None

    def test_malformed_bob_envelope_falls_back(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(
            bisect,
            "_resolve_bob",
            lambda: ["bob"],
        )

        monkeypatch.setattr(
            bisect.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout="not json",
                stderr="",
            ),
        )

        assert bisect._invoke_bob(TEST_NAME) is None

    def test_nonzero_bob_exit_falls_back(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(
            bisect,
            "_resolve_bob",
            lambda: ["bob"],
        )

        monkeypatch.setattr(
            bisect.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args=args[0],
                returncode=1,
                stdout="",
                stderr="bob failed",
            ),
        )

        assert bisect._invoke_bob(TEST_NAME) is None

    def test_invalid_last_message_falls_back(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(
            bisect,
            "_resolve_bob",
            lambda: ["bob"],
        )

        monkeypatch.setattr(
            bisect.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout=json.dumps(
                    {
                        "last_message": "not json"
                    }
                ),
                stderr="",
            ),
        )

        assert bisect._invoke_bob(TEST_NAME) is None


class TestWindowsBash:
    def test_find_bash_prefers_git_bash_on_windows(
        self,
        monkeypatch,
        tmp_path,
    ):
        git_bash = tmp_path / "Git" / "bin" / "bash.exe"
        git_bash.parent.mkdir(parents=True)
        git_bash.write_text("", encoding="utf-8")

        monkeypatch.setattr(
            bisect.os,
            "name",
            "nt",
        )

        monkeypatch.setattr(
            bisect,
            "shutil",
            type(
                "FakeShutil",
                (),
                {
                    "which": staticmethod(
                        lambda name: "C:\\Windows\\System32\\bash.EXE"
                    )
                },
            ),
        )

        original_path_class = Path

        class FakeGitBashPath:
            def exists(self):
                return True

            def __str__(self):
                return str(git_bash)

        original_git_bash = original_path_class(
            r"C:\Program Files\Git\bin\bash.exe"
        )

        def fake_path(value):
            if str(value) == str(original_git_bash):
                return FakeGitBashPath()

            return original_path_class(value)

        monkeypatch.setattr(
            bisect,
            "Path",
            fake_path,
        )

        result = bisect._find_bash()

        assert result == str(git_bash)


class TestFallback:
    def test_fallback_calls_actual_script(
        self,
        monkeypatch,
        tmp_path,
    ):
        script = tmp_path / "git_bisect_runner.sh"
        script.write_text(
            "#!/bin/bash\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(
            bisect,
            "_BISECT_SCRIPT",
            script,
        )

        monkeypatch.setattr(
            bisect,
            "_find_bash",
            lambda: "git-bash.exe",
        )

        captured = {}

        def fake_run(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

            return subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout=(
                    "Bisect result:\n"
                    f"{EXPECTED_FIRST_BAD} "
                    "is the first bad commit\n"
                ),
                stderr="",
            )

        monkeypatch.setattr(
            bisect.subprocess,
            "run",
            fake_run,
        )

        result = bisect._fallback(TEST_NAME)

        assert result["subagent"] == "bisect"
        assert result["test_name"] == TEST_NAME
        assert EXPECTED_FIRST_BAD in result["evidence"]

        command = captured["args"][0]

        assert command[0] == "git-bash.exe"
        assert command[1] == str(script)
        assert command[2] == TEST_NAME
        assert command[3] == "10"
        assert command[4] == "1"

    def test_fallback_uses_devnull_stdin(
        self,
        monkeypatch,
        tmp_path,
    ):
        script = tmp_path / "git_bisect_runner.sh"
        script.write_text(
            "#!/bin/bash\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(
            bisect,
            "_BISECT_SCRIPT",
            script,
        )

        monkeypatch.setattr(
            bisect,
            "_find_bash",
            lambda: "git-bash.exe",
        )

        captured = {}

        def fake_run(*args, **kwargs):
            captured["kwargs"] = kwargs

            return subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout=(
                    f"{EXPECTED_FIRST_BAD} "
                    "is the first bad commit"
                ),
                stderr="",
            )

        monkeypatch.setattr(
            bisect.subprocess,
            "run",
            fake_run,
        )

        bisect._fallback(TEST_NAME)

        assert (
            captured["kwargs"]["stdin"]
            is subprocess.DEVNULL
        )

    def test_fallback_rejects_missing_expected_commit(
        self,
        monkeypatch,
        tmp_path,
    ):
        script = tmp_path / "git_bisect_runner.sh"
        script.write_text(
            "#!/bin/bash\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(
            bisect,
            "_BISECT_SCRIPT",
            script,
        )

        monkeypatch.setattr(
            bisect,
            "_find_bash",
            lambda: "git-bash.exe",
        )

        monkeypatch.setattr(
            bisect.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout="No first bad commit found",
                stderr="",
            ),
        )

        with pytest.raises(
            RuntimeError,
            match="expected first bad commit",
        ):
            bisect._fallback(TEST_NAME)

    def test_fallback_rejects_nonzero_exit(
        self,
        monkeypatch,
        tmp_path,
    ):
        script = tmp_path / "git_bisect_runner.sh"
        script.write_text(
            "#!/bin/bash\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(
            bisect,
            "_BISECT_SCRIPT",
            script,
        )

        monkeypatch.setattr(
            bisect,
            "_find_bash",
            lambda: "git-bash.exe",
        )

        monkeypatch.setattr(
            bisect.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args=args[0],
                returncode=1,
                stdout="",
                stderr="runner failed",
            ),
        )

        with pytest.raises(
            RuntimeError,
            match="exited with code 1",
        ):
            bisect._fallback(TEST_NAME)

    def test_fallback_rejects_empty_output(
        self,
        monkeypatch,
        tmp_path,
    ):
        script = tmp_path / "git_bisect_runner.sh"
        script.write_text(
            "#!/bin/bash\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(
            bisect,
            "_BISECT_SCRIPT",
            script,
        )

        monkeypatch.setattr(
            bisect,
            "_find_bash",
            lambda: "git-bash.exe",
        )

        monkeypatch.setattr(
            bisect.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout="",
                stderr="",
            ),
        )

        with pytest.raises(
            RuntimeError,
            match="no output",
        ):
            bisect._fallback(TEST_NAME)

class TestRun:
    def test_run_uses_bob_result(
        self,
        monkeypatch,
        tmp_path,
    ):
        data = valid_result()

        monkeypatch.setattr(
            bisect,
            "_invoke_bob",
            lambda test_name: data,
        )

        monkeypatch.setattr(
            bisect,
            "_EVIDENCE_DIR",
            tmp_path,
        )

        result = bisect.run(TEST_NAME)

        assert result == data

        evidence_file = (
            tmp_path
            / "bisect_tests_test_d_regression.py__test_calculate_total.json"
        )

        assert evidence_file.exists()

        saved = json.loads(
            evidence_file.read_text(
                encoding="utf-8"
            )
        )

        assert saved == data

    def test_run_uses_fallback_when_bob_fails(
        self,
        monkeypatch,
        tmp_path,
    ):
        data = valid_result()

        monkeypatch.setattr(
            bisect,
            "_invoke_bob",
            lambda test_name: None,
        )

        monkeypatch.setattr(
            bisect,
            "_fallback",
            lambda test_name: data,
        )

        monkeypatch.setattr(
            bisect,
            "_EVIDENCE_DIR",
            tmp_path,
        )

        result = bisect.run(TEST_NAME)

        assert result == data

    def test_run_rejects_invalid_node_id(self):
        with pytest.raises(
            ValueError,
            match="full pytest node ID",
        ):
            bisect.run("test_d_regression")

    def test_result_is_json_serializable(self):
        data = valid_result()

        encoded = json.dumps(data)
        decoded = json.loads(encoded)

        assert decoded == data


class TestBobEnvironment:
    def test_load_bob_env_preserves_existing_key(
        self,
        monkeypatch,
    ):
        monkeypatch.setenv(
            "BOB_API_KEY",
            "test-key",
        )

        result = bisect._load_bob_env()

        assert result["BOB_API_KEY"] == "test-key"

    def test_load_bob_env_reads_dotenv(
        self,
        monkeypatch,
        tmp_path,
    ):
        env_file = tmp_path / ".env"

        env_file.write_text(
            'BOB_API_KEY="from-dotenv"\n',
            encoding="utf-8",
        )

        monkeypatch.setattr(
            bisect,
            "_ENV_FILE",
            env_file,
        )

        monkeypatch.delenv(
            "BOB_API_KEY",
            raising=False,
        )

        result = bisect._load_bob_env()

        assert result["BOB_API_KEY"] == "from-dotenv"