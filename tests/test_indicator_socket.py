"""Tests for the event-driven indicator status client."""

import asyncio
import os
import socket
from pathlib import Path

import pytest

from sanex.exceptions import IndicatorError

from sanex.indicator.client import IndicatorStatusClient
from sanex.indicator.server import IndicatorStatusServer
from sanex.model.indicator import IndicatorStatus


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
