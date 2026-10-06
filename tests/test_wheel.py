"""Check installed-package responses and runner cleanup without external operations."""

import io
import signal
import subprocess
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest


def test_listeners_accept_sign_in_page_and_certificate_rejection(
    run_script: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejection = HTTPError("https://127.0.0.1:18443/client/sync", 403, "Forbidden", {}, io.BytesIO())
    opening = Mock(side_effect=[io.BytesIO(b"Sign in"), rejection])
    monkeypatch.setattr(run_script.request, "urlopen", opening)

    run_script.check_listeners(18000, 18443)

    api_request = opening.call_args.args[0]
    assert api_request.full_url == "https://127.0.0.1:18443/client/sync"
    assert api_request.get_method() == "POST"
    assert api_request.data == b"{}"
    assert rejection.fp.closed


def test_listeners_refuse_an_api_that_accepts_no_certificate(run_script: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    opening = Mock(side_effect=[io.BytesIO(b"Sign in"), io.BytesIO(b"accepted")])
    monkeypatch.setattr(run_script.request, "urlopen", opening)

    with pytest.raises(RuntimeError, match="accepted a client request without a certificate"):
        run_script.check_listeners(18000, 18443)


def test_listeners_do_not_treat_other_http_errors_as_success(run_script: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    failure = HTTPError("https://127.0.0.1:18443/client/sync", 500, "Server error", {}, io.BytesIO())
    opening = Mock(side_effect=[io.BytesIO(b"Sign in"), failure])
    monkeypatch.setattr(run_script.request, "urlopen", opening)

    with pytest.raises(HTTPError, match="500"):
        run_script.check_listeners(18000, 18443)

    assert failure.fp.closed


@pytest.mark.parametrize("wait_timeout", [False, True])
def test_wheel_stops_its_server_after_a_failed_check(
    run_script: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wait_timeout: bool,
) -> None:
    server = Mock(pid=12345)
    server.wait.side_effect = [subprocess.TimeoutExpired("sanea", 5), 0] if wait_timeout else [0]
    kill_group = Mock()
    monkeypatch.setattr(run_script.Commands, "run", Mock())
    monkeypatch.setattr(run_script.os, "killpg", kill_group)
    monkeypatch.setattr(run_script.subprocess, "Popen", Mock(return_value=server))
    monkeypatch.setattr(run_script, "free_ports", Mock(return_value=(18000, 18443, 62117)))
    monkeypatch.setattr(run_script, "check_listeners", Mock(side_effect=RuntimeError("listener failed")))

    with pytest.raises(RuntimeError, match="listener failed"):
        run_script.run_wheel(tmp_path / "source", tmp_path, {})

    kill_group.assert_any_call(server.pid, signal.SIGTERM)
    assert kill_group.call_count == 1 + int(wait_timeout)
    server.wait.assert_any_call(timeout=5)

    if wait_timeout:
        kill_group.assert_any_call(server.pid, signal.SIGKILL)


@pytest.mark.parametrize(("failure", "exit_code"), [(RuntimeError("check failed"), 1), (KeyboardInterrupt(), 130)])
def test_main_cleans_temporary_state_after_failure(
    run_script: ModuleType, runner_workspace: Path, monkeypatch: pytest.MonkeyPatch,
    failure: BaseException, exit_code: int,
) -> None:
    directories: list[Path] = []
    previous_handler = signal.getsignal(signal.SIGTERM)

    def fail_check(project_root: Path, work_dir: Path, environment: dict[str, str]) -> None:
        directories.append(work_dir)
        raise failure

    monkeypatch.setattr(run_script, "run_wheel", fail_check)

    result = run_script.main(["wheel"])

    assert result == exit_code
    assert len(directories) == 1
    assert not directories[0].exists()
    assert signal.getsignal(signal.SIGTERM) == previous_handler


def test_main_cleans_temporary_state_on_termination(
    run_script: ModuleType, runner_workspace: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    directories: list[Path] = []
    previous_handler = signal.getsignal(signal.SIGTERM)

    def terminate_check(project_root: Path, work_dir: Path, environment: dict[str, str]) -> None:
        directories.append(work_dir)
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(run_script, "run_wheel", terminate_check)

    with pytest.raises(SystemExit, match="143"):
        run_script.main(["wheel"])

    assert len(directories) == 1
    assert not directories[0].exists()
    assert signal.getsignal(signal.SIGTERM) == previous_handler
