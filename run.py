#!/usr/bin/env -S uv run --script --python 3.12
# /// script
# requires-python = ">=3.12"
# dependencies = ["envbox>=2.0.1,<3"]
# ///
"""Run isolated development, integration tests and installed-package checks."""

import argparse
import json
import logging
import os
import signal
import socket
import ssl
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp
from time import monotonic, sleep
from types import FrameType
from typing import BinaryIO
from urllib import error, request

from envbox import read_envfile


PROJECT_ROOT = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


@dataclass
class Commands:
    """Run child processes and stop their process groups on every exit path."""

    cwd: Path
    environment: Mapping[str, str]

    @contextmanager
    def start(
        self, command: Sequence[str], *, cwd: Path | None = None, stdout: BinaryIO | int | None = None,
    ) -> Iterator[subprocess.Popen[bytes]]:
        process = subprocess.Popen(
            command, cwd=cwd or self.cwd, env=self.environment, start_new_session=True,
            stdout=stdout, stderr=subprocess.STDOUT if stdout is not None else None,
        )

        try:
            yield process

        finally:

            try:
                os.killpg(process.pid, signal.SIGTERM)

            except ProcessLookupError:
                pass

            try:
                process.wait(timeout=5)

            except subprocess.TimeoutExpired:

                try:
                    os.killpg(process.pid, signal.SIGKILL)

                except ProcessLookupError:
                    pass

                process.wait()

    def run(self, command: Sequence[str], *, cwd: Path | None = None, stdout: BinaryIO | int | None = None) -> None:

        with self.start(command, cwd=cwd, stdout=stdout) as process:
            exit_code = process.wait()

            if exit_code:
                raise subprocess.CalledProcessError(exit_code, command)


