"""Tests for the event-driven indicator status client."""

import asyncio
import os
import socket
from pathlib import Path

import pytest
from sanelib.protocol import Account, Config

from sanex.exceptions import IndicatorError

from sanex.indicator.client import IndicatorStatusClient
from sanex.indicator.server import IndicatorStatusServer
from sanex.model.indicator import IndicatorStatus
from sanex.model.session import LoginSession, SessionState, SessionType
from sanex.model.window import AtspiWindowSnapshot
from sanex.service.daemon import SanexServiceFactory
from sanex.storage.config import ConfigStore
from sanex.storage.events import EventStore
from sanex.storage.pki import PkiPaths
from sanex.storage.state import RuntimeStateStore


class IndicatorLogind:
    """Supply synthetic session ownership without accessing the system bus."""

    def __init__(self, session: LoginSession) -> None:
        self.session = session
        self.waiting = asyncio.Event()

    async def connect(self) -> None:
        return None

    async def start_sleep_monitoring(self) -> None:
        return None

    async def stop_sleep_monitoring(self) -> None:
        return None

    async def wait_for_sleep_change(self) -> bool:
        self.waiting.set()
        return await asyncio.Event().wait()

    async def read_sessions(self) -> tuple[LoginSession, ...]:
        return (self.session,)

    async def terminate(self, ident: str) -> None:
        raise AssertionError("indicator checks must not terminate a session")

    def close(self) -> None:
        return None


class IndicatorWindowAgents:
    """Require collection to remain disabled throughout the scenario."""

    async def reconcile(self, sessions: tuple[LoginSession, ...]) -> None:
        assert sessions == ()

    async def read_windows(self, session: LoginSession) -> AtspiWindowSnapshot:
        raise AssertionError("indicator checks must not observe windows")

    async def close(self) -> None:
        return None


@pytest.mark.parametrize("configured", [True, False], ids=["collection-disabled", "unconfigured-account"])
def test_service_provides_indicator_status_without_account_collection(tmp_path: Path, configured: bool) -> None:
    asyncio.run(scenario_service_provides_indicator_status_without_account_collection(tmp_path, configured))


async def scenario_service_provides_indicator_status_without_account_collection(tmp_path: Path, configured: bool) -> None:
    uid = os.getuid()
    config = Config(
        ident=1, timezone="UTC", sync_interval=300, discovery_interval=30, walk_interval=1,
        save_interval=5, min_prc_duration=5, ignored_prcs=(),
        accounts=(Account(uid=uid, collect=False, apply=False, limits=None),) if configured else (),
    )
    config_store = ConfigStore(tmp_path / "config" / "config.json")
    config_store.path.parent.mkdir(mode=0o700)
    config_store.save(config.model_dump_json())
    boot_path = tmp_path / "boot_id"
    boot_path.write_text("synthetic-boot\n")
    session = LoginSession(
        ident="3", uid=uid, login="synthetic-user", path="/session/3", started=1_790_956_700,
        type=SessionType.WAYLAND, state=SessionState.ACTIVE, active=True, idle=False, locked=False,
    )
    logind = IndicatorLogind(session)
    agents = IndicatorWindowAgents()
    factory = SanexServiceFactory(
        config_store=config_store, state_store=RuntimeStateStore(tmp_path / "accounts"),
        event_store=EventStore(tmp_path / "accounts"), boot_ident_path=boot_path,
        pki_paths=PkiPaths(tmp_path / "pki"), command_state_path=tmp_path / "commands.json",
        update_state_path=tmp_path / "update.json", indicator_socket_path=tmp_path / "indicator.sock",
        indicator_socket_mode=0o600, require_root=False, enforcement_enabled=False, update_enabled=False,
        logind_factory=lambda: logind, window_agents_factory=lambda path: agents,
    )
    service = factory.create()
    service.install_signal_handlers = False
    server = service.indicator_server
    assert isinstance(server, IndicatorStatusServer)
    connected = asyncio.Event()
    authorize_peer = server.authorize

    def authorize(uid: int, sess_ident: str) -> bool:
        allowed = authorize_peer(uid, sess_ident)

        if allowed:
            connected.set()

        return allowed

    server.authorize = authorize
    service_task = asyncio.create_task(service.run())
    stop_requested = asyncio.Event()
    client_task = None
    received: list[IndicatorStatus | None] = []

    def receive(status: IndicatorStatus | None) -> None:
        received.append(status)

        if status is not None:
            stop_requested.set()

    try:
        await asyncio.wait_for(logind.waiting.wait(), timeout=5)
        client = IndicatorStatusClient("3", path=server.path)
        client_task = asyncio.create_task(client.run(stop_requested, receive))
        await asyncio.wait_for(connected.wait(), timeout=5)
        await service.apply_config(config.model_copy(update={"ident": 2}).model_dump_json())
        await asyncio.wait_for(client_task, timeout=5)

        assert received == [None, IndicatorStatus(remaining=None, break_duration=0)]
        assert all(runtime.state.sess_runs == [] for runtime in service.runtimes.values())
        reader, writer = await asyncio.open_unix_connection(server.path)
        try:
            writer.write(b"{\"sess_ident\":\"foreign\"}\n")
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), timeout=5) == b""
        finally:
            writer.close()
            await writer.wait_closed()

    finally:
        stop_requested.set()
        service.request_stop()

        if client_task is not None:
            await asyncio.wait_for(client_task, timeout=5)

        await asyncio.wait_for(service_task, timeout=5)

    assert not server.path.exists()


