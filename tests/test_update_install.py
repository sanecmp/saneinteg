"""Install a synthetic sanex release through the real uv update command."""

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import zipfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from sanelib.protocol import Command, CommandStatus

from sanex.sync.commands import CommandStore
from sanex.sync.update import (
    InstallConfigStore,
    UpdateHandler,
    UpdateRecovery,
    UpdateStateStore,
)


OLD_VERSION = "9.9.8"
NEW_VERSION = "9.9.9"


class PassthroughExecutableValidator:
    """Allow executables in the unprivileged integration environment."""

    def validate(self, path: str) -> str:
        return path


class SubprocessReplacer:
    """Run the replacement command to completion without replacing pytest."""

    def replace(
        self,
        executable: str,
        arguments: Sequence[str],
        environment: Mapping[str, str],
    ) -> None:
        completed = subprocess.run(
            arguments,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr


class QuietRequestHandler(SimpleHTTPRequestHandler):
    def log_message(self, format_: str, *arguments: object) -> None:
        return None


def test_installs_exact_release_from_temporary_https_index(
    tmp_path: Path,
) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    tool_dir = tmp_path / "bundle"
    tool_bin_dir = tmp_path / "bin"
    old_wheel = build_wheel(tmp_path, OLD_VERSION)
    base_environment = make_environment(tool_dir, tool_bin_dir)
    install_wheel(uv, old_wheel, base_environment)
    assert read_installed_version(tool_bin_dir) == OLD_VERSION

    new_wheel = build_wheel(tmp_path, NEW_VERSION)
    index_root = tmp_path / "index"
    publish_wheel(index_root, new_wheel)
    ca_certificate, server_certificate, private_key = issue_server_certificate(
        tmp_path
    )

    with serve_index(index_root, server_certificate, private_key) as index_url:
        install_path = tmp_path / "install.json"
        install_path.write_text(
            json.dumps({"uv": uv, "python": sys.executable}),
            encoding="utf-8",
        )
        state_store = UpdateStateStore(tmp_path / "update.json")
        command_store = CommandStore(tmp_path / "commands.json")
        command = Command(
            ident=73,
            type="update",
            payload={"version": NEW_VERSION, "index_url": index_url},
        )
        command_store.accept((), (command,))

        async def prepare_update() -> None:
            return None

        handler = UpdateHandler(
            before_exec=prepare_update,
            installed_version=lambda: OLD_VERSION,
            install_store=InstallConfigStore(install_path),
            state_store=state_store,
            executable_validator=PassthroughExecutableValidator(),
            process_replacer=SubprocessReplacer(),
            environment=lambda: {
                **base_environment,
                "SSL_CERT_FILE": f"{ca_certificate}",
            },
            tool_dir=f"{tool_dir}",
            tool_bin_dir=f"{tool_bin_dir}",
        )

        result = asyncio.run(handler.run(command))

    assert result.status is CommandStatus.FAILED
    assert result.error == "uv process replacement returned"
    state = state_store.load()
    assert state is not None
    assert state.version == NEW_VERSION
    assert state.index_url == index_url
    assert state.attempts == 1
    assert read_installed_version(tool_bin_dir) == NEW_VERSION

    UpdateRecovery(
        command_store,
        state_store,
        lambda: read_installed_version(tool_bin_dir),
    ).reconcile()

    assert state_store.load() is None
    assert not command_store.pending
    assert command_store.results[0].status is CommandStatus.DONE


def build_wheel(directory: Path, version: str) -> Path:
    """Build a dependency-free fixture wheel without invoking project tooling."""
    wheel = directory / f"sanex-{version}-py3-none-any.whl"
    distribution = f"sanex-{version}.dist-info"
    files = {
        "sanex/__init__.py": "",
        "sanex/cli.py": (
            "from importlib.metadata import version\n\n"
            "def main():\n"
            "    print(version('sanex'))\n"
        ),
        f"{distribution}/METADATA": (
            "Metadata-Version: 2.3\n"
            "Name: sanex\n"
            f"Version: {version}\n"
        ),
        f"{distribution}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: sanecmp-integration\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
        f"{distribution}/entry_points.txt": (
            "[console_scripts]\n"
            "sanex = sanex.cli:main\n"
        ),
    }
    record_path = f"{distribution}/RECORD"
    records = [wheel_record(path, content.encode()) for path, content in files.items()]
    records.append(f"{record_path},,")
    files[record_path] = "\n".join(records) + "\n"
    with zipfile.ZipFile(wheel, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return wheel


def wheel_record(path: str, content: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
    return f"{path},sha256={digest.decode()},{len(content)}"


def make_environment(tool_dir: Path, tool_bin_dir: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment["UV_TOOL_DIR"] = f"{tool_dir}"
    environment["UV_TOOL_BIN_DIR"] = f"{tool_bin_dir}"
    return environment


def install_wheel(uv: str, wheel: Path, environment: Mapping[str, str]) -> None:
    subprocess.run(
        (
            uv,
            "tool",
            "install",
            "--force",
            "--no-cache",
            "--no-config",
            "--no-sources",
            "--no-managed-python",
            "--no-python-downloads",
            "--no-progress",
            "--color",
            "never",
            "--python",
            sys.executable,
            f"{wheel}",
        ),
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def read_installed_version(tool_bin_dir: Path) -> str:
    output = subprocess.check_output(
        (tool_bin_dir / "sanex",),
        text=True,
    )
    return output.strip()


def publish_wheel(index_root: Path, wheel: Path) -> None:
    package_directory = index_root / "packages"
    simple_directory = index_root / "simple" / "sanex"
    package_directory.mkdir(parents=True)
    simple_directory.mkdir(parents=True)
    published = package_directory / wheel.name
    shutil.copyfile(wheel, published)
    digest = hashlib.sha256(published.read_bytes()).hexdigest()
    simple_directory.joinpath("index.html").write_text(
        f'<a href="../../packages/{wheel.name}#sha256={digest}">{wheel.name}</a>\n',
        encoding="utf-8",
    )


def issue_server_certificate(directory: Path) -> tuple[Path, Path, Path]:
    ca_private_key = ec.generate_private_key(ec.SECP256R1())
    ca_subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "sanecmp integration CA")]
    )
    now = datetime.now(UTC)
    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_subject)
        .issuer_name(ca_subject)
        .public_key(ca_private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=10))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_private_key, hashes.SHA256())
    )
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    server_certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=10))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_private_key, hashes.SHA256())
    )
    ca_certificate_path = directory / "index-ca.crt"
    certificate_path = directory / "index-server.crt"
    private_key_path = directory / "index.key"
    ca_certificate_path.write_bytes(
        ca_certificate.public_bytes(serialization.Encoding.PEM)
    )
    certificate_path.write_bytes(
        server_certificate.public_bytes(serialization.Encoding.PEM)
    )
    private_key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_certificate_path, certificate_path, private_key_path


@contextmanager
def serve_index(
    directory: Path,
    certificate: Path,
    private_key: Path,
) -> Iterator[str]:
    handler = partial(QuietRequestHandler, directory=f"{directory}")
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, private_key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"https://localhost:{port}/simple"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
