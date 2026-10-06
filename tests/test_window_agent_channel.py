"""Tests for the private root-to-window-agent transport."""

import asyncio
import socket
import os
from pathlib import Path

import pytest

from sanex.service.window_agents import WindowAgentManager

from sanex.exceptions import WindowAgentError
from sanex.model.session import LoginSession, SessionState, SessionType
from sanex.model.window import AtspiWindow, AtspiWindowSnapshot, WindowRole
from sanex.platform.window_agent import (
    WindowAgentClient,
    WindowAgentPool,
    WindowAgentServer,
)


class FakeBackend:
    def __init__(self, windows: tuple[AtspiWindow, ...]) -> None:
        self.snapshot = AtspiWindowSnapshot(windows=windows)
        self.closed: list[AtspiWindow] = []

    async def windows(self) -> AtspiWindowSnapshot:
        return self.snapshot

    async def close_window(self, window: AtspiWindow) -> bool:
        self.closed.append(window)
        return True


def login_session(uid: int = 1001, ident: str = "3") -> LoginSession:
    return LoginSession(
        ident=ident,
        uid=uid,
        login="child",
        path=f"/session/{ident}",
        started=1_790_842_334,
        type=SessionType.WAYLAND,
        state=SessionState.ACTIVE,
        active=True,
        idle=False,
        locked=False,
    )


def sample_window() -> AtspiWindow:
    return AtspiWindow(
        bus=":1.188",
        path="/org/a11y/atspi/accessible/124",
        pid=6443,
        title="Synthetic window",
        role=WindowRole.FRAME,
    )


async def stream_pair() -> tuple[
    asyncio.StreamReader,
    asyncio.StreamWriter,
    asyncio.StreamReader,
    asyncio.StreamWriter,
]:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    left.setblocking(False)
    right.setblocking(False)
    root_reader, root_writer = await asyncio.open_connection(sock=left)
    agent_reader, agent_writer = await asyncio.open_connection(sock=right)
    return root_reader, root_writer, agent_reader, agent_writer


def test_client_and_server_exchange_snapshot_and_close_request() -> None:
    asyncio.run(scenario_client_and_server_exchange_snapshot_and_close_request())


async def scenario_client_and_server_exchange_snapshot_and_close_request() -> None:
    root_reader, root_writer, agent_reader, agent_writer = await stream_pair()
    window = sample_window()
    backend = FakeBackend((window,))
    server = WindowAgentServer("3", backend, agent_reader, agent_writer)
    task = asyncio.create_task(server.serve())
    client = WindowAgentClient(1001, "3", root_reader, root_writer)

    snapshot = await client.windows(login_session())
    accepted = await client.close_window(1001, "3", window)
    await client.close()
    await task
    agent_writer.close()
    await agent_writer.wait_closed()

    assert snapshot.windows == (window,)
    assert accepted
    assert backend.closed == [window]


class FakeProcess:
    def __init__(self, task: asyncio.Task[None]) -> None:
        self.task = task
        self.stderr: asyncio.StreamReader | None = None

    @property
    def returncode(self) -> int | None:

        if not self.task.done():
            return None

        if self.task.cancelled():
            return -15

        return 1 if self.task.exception() is not None else 0

    async def wait(self) -> int:
        try:
            await self.task

        except asyncio.CancelledError:
            return -15

        except Exception:
            return 1

        return 0

    def terminate(self) -> None:
        self.task.cancel()

    def kill(self) -> None:
        self.task.cancel()


class FakeLauncher:
    def __init__(self, window: AtspiWindow) -> None:
        self.window = window
        self.sessions: list[LoginSession] = []
        self.backends: list[FakeBackend] = []
        self.processes: list[FakeProcess] = []

    async def __call__(self, session: LoginSession, fd: int) -> FakeProcess:
        agent_socket = socket.socket(fileno=os.dup(fd))
        agent_socket.setblocking(False)
        backend = FakeBackend((self.window,))

        async def run() -> None:
            reader, writer = await asyncio.open_connection(sock=agent_socket)
            try:
                await WindowAgentServer(
                    session.ident,
                    backend,
                    reader,
                    writer,
                ).serve()
            finally:
                writer.close()
                await writer.wait_closed()

        process = FakeProcess(asyncio.create_task(run()))
        self.sessions.append(session)
        self.backends.append(backend)
        self.processes.append(process)
        return process


def manager(launcher: FakeLauncher) -> WindowAgentManager:
    """Build a manager around the in-process agent launcher."""
    return WindowAgentManager(
        executable=Path("/synthetic/sanex-window-agent"),
        launcher=launcher,
    )


def test_reconcile_routes_requests_and_stops_removed_agent() -> None:
    asyncio.run(scenario_reconcile_routes_requests_and_stops_removed_agent())


async def scenario_reconcile_routes_requests_and_stops_removed_agent() -> None:
    session = login_session()
    window = sample_window()
    launcher = FakeLauncher(window)
    agents = manager(launcher)

    await agents.reconcile((session,))

    assert (await agents.windows(session)).windows == (window,)
    assert await agents.close_window(session.uid, session.ident, window)
    assert launcher.backends[0].closed == [window]

    await agents.reconcile(())

    assert agents.pool.clients == {}
    assert launcher.processes[0].returncode == 0


def test_reconcile_replaces_agent_when_session_uid_changes() -> None:
    asyncio.run(scenario_reconcile_replaces_agent_when_session_uid_changes())


async def scenario_reconcile_replaces_agent_when_session_uid_changes() -> None:
    first = login_session()
    replacement = first.model_copy(update={"uid": first.uid + 1})
    launcher = FakeLauncher(sample_window())
    agents = manager(launcher)

    await agents.reconcile((first,))
    await agents.reconcile((replacement,))

    assert [session.uid for session in launcher.sessions] == [first.uid, replacement.uid]
    assert agents.pool.clients[first.ident].uid == replacement.uid
    assert launcher.processes[0].returncode == 0

    await agents.close()


def test_reconcile_restarts_exited_agent() -> None:
    asyncio.run(scenario_reconcile_restarts_exited_agent())


async def scenario_reconcile_restarts_exited_agent() -> None:
    session = login_session()
    launcher = FakeLauncher(sample_window())
    agents = manager(launcher)

    await agents.reconcile((session,))
    launcher.processes[0].terminate()
    await launcher.processes[0].wait()
    await agents.reconcile((session,))

    assert len(launcher.processes) == 2
    assert agents.pool.clients[session.ident].uid == session.uid

    await agents.close()