def test_reports_unavailable_then_receives_pushed_status(tmp_path: Path) -> None:
    asyncio.run(scenario_reports_unavailable_then_receives_pushed_status(tmp_path))


async def scenario_reports_unavailable_then_receives_pushed_status(tmp_path: Path) -> None:
    path = tmp_path / "indicator.sock"
    stop_requested = asyncio.Event()
    changed = asyncio.Event()
    received: list[IndicatorStatus | None] = []

    def receive(status: IndicatorStatus | None) -> None:
        received.append(status)
        changed.set()

    client = IndicatorStatusClient(
        sess_ident="3",
        path=path,
        reconnect_delays=(1,),
    )
    task = asyncio.create_task(client.run(stop_requested, receive))
    await changed.wait()
    assert received == [None]

    connected = asyncio.Event()

    def authorize(uid: int, sess_ident: str) -> bool:
        connected.set()
        return sess_ident == "3"

    status = IndicatorStatus(remaining=3600, break_duration=7200)
    server = IndicatorStatusServer(
        path=path,
        mode=0o600,
        authorize=authorize,
        provide_status=lambda uid, sess_ident, timestamp: status,
    )
    await server.start()
    await asyncio.wait_for(connected.wait(), timeout=5)
    server.publish(1_790_956_800)

    while received[-1] is None:
        changed.clear()
        await asyncio.wait_for(changed.wait(), timeout=5)

    assert received == [None, status]

    stop_requested.set()
    await asyncio.wait_for(task, timeout=5)
    await server.close()


def test_reports_disconnect_once_and_reconnects(tmp_path: Path) -> None:
    asyncio.run(scenario_reports_disconnect_once_and_reconnects(tmp_path))


async def scenario_reports_disconnect_once_and_reconnects(tmp_path: Path) -> None:
    path = tmp_path / "indicator.sock"
    status = IndicatorStatus(remaining=None, break_duration=0)
    connected = asyncio.Event()

    def authorize(uid: int, sess_ident: str) -> bool:
        connected.set()
        return True

    server = IndicatorStatusServer(
        path=path,
        mode=0o600,
        authorize=authorize,
        provide_status=lambda uid, sess_ident, timestamp: status,
    )
    await server.start()
    stop_requested = asyncio.Event()
    changed = asyncio.Event()
    received: list[IndicatorStatus | None] = []

    def receive(value: IndicatorStatus | None) -> None:
        received.append(value)
        changed.set()

    client = IndicatorStatusClient(
        sess_ident="3",
        path=path,
        reconnect_delays=(1,),
    )
    task = asyncio.create_task(client.run(stop_requested, receive))
    await asyncio.wait_for(connected.wait(), timeout=5)
    changed.clear()
    server.publish(1_790_956_800)
    await asyncio.wait_for(changed.wait(), timeout=5)
    assert received[-1] == status

    await server.close()

    while received[-1:] != [None]:
        changed.clear()
        await asyncio.wait_for(changed.wait(), timeout=5)

    assert received == [None, status, None]

    stop_requested.set()
    await asyncio.wait_for(task, timeout=5)