def load_environment(project_root: Path) -> dict[str, str]:
    env_file = project_root / ".env"

    if not env_file.exists():
        env_file.write_text((project_root / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")

    environment = {**read_envfile(project_root / ".env.example"), **read_envfile(env_file), **os.environ}
    # Child commands select their own environments, not the uv script environment.
    environment.pop("VIRTUAL_ENV", None)
    return environment


@contextmanager
def run_directory(project_root: Path, command: str, parent: str | None) -> Iterator[Path]:

    if parent:
        parent_dir = project_root / Path(parent).expanduser()
        parent_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        yield Path(mkdtemp(prefix="run-", dir=parent_dir))

    else:

        with TemporaryDirectory(prefix=f"saneinteg-{command}-") as directory:
            yield Path(directory)


def registration_code(server: subprocess.Popen[bytes], log_path: Path) -> str:
    deadline = monotonic() + 30

    while monotonic() < deadline:

        if server.poll() is not None:
            raise RuntimeError("sanea stopped before registration became available")

        for line in log_path.read_text(encoding="utf-8").splitlines():

            if line.startswith("SANEA_REGISTRATION_CODE=") and (code := line.partition("=")[2]):
                return code

        sleep(0.1)

    raise RuntimeError("Timed out waiting for the sanea registration code")


def develop(project_root: Path, work_dir: Path, environment: dict[str, str]) -> None:
    execute = Commands(project_root, environment)

    for component in ("sanex", "sanea"):
        execute.run(["ma", "up", "--tool"], cwd=project_root / "components" / component)

    execute.run(["sanea", "migrate", "--noinput"])
    execute.run([
        "sanea", "shell", "--command",
        "from django.contrib.auth import get_user_model; "
        "get_user_model().objects.create_user(\"demo\", password=\"demo\", is_staff=True)",
    ])
    sanex_dir = work_dir / "sanex"
    sanex_dir.mkdir(mode=0o700)
    sanex = ["sanex", "develop", "--state-dir", f"{sanex_dir}"]
    log_path = work_dir / "sanea.log"

    with log_path.open("wb") as server_log, execute.start(
        ["sanea", "serve", "--open-registration"], stdout=server_log,
    ) as server:

        try:
            code = registration_code(server, log_path)

        except RuntimeError:
            logger.error("sanea startup log:\n%s", log_path.read_text(encoding="utf-8"))
            raise

        execute.run([*sanex, "register", code])
        logger.info(
            "Development applications are ready.\nSanea: http://127.0.0.1:%s/\n"
            "Credentials: demo / demo\nSanea log: %s\n"
            "Sanex is running in the foreground. Press Ctrl+C to stop both applications.",
            environment["SANEA_HTTP_PORT"], log_path,
        )
        execute.run([*sanex, "service"])


def run_tests(project_root: Path, environment: dict[str, str], arguments: Sequence[str]) -> None:
    Commands(project_root, environment).run(["uv", "run", "--python", "3.12", "--group", "tests", "pytest", *arguments])


def free_ports() -> tuple[int, ...]:

    with ExitStack() as stack:
        sockets = [stack.enter_context(socket.socket()) for attempt in range(3)]

        for item in sockets:
            item.bind(("127.0.0.1", 0))

        return tuple(item.getsockname()[1] for item in sockets)


def check_listeners(http_port: int, https_port: int) -> None:
    http_url = f"http://127.0.0.1:{http_port}/"
    deadline = monotonic() + 10

    while True:
        try:

            with request.urlopen(http_url, timeout=1) as response:
                body = response.read()

            break

        except OSError as failure:

            if isinstance(failure, error.HTTPError):
                failure.close()

            if monotonic() >= deadline:
                raise

            sleep(0.05)

    if b"Sign in" not in body:
        raise RuntimeError("sanea HTTP listener did not render the sign-in page")

    api_request = request.Request(
        f"https://127.0.0.1:{https_port}/client/sync", data=b"{}",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    # The temporary test CA is not trusted; this checks the client-certificate requirement.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    try:

        with request.urlopen(api_request, context=context, timeout=2):
            raise RuntimeError("sanea accepted a client request without a certificate")

    except error.HTTPError as response_error:
        response_error.close()

        if response_error.code != 403:
            raise


def run_wheel(project_root: Path, work_dir: Path, environment: dict[str, str]) -> None:
    wheel_dir = work_dir / "wheels"
    wheel_dir.mkdir()
    environment = {name: value for name, value in environment.items() if name not in {"PYTHONPATH", "PYTHONHOME"}}
    execute = Commands(work_dir, environment)

    for component in ("sanelib", "sanea", "sanex"):
        execute.run(["uv", "build", "--out-dir", f"{wheel_dir}"], cwd=project_root / "components" / component)

    venv = work_dir / "venv"
    binaries = venv / "bin"
    python = f"{binaries / "python"}"
    execute.run(["uv", "venv", "--python", "3.12", f"{venv}"])
    execute.run(["uv", "pip", "install", "--python", python, *(f"{wheel}" for wheel in sorted(wheel_dir.glob("*.whl")))])
    http_port, https_port, discovery_port = free_ports()
    environment.update({
        "SANEA_SERVER_HOST": "127.0.0.1",
        "SANEA_ALLOWED_HOSTS": json.dumps(["localhost", "127.0.0.1"]),
        "SANEA_HTTP_PORT": f"{http_port}",
        "SANEA_HTTPS_PORT": f"{https_port}",
        "SANEA_DISCOVERY_PORT": f"{discovery_port}",
    })
    # Probe imports with the installed environment's interpreter, not this script's Python.
    execute.run([python, "-c", "import sanea, sanelib, sanex; from sanelib.protocol import Config; assert Config"])

    for command in ("sanea", "sanex", "sanex-indicator"):
        execute.run([f"{binaries / command}", "--version"])
        execute.run([f"{binaries / command}", "--help"], stdout=subprocess.DEVNULL)

    execute.run([f"{binaries / "sanex-window-agent"}", "--help"], stdout=subprocess.DEVNULL)
    sanea = f"{binaries / "sanea"}"
    execute.run([sanea, "migrate", "--noinput", "--verbosity", "0"])
    execute.run([sanea, "check"])
    execute.run([sanea, "findstatic", "sanea/css/app.css", "--verbosity", "0"])

    with (work_dir / "server.log").open("wb") as server_log, execute.start(
        [sanea, "serve", "--verbosity", "0"], stdout=server_log,
    ):
        check_listeners(http_port, https_port)

    logger.info("Wheel and sdist check passed.")


def terminate_on_signal(signal_number: int, frame: FrameType | None) -> None:
    raise SystemExit(128 + signal_number)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("develop", help="start sanea and a safe development sanex client")
    commands.add_parser("tests", help="run integration tests; extra arguments are passed to pytest")
    commands.add_parser("wheel", help="build distributions and exercise installed applications")
    arguments, extra = parser.parse_known_args(argv)

    if extra and arguments.command != "tests":
        parser.error(f"Unexpected arguments: {" ".join(extra)}")

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    previous_handler = signal.signal(signal.SIGTERM, terminate_on_signal)

    try:
        environment = load_environment(PROJECT_ROOT)
        parent_variable = {
            "develop": "SANECMP_DEVELOPMENT_STATE_DIR", "tests": "SANECMP_INTEGRATION_STATE_DIR",
        }.get(arguments.command)
        parent = environment.get(parent_variable) if parent_variable else None

        with run_directory(PROJECT_ROOT, arguments.command, parent) as work_dir:
            state_dir = work_dir / "sanea"
            state_dir.mkdir(mode=0o700)
            environment.update({
                "PYTHON_ENV": "development" if arguments.command == "develop" else "testing",
                "PYTHONUNBUFFERED": "1",
                "SANEA_STATE_DIR": f"{state_dir}",
                "SANEA_DATABASE_PATH": f"{state_dir / "sanea.sqlite3"}",
                "SANEA_PKI_DIR": f"{state_dir / "pki"}",
            })
            logger.info("Run state: %s", work_dir)

            if arguments.command == "develop":
                develop(PROJECT_ROOT, work_dir, environment)

            elif arguments.command == "tests":
                environment["SANECMP_INTEGRATION_STATE_DIR"] = f"{work_dir}"
                run_tests(PROJECT_ROOT, environment, extra[1:] if extra[:1] == ["--"] else extra)

            else:
                run_wheel(PROJECT_ROOT, work_dir, environment)

    except subprocess.CalledProcessError as failure:
        logger.error("Command failed with exit code %d.", failure.returncode)
        return failure.returncode if failure.returncode > 0 else 128 - failure.returncode

    except (OSError, RuntimeError) as failure:
        logger.error("Run failed: %s", failure)
        return 1

    except KeyboardInterrupt:
        logger.error("Run interrupted.")
        return 130

    finally:
        signal.signal(signal.SIGTERM, previous_handler)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
