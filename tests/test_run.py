"""Check runner commands, local settings and resource isolation without external operations."""

import os
import signal
import subprocess
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("exit_code", [0, 7])
@pytest.mark.parametrize("retained", [False, True])
def test_tests_forward_arguments_preserve_exit_code_and_isolate_state(
    run_script: ModuleType, runner_workspace: Path, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, exit_code: int, retained: bool,
) -> None:
    observed = Mock()
    previous_handler = signal.getsignal(signal.SIGTERM)
    parent = runner_workspace / "runs"
    monkeypatch.setenv("SANECMP_INTEGRATION_STATE_DIR", f"{parent}" if retained else "")
    monkeypatch.setenv("SANEA_DATABASE_PATH", f"{tmp_path / "must-not-be-used.sqlite3"}")
    monkeypatch.setenv("PYTHON_ENV", "production")
    monkeypatch.chdir(tmp_path)

    def execute(commands: Any, arguments: Sequence[str]) -> None:
        observed(list(arguments), commands.cwd, dict(commands.environment))

        if exit_code:
            raise subprocess.CalledProcessError(exit_code, arguments)

    monkeypatch.setattr(run_script.Commands, "run", execute)

    result = run_script.main(["tests", "-q", "-k", "client"])

    assert result == exit_code
    command, cwd, environment = observed.call_args.args
    assert command == ["uv", "run", "--python", "3.12", "--group", "tests", "pytest", "-q", "-k", "client"]
    assert cwd == runner_workspace
    assert Path.cwd() == tmp_path
    assert environment["PYTHON_ENV"] == "testing"
    state_dir = Path(environment["SANECMP_INTEGRATION_STATE_DIR"])
    assert environment["SANEA_STATE_DIR"] == f"{state_dir / "sanea"}"
    assert environment["SANEA_DATABASE_PATH"] == f"{state_dir / "sanea" / "sanea.sqlite3"}"
    assert environment["SANEA_PKI_DIR"] == f"{state_dir / "sanea" / "pki"}"
    assert state_dir.exists() is retained
    assert (state_dir.parent == parent) is retained
    assert (runner_workspace / ".env").read_bytes() == (runner_workspace / ".env.example").read_bytes()
    assert signal.getsignal(signal.SIGTERM) == previous_handler


def test_environment_reads_local_config_preserves_it_and_prefers_inherited_values(
    run_script: ModuleType, runner_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = runner_workspace / ".env"
    content = "SANEA_HTTP_PORT=18100\nSANECMP_DEVELOPMENT_STATE_DIR=.state/runs\n"
    env_file.write_text(content, encoding="utf-8")
    monkeypatch.setenv("SANEA_HTTP_PORT", "18101")
    monkeypatch.setenv("VIRTUAL_ENV", "/tmp/runner-environment")

    environment = run_script.load_environment(runner_workspace)

    assert environment["SANEA_HTTP_PORT"] == "18101"
    assert environment["SANEA_HTTPS_PORT"] == "18443"
    assert environment["SANECMP_DEVELOPMENT_STATE_DIR"] == ".state/runs"
    assert "VIRTUAL_ENV" not in environment
    assert "SANECMP_DEVELOPMENT_STATE_DIR" not in os.environ
    assert env_file.read_text(encoding="utf-8") == content


def test_development_prepares_tools_and_registers_before_starting_safe_service(
    run_script: ModuleType, runner_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = Mock()
    server = Mock(pid=12345)
    server.poll.return_value = None
    server.wait.return_value = 0
    kill_group = Mock()
    content = (runner_workspace / ".env.example").read_text(encoding="utf-8").replace("18000", "18102")
    (runner_workspace / ".env").write_text(content, encoding="utf-8")

    def execute(commands: Any, arguments: Sequence[str], *, cwd: Path | None = None) -> None:
        observed(list(arguments), cwd or commands.cwd, dict(commands.environment))

    def start_server(command: Sequence[str], **options: Any) -> Mock:
        options["stdout"].write(b"SANEA_REGISTRATION_CODE=ABCD-EFGH\n")
        options["stdout"].flush()
        return server

    launch = Mock(side_effect=start_server)
    monkeypatch.setattr(run_script.Commands, "run", execute)
    monkeypatch.setattr(run_script.subprocess, "Popen", launch)
    monkeypatch.setattr(run_script.os, "killpg", kill_group)

    result = run_script.main(["develop"])

    assert result == 0
    calls = observed.call_args_list
    assert len(calls) == 6
    assert calls[0].args[:2] == (["ma", "up", "--tool"], runner_workspace / "components" / "sanex")
    assert calls[1].args[:2] == (["ma", "up", "--tool"], runner_workspace / "components" / "sanea")
    assert calls[2].args[0] == ["sanea", "migrate", "--noinput"]
    assert calls[3].args[0][:3] == ["sanea", "shell", "--command"]
    assert calls[4].args[0][-2:] == ["register", "ABCD-EFGH"]
    assert calls[5].args[0] == [*calls[4].args[0][:-2], "service"]
    assert calls[5].args[0][:3] == ["sanex", "develop", "--state-dir"]
    assert launch.call_args.args[0] == ["sanea", "serve", "--open-registration"]
    assert launch.call_args.kwargs["cwd"] == runner_workspace
    assert launch.call_args.kwargs["start_new_session"] is True
    environment = calls[0].args[2]
    assert environment["PYTHON_ENV"] == "development"
    assert environment["SANEA_HTTP_PORT"] == "18102"
    assert not Path(environment["SANEA_STATE_DIR"]).exists()
    kill_group.assert_called_once_with(server.pid, signal.SIGTERM)
    server.wait.assert_called_once_with(timeout=5)


@pytest.mark.parametrize(("server_status", "message"), [(1, "sanea stopped"), (None, "Timed out")])
def test_development_registration_reports_server_exit_or_timeout(
    run_script: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    server_status: int | None, message: str,
) -> None:
    server = Mock()
    server.poll.return_value = server_status
    log_path = tmp_path / "sanea.log"
    log_path.write_text("Starting sanea...\n", encoding="utf-8")
    monkeypatch.setattr(run_script, "monotonic", Mock(side_effect=[0, 1, 31]))
    monkeypatch.setattr(run_script, "sleep", Mock())

    with pytest.raises(RuntimeError, match=message):
        run_script.registration_code(server, log_path)


@pytest.mark.parametrize("command", ["develop", "wheel"])
def test_non_test_commands_reject_extra_arguments_before_creating_local_files(
    run_script: ModuleType, runner_workspace: Path, command: str,
) -> None:

    with pytest.raises(SystemExit, match="2"):
        run_script.main([command, "--unexpected"])

    assert not (runner_workspace / ".env").exists()