def test_publishes_only_changed_status_to_peer_session(tmp_path: Path) -> None:
    asyncio.run(scenario_publishes_only_changed_status_to_peer_session(tmp_path))


async def scenario_publishes_only_changed_status_to_peer_session(tmp_path: Path) -> None:
    authorized = asyncio.Event()
    peer_uids: list[int] = []
    current = [IndicatorStatus(remaining=3600, break_duration=7200)]

    def authorize(uid: int, sess_ident: str) -> bool:
        peer_uids.append(uid)
        authorized.set()
        return sess_ident == "3"

    server = IndicatorStatusServer(
        path=tmp_path / "run" / "indicator.sock",
        mode=0o600,
        authorize=authorize,
        provide_status=lambda uid, sess_ident, timestamp: current[0],
    )
    await server.start()
    reader, writer = await asyncio.open_unix_connection(server.path)
    writer.write(b"{\"sess_ident\":\"3\"}\n")
    await writer.drain()
    await authorized.wait()

    server.publish(1_790_956_800)
    first = await asyncio.wait_for(reader.readline(), timeout=5)
    current[0] = IndicatorStatus(remaining=3599, break_duration=7200)
    server.publish(1_790_956_801)

    assert first == IndicatorStatus(remaining=3600, break_duration=7200).encode()
    assert peer_uids == [os.getuid()]
    current[0] = IndicatorStatus(remaining=3540, break_duration=7200)
    server.publish(1_790_956_860)
    changed = await asyncio.wait_for(reader.readline(), timeout=5)
    assert changed == current[0].encode()

    writer.close()
    await writer.wait_closed()
    await server.close()
    assert not server.path.exists()


def test_rejects_session_not_owned_by_peer(tmp_path: Path) -> None:
    asyncio.run(scenario_rejects_session_not_owned_by_peer(tmp_path))


async def scenario_rejects_session_not_owned_by_peer(tmp_path: Path) -> None:
    server = IndicatorStatusServer(
        path=tmp_path / "indicator.sock",
        mode=0o600,
        authorize=lambda uid, sess_ident: False,
        provide_status=lambda uid, sess_ident, timestamp: None,
    )
    await server.start()
    reader, writer = await asyncio.open_unix_connection(server.path)
    writer.write(b"{\"sess_ident\":\"foreign\"}\n")
    await writer.drain()

    assert await asyncio.wait_for(reader.read(), timeout=5) == b""

    writer.close()
    await writer.wait_closed()
    await server.close()


def test_replaces_stale_socket_but_not_active_server(tmp_path: Path) -> None:
    asyncio.run(scenario_replaces_stale_socket_but_not_active_server(tmp_path))


async def scenario_replaces_stale_socket_but_not_active_server(tmp_path: Path) -> None:
    path = tmp_path / "indicator.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(f"{path}")
    stale.close()
    server = IndicatorStatusServer(
        path=path,
        mode=0o600,
        authorize=lambda uid, sess_ident: False,
        provide_status=lambda uid, sess_ident, timestamp: None,
    )

    await server.start()
    competing = IndicatorStatusServer(
        path=path,
        mode=0o600,
        authorize=lambda uid, sess_ident: False,
        provide_status=lambda uid, sess_ident, timestamp: None,
    )

    with pytest.raises(IndicatorError, match="already accepting"):
        await competing.start()

    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(b"{\"sess_ident\":\"3\"}\n")
    await writer.drain()
    assert await asyncio.wait_for(reader.read(), timeout=5) == b""

    writer.close()
    await writer.wait_closed()
    await server.close()
