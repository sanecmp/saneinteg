"""Network-level registration and synchronization between sanea and sanex."""

import asyncio
import hashlib
import os
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from asgiref.sync import sync_to_async
from django.core.wsgi import get_wsgi_application
from django.db import connections
from sanelib.protocol import (
    Actor,
    DiscoveredAccount,
    EndMeta,
    EventType,
    MAX_EVENTS_PER_PACKET,
    SessionEndEvent,
    SessionStartEvent,
    SessionStartMeta,
    SyncRequest,
    SyncResponse,
    UpdateCommandPayload,
)

from sanea.core.models import (
    ActivityEvent,
    AppRule,
    ClientCommand,
    Computer,
    EventPacket,
    GlobalUpdate,
    IdentifierSequence,
    Limits,
    LimitsTemplate,
    MatchType,
    Person,
    Range,
    SessionRule,
)
from sanea.core.services.registration_window import registration_window
from sanea.network.server import ServerRunner, create_servers
from sanea.core.models.prc_exclusion import DEFAULT_IGNORED_PRCS
from sanex.accounting.app import AppQuota, AppRuleMatcher
from sanex.accounting.runtime import AccountRuntime
from sanex.accounting.schedule import ScheduleResolver
from sanex.accounting.session import SessionLimit, SessionQuota
from sanex.service.sync import ServiceSyncState
from sanex.service.runtime import RuntimeRepository
from sanex.model.state import RangeOccurrence
from sanex.storage.config import ConfigStore
from sanex.storage.events import EventPacket as LocalEventPacket
from sanex.storage.events import EventStore
from sanex.storage.pki import PkiPaths, RegistrationPkiStore
from sanex.storage.state import RuntimeStateStore
from sanex.sync.client import EventUploadResult, LogUploadResult, SyncHttpClient
from sanex.sync.commands import CommandExecutor, CommandStore
from sanex.sync.discovery import SaneaDiscovery
from sanex.sync.exchange import SyncExchange
from sanex.sync.log import TechnicalLog
from sanex.sync.registration import RegistrationService


@dataclass(frozen=True, slots=True)
class _Accounts:
    snapshot: tuple[DiscoveredAccount, ...]

    async def discover(self) -> tuple[DiscoveredAccount, ...]:
        return self.snapshot


@dataclass(frozen=True, slots=True)
class _RunningSanea:
    discovery_port: int
    https_port: int

    @property
    def base_url(self) -> str:
        return f"https://127.0.0.1:{self.https_port}"


@dataclass(frozen=True, slots=True)
class _RegisteredSanex:
    config_store: ConfigStore
    pki_paths: PkiPaths


@dataclass(slots=True)
class _LoseFirstEventResponse:
    """Let sanea store one packet, then emulate losing its HTTP response."""

    client: SyncHttpClient
    lost: bool = False

    async def sync(self, base_url: str, request: SyncRequest) -> SyncResponse:
        return await self.client.sync(base_url, request)

    async def upload_log(self, base_url: str, content: bytes) -> LogUploadResult:
        return await self.client.upload_log(base_url, content)

    async def upload_events(
        self,
        base_url: str,
        packet: LocalEventPacket,
    ) -> EventUploadResult:
        result = await self.client.upload_events(base_url, packet)
        if not self.lost:
            self.lost = True
            raise httpx.ReadError("simulated lost event-upload response")
        return result


@dataclass(frozen=True, slots=True)
class _KeepCommandsPending:
    """Leave received commands untouched while testing server cancellation."""

    async def run(self) -> None:
        return None


def _reserve_ports(count: int) -> tuple[int, ...]:
    sockets = []
    try:
        for _ in range(count):
            reserved = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            reserved.bind(("127.0.0.1", 0))
            sockets.append(reserved)
        return tuple(reserved.getsockname()[1] for reserved in sockets)
    finally:
        for reserved in sockets:
            reserved.close()


def _wait_for_listener(port: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            connection = socket.create_connection(("127.0.0.1", port), timeout=0.1)
        except OSError:
            time.sleep(0.02)
            continue
        connection.close()
        return
    raise AssertionError(f"sanea listener did not start on port {port}")


def _reserve_udp_port() -> int:
    reserved = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        reserved.bind(("127.0.0.1", 0))
        return reserved.getsockname()[1]
    finally:
        reserved.close()


def _create_account_limits() -> Limits:
    limits = Limits.objects.create(name="Integration limits")
    session_rule = SessionRule(
        limits=limits,
        ident=17,
        name="Study session",
        apply=True,
        max_sessions=2,
        max_duration=3_600,
        break_duration=900,
    )
    session_rule.full_clean()
    session_rule.save()
    app_rule = AppRule(
        session_rule=session_rule,
        ident=23,
        name="Study browser",
        apply=True,
        max_launches=1,
        max_time=1_200,
        prc_name="firefox",
        prc_name_match=MatchType.EXACT,
        wnd_title="Math lesson",
        wnd_title_match=MatchType.CONTAINS,
    )
    app_rule.full_clean()
    app_rule.save()
    range_ = Range(
        limits=limits,
        ident=11,
        apply=True,
        weekday=0,
        since=480,
        till=720,
        session_rule=session_rule,
    )
    range_.full_clean()
    range_.save()
    return limits


def _prepare_server_changes() -> tuple[int, int, int]:
    computer = Computer.objects.get()
    account = computer.accounts.get(uid=1001)
    limits = _create_account_limits()
    template = LimitsTemplate.objects.create(
        name=limits.name,
        current_limits=limits,
    )
    person = Person.objects.create(name="Alex", limits_tpl=template)
    account.person = person
    account.limits = limits
    account.collect = True
    account.apply = True
    account.full_clean()
    account.save(
        update_fields=("person", "limits", "collect", "apply", "updated")
    )
    config = computer.materialize_config()
    command = ClientCommand.objects.create(
        computer=computer,
        type="diagnostic",
        payload={"scope": "integration"},
    )
    return computer.pk, config.ident, command.pk


def _update_and_propagate_template() -> tuple[int, int]:
    """Advance a template while preserving existing accounting identities."""
    computer = Computer.objects.get()
    account = computer.accounts.select_related("limits", "person").get(uid=1001)
    source = account.limits
    assert source is not None
    target = source.checkout_for_edit()

    session_rule = target.session_rules.get(ident=17)
    session_rule.name = "Updated study session"
    session_rule.save(update_fields=("name", "updated"))
    app_rule = session_rule.app_rules.get(ident=23)
    app_rule.name = "Updated study browser"
    app_rule.max_launches = 2
    app_rule.save(update_fields=("name", "max_launches", "updated"))

    new_app_ident = IdentifierSequence.allocate(AppRule)
    new_app_rule = AppRule(
        session_rule=session_rule,
        ident=new_app_ident,
        name="New study tool",
        apply=True,
        max_launches=1,
        max_time=600,
        prc_name="calculator",
        prc_name_match=MatchType.EXACT,
    )
    new_app_rule.full_clean()
    new_app_rule.save()

    person = account.person
    assert person is not None
    assert person.propagate_template([account]) == 1
    config = computer.configs.order_by("-ident").first()
    assert config is not None
    return config.ident, new_app_ident


def _replace_and_propagate_app_rule() -> tuple[int, int]:
    """Delete one rule and distribute a replacement with a fresh identity."""
    computer = Computer.objects.get()
    account = computer.accounts.select_related("limits", "person").get(uid=1001)
    source = account.limits
    assert source is not None
    target = source.checkout_for_edit()
    session_rule = target.session_rules.get(ident=17)
    session_rule.app_rules.get(ident=23).delete()

    replacement_ident = IdentifierSequence.allocate(AppRule)
    replacement = AppRule(
        session_rule=session_rule,
        ident=replacement_ident,
        name="Replacement study browser",
        apply=True,
        max_launches=2,
        max_time=1_200,
        prc_name="firefox",
        prc_name_match=MatchType.EXACT,
        wnd_title="Math lesson",
        wnd_title_match=MatchType.CONTAINS,
    )
    replacement.full_clean()
    replacement.save()

    person = account.person
    assert person is not None
    assert person.propagate_template([account]) == 1
    config = computer.configs.order_by("-ident").first()
    assert config is not None
    return config.ident, replacement_ident


def _change_range_session_rule() -> tuple[int, int]:
    """Assign a fresh rule without changing the range budget identity."""
    computer = Computer.objects.get()
    account = computer.accounts.select_related("limits", "person").get(uid=1001)
    source = account.limits
    assert source is not None
    target = source.checkout_for_edit()

    session_ident = IdentifierSequence.allocate(SessionRule)
    session_rule = SessionRule(
        limits=target,
        ident=session_ident,
        name="Replacement session rule",
        apply=True,
        max_sessions=2,
        max_duration=3_600,
        break_duration=900,
    )
    session_rule.full_clean()
    session_rule.save()
    range_ = target.ranges.get(ident=11)
    range_.session_rule = session_rule
    range_.full_clean()
    range_.save(update_fields=("session_rule", "updated"))

    person = account.person
    assert person is not None
    assert person.propagate_template([account]) == 1
    config = computer.configs.order_by("-ident").first()
    assert config is not None
    return config.ident, session_ident


def _replace_and_propagate_range(session_ident: int) -> tuple[int, int]:
    """Replace one range and distribute it with a fresh budget identity."""
    computer = Computer.objects.get()
    account = computer.accounts.select_related("limits", "person").get(uid=1001)
    source = account.limits
    assert source is not None
    target = source.checkout_for_edit()
    target.ranges.get(ident=11).delete()
    session_rule = target.session_rules.get(ident=session_ident)

    range_ident = IdentifierSequence.allocate(Range)
    range_ = Range(
        limits=target,
        ident=range_ident,
        apply=True,
        weekday=0,
        since=480,
        till=720,
        session_rule=session_rule,
    )
    range_.full_clean()
    range_.save()

    person = account.person
    assert person is not None
    assert person.propagate_template([account]) == 1
    config = computer.configs.order_by("-ident").first()
    assert config is not None
    return config.ident, range_ident


def _assert_server_results(
    computer_id: int,
    config_ident: int,
    command_id: int,
) -> None:
    computer = Computer.objects.get(pk=computer_id)
    command = ClientCommand.objects.get(pk=command_id)
    events = list(
        ActivityEvent.objects.filter(account__computer=computer).order_by("seq")
    )

    assert computer.hostname == "integration-child-renamed"
    assert computer.version == "0.1.1"
    assert computer.status == "allowed"
    assert computer.current_config_id == config_ident
    assert computer.applied_config_id == config_ident
    assert computer.log_tail == "integration exchange completed\n"
    assert list(
        computer.accounts.order_by("uid").values_list(
            "uid",
            "login",
            "name",
            "collect",
        )
    ) == [
        (1001, "child", "Alex", True),
        (1002, "parent", "Parent", False),
    ]
    assert command.status == "failed"
    assert command.error == "unsupported command type: diagnostic"
    assert command.completed is not None
    assert EventPacket.objects.filter(account__computer=computer).count() == 1
    assert [(event.seq, event.type, event.duration) for event in events] == [
        (1, "session_start", None),
        (2, "session_end", 120),
    ]


def _assert_server_has_no_events() -> None:
    assert EventPacket.objects.count() == 0
    assert ActivityEvent.objects.count() == 0


def _assert_server_has_one_event_packet() -> None:
    assert EventPacket.objects.count() == 1
    assert ActivityEvent.objects.count() == 2


def _activate_global_update() -> tuple[int, int]:
    connections.close_all()
    result = GlobalUpdate.activate(
        UpdateCommandPayload(
            version="9.8.7",
            index_url="https://packages.example.test/simple",
        )
    )
    return result.update.pk, result.created


def _cancel_global_update(update_id: int) -> int:
    connections.close_all()
    return GlobalUpdate.objects.get(pk=update_id).cancel()


def _assert_global_update_cancelled(update_id: int) -> None:
    connections.close_all()
    update = GlobalUpdate.objects.get(pk=update_id)
    assert update.active is False
    assert update.cancelled is not None
    assert update.commands.count() == 0


async def _register_sanex(
    running_sanea: _RunningSanea,
    sanex_root: Path,
    accounts: tuple[DiscoveredAccount, ...],
    hostname: str,
) -> _RegisteredSanex:
    pki_root = sanex_root / "pki"
    config_store = ConfigStore(sanex_root / "config.json")
    registration = RegistrationService(
        pki=RegistrationPkiStore(
            permanent_root=pki_root,
            pending_root=sanex_root / "pending-pki",
        ),
        config_store=config_store,
        discovery=SaneaDiscovery(
            port=running_sanea.discovery_port,
            targets=("127.0.0.1",),
        ),
        accounts=_Accounts(accounts),
        hostname=lambda: hostname,
        version=lambda: "0.1.0",
    )
    code = registration_window.open().code
    initial_config = await registration.run(code)
    assert config_store.load() == initial_config
    pki_paths = PkiPaths(pki_root)
    certificate = ssl.PEM_cert_to_DER_cert(pki_paths.certificate.read_text())
    await sync_to_async(_assert_registered_fingerprint)(
        hostname, hashlib.sha256(certificate).hexdigest()
    )
    return _RegisteredSanex(config_store, pki_paths)


def _assert_registered_fingerprint(hostname: str, fingerprint: str) -> None:
    # Each scenario recreates SQLite while the asynchronous worker survives.
    connections.close_all()
    computer = Computer.objects.get(hostname=hostname)
    assert computer.certificate_fingerprint == fingerprint


def _allow_sandbox_pki(monkeypatch) -> None:
    """Ignore only the overflow UID exposed by a sandboxed filesystem root."""
    if Path("/").stat().st_uid not in {0, os.geteuid()}:
        monkeypatch.setattr(
            RegistrationPkiStore,
            "_validate_directory_chain",
            lambda self, directory: None,
        )


def _enable_collection(hostnames: tuple[str, ...]) -> None:
    # pytest recreates the file-backed database between scenarios while the
    # sync_to_async worker thread survives and may retain the old file handle.
    connections.close_all()
    for hostname in hostnames:
        computer = Computer.objects.get(hostname=hostname)
        for account in computer.accounts.all():
            account.collect = True
            account.save(update_fields=("collect", "updated"))


def _enable_multi_account_limits(hostname: str) -> None:
    connections.close_all()
    computer = Computer.objects.get(hostname=hostname)
    accounts = tuple(computer.accounts.order_by("uid"))
    assert len(accounts) == 2
    first_limits = _create_account_limits()
    second_limits = first_limits.clone("Independent second-account limits")
    for account, limits in zip(
        accounts,
        (first_limits, second_limits),
        strict=True,
    ):
        account.collect = True
        account.apply = True
        account.limits = limits
        account.save(
            update_fields=("collect", "apply", "limits", "updated")
        )


def _build_sync_state(
    sanex_root: Path,
    config_store: ConfigStore,
    accounts: tuple[DiscoveredAccount, ...],
    hostname: str,
    version: str,
    log_content: bytes,
    command_executor: CommandExecutor | _KeepCommandsPending | None = None,
) -> tuple[ServiceSyncState, EventStore, CommandStore]:
    event_store = EventStore(sanex_root / "accounts")
    command_store = CommandStore(sanex_root / "commands.json")
    log_path = sanex_root / "sanex.log"
    log_path.write_bytes(log_content)

    def get_config():
        config = config_store.load()
        assert config is not None
        return config

    async def apply_config(data: str | bytes | bytearray) -> bool:
        config_store.save(data)
        return True

    state = ServiceSyncState(
        config=get_config,
        apply_config=apply_config,
        state_lock=asyncio.Lock(),
        event_store=event_store,
        command_store=command_store,
        command_executor=command_executor or CommandExecutor(command_store, {}),
        update_recovery=SimpleNamespace(reconcile=lambda: None),
        accounts=_Accounts(accounts),
        technical_log=TechnicalLog(log_path),
        hostname=lambda: hostname,
        package_version=lambda: version,
    )
    return state, event_store, command_store


def _build_session_packet(
    directory: Path,
    uid: int,
    session_ident: str,
    timestamp: int,
) -> LocalEventPacket:
    store = EventStore(directory)
    store.recover(uid, 0)
    _append_session_events(store, uid, session_ident, timestamp, ended=True)
    packet = store.seal(uid)
    assert packet is not None
    return packet


def _append_session_events(
    store: EventStore,
    uid: int,
    session_ident: str,
    timestamp: int,
    *,
    ended: bool,
) -> None:
    store.append(
        uid,
        SessionStartEvent(
            seq=1,
            type=EventType.SESSION_START,
            timestamp=timestamp,
            run_ident=1,
            sess_ident=session_ident,
            meta=SessionStartMeta(existing=False),
        ),
    )
    if not ended:
        return
    store.append(
        uid,
        SessionEndEvent(
            seq=2,
            type=EventType.SESSION_END,
            timestamp=timestamp + 60,
            run_ident=1,
            duration=60,
            meta=EndMeta(actor=Actor.USER),
        ),
    )


def _assert_parallel_results(hostnames: tuple[str, ...]) -> None:
    computers = Computer.objects.filter(hostname__in=hostnames).order_by("hostname")
    assert computers.count() == 2
    assert all(computer.version == "0.2.0" for computer in computers)
    assert all(computer.current_config_id is not None for computer in computers)
    assert EventPacket.objects.filter(account__computer__in=computers).count() == 2
    events = ActivityEvent.objects.filter(
        account__computer__in=computers
    ).order_by("account__computer__hostname", "seq")
    assert [(event.seq, event.duration) for event in events] == [
        (1, None),
        (2, 60),
        (1, None),
        (2, 60),
    ]


def _assert_multi_account_results(hostname: str) -> None:
    computer = Computer.objects.get(hostname=hostname)
    assert computer.current_config_id == computer.applied_config_id
    assert list(
        computer.accounts.order_by("uid").values_list("uid", "collect", "apply")
    ) == [(3001, True, True), (3002, True, True)]
    assert list(
        EventPacket.objects.filter(account__computer=computer)
        .order_by("account__uid")
        .values_list("account__uid", "event_count")
    ) == [(3001, 2), (3002, 1)]
    assert list(
        ActivityEvent.objects.filter(account__computer=computer)
        .order_by("account__uid", "seq")
        .values_list("account__uid", "seq", "type")
    ) == [
        (3001, 1, "session_start"),
        (3001, 2, "session_end"),
        (3002, 1, "session_start"),
    ]


def _assert_maximum_packet_results(hostname: str) -> None:
    computer = Computer.objects.get(hostname=hostname)
    account = computer.accounts.get(uid=4001)
    packet = EventPacket.objects.get(account=account)
    assert packet.first_seq == 1
    assert packet.last_seq == MAX_EVENTS_PER_PACKET
    assert packet.event_count == MAX_EVENTS_PER_PACKET
    events = ActivityEvent.objects.filter(account=account)
    assert events.count() == MAX_EVENTS_PER_PACKET
    assert events.order_by("seq").first().seq == 1
    assert events.order_by("-seq").first().seq == MAX_EVENTS_PER_PACKET


@pytest.fixture
def running_sanea(settings, integration_state_dir: Path) -> Iterator[_RunningSanea]:
    http_port, https_port, discovery_port = _reserve_ports(3)
    settings.PKI_DIR = integration_state_dir / "sanea-pki"
    servers = create_servers(
        get_wsgi_application(),
        "127.0.0.1",
        http_port,
        https_port,
        discovery_port,
        settings.PKI_DIR,
        lambda: True,
    )
    runner = ServerRunner(servers)
    errors = []

    def run() -> None:
        try:
            runner.run()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run, name="integration-sanea")
    thread.start()
    _wait_for_listener(https_port)
    try:
        yield _RunningSanea(discovery_port, https_port)
    finally:
        runner.request_stop()
        thread.join(5)
        assert not thread.is_alive()
        assert not errors


def test_registration_and_authenticated_sync_use_real_network_transports(
    running_sanea: _RunningSanea,
    integration_state_dir: Path,
    monkeypatch,
) -> None:
    accounts = (
        DiscoveredAccount(uid=1001, login="child", name="Alex"),
        DiscoveredAccount(uid=1002, login="parent", name="Parent"),
    )
    sanex_root = integration_state_dir / "sanex"
    pki_root = sanex_root / "pki"
    _allow_sandbox_pki(monkeypatch)

    async def exchange() -> None:
        registered = await _register_sanex(
            running_sanea,
            sanex_root,
            accounts,
            "integration-child",
        )
        config_store = registered.config_store
        initial_config = config_store.load()
        assert initial_config is not None
        computer_id, next_config_ident, command_id = await sync_to_async(
            _prepare_server_changes
        )()
        discovery = SaneaDiscovery(
            port=running_sanea.discovery_port,
            targets=("127.0.0.1",),
        )
        state, event_store, command_store = _build_sync_state(
            sanex_root,
            config_store,
            accounts,
            "integration-child-renamed",
            "0.1.1",
            b"integration exchange completed\n",
        )
        get_config = state.config
        event_store.recover(1001, 0)
        event_store.append(
            1001,
            SessionStartEvent(
                seq=1,
                type=EventType.SESSION_START,
                timestamp=1_791_040_000,
                run_ident=1,
                sess_ident="integration-session",
                meta=SessionStartMeta(existing=False),
            ),
        )
        event_store.append(
            1001,
            SessionEndEvent(
                seq=2,
                type=EventType.SESSION_END,
                timestamp=1_791_040_120,
                run_ident=1,
                duration=120,
                meta=EndMeta(actor=Actor.USER),
            ),
        )
        packet = event_store.seal(1001)
        assert packet is not None
        async with SyncHttpClient(registered.pki_paths.client_context()) as client:
            unavailable_exchange = SyncExchange(
                state=state,
                client=client,
                discovery=SaneaDiscovery(
                    port=_reserve_udp_port(),
                    timeout=0.05,
                    targets=("127.0.0.1",),
                ),
            )
            assert await unavailable_exchange.run() is False
            assert event_store.pending(1001) == (packet,)
            assert config_store.load() == initial_config
            assert command_store.pending == ()
            assert command_store.results == ()
            await sync_to_async(_assert_server_has_no_events)()

            lossy_exchange = SyncExchange(
                state=state,
                client=_LoseFirstEventResponse(client),
                discovery=discovery,
            )
            with pytest.raises(
                httpx.ReadError,
                match="simulated lost event-upload response",
            ):
                await lossy_exchange.run()
            assert event_store.pending(1001) == (packet,)
            assert [command.ident for command in command_store.pending] == [command_id]
            assert command_store.results == ()
            await sync_to_async(_assert_server_has_one_event_packet)()

            sync_exchange = SyncExchange(
                state=state,
                client=client,
                discovery=discovery,
            )
            assert await sync_exchange.run() is True

            config = get_config()
            assert config.ident == next_config_ident
            assert config.ignored_prcs == DEFAULT_IGNORED_PRCS
            child_config = next(
                account for account in config.accounts if account.uid == 1001
            )
            assert child_config.collect is True
            assert child_config.apply is True
            limits = child_config.limits
            assert limits is not None
            active = ScheduleResolver.for_config(config).resolve(
                limits,
                int(datetime(2026, 10, 5, 9, tzinfo=UTC).timestamp()),
            )
            assert active is not None
            assert active.range.ident == 11
            assert active.session_rule.ident == 17
            assert active.session_rule.max_sessions == 2
            assert active.session_rule.max_duration == 3_600
            assert active.session_rule.break_duration == 900
            app_rule = active.session_rule.app_rules[0]
            assert app_rule.ident == 23
            assert app_rule.max_launches == 1
            assert app_rule.max_time == 1_200
            matcher = AppRuleMatcher(app_rule)
            assert matcher.matches(
                "firefox",
                "/usr/bin/firefox",
                "Math lesson — exercises",
            )
            assert not matcher.matches(
                "firefox",
                "/usr/bin/firefox",
                "Entertainment",
            )
            occurrence = RangeOccurrence(
                range_ident=active.range.ident,
                date=active.occurrence_date,
                sessions=[],
                apps=[],
            )
            session_quota = SessionQuota(occurrence, active.session_rule)
            first_session = session_quota.start("session-1", 1)
            second_session = session_quota.start("session-2", 2)
            third_session = session_quota.start("session-3", 3)
            assert first_session.allowed
            assert second_session.allowed
            assert third_session.limit is SessionLimit.MAX_SESSIONS
            assert first_session.usage is not None
            assert (
                session_quota.spend(first_session.usage, 3_600)
                is SessionLimit.MAX_DURATION
            )

            app_quota = AppQuota(occurrence, app_rule)
            assert app_quota.reconcile((41,)).allowed
            launch_check = app_quota.reconcile((41, 42))
            assert launch_check.launch_blocked == (42,)
            time_check = app_quota.spend(1_200)
            assert set(time_check.time_blocked) == {41, 42}
            assert [result.ident for result in command_store.results] == [command_id]

            assert await sync_exchange.run() is True

            updated_config_ident, new_app_ident = await sync_to_async(
                _update_and_propagate_template
            )()
            assert await sync_exchange.run() is True

            updated_config = get_config()
            assert updated_config.ident == updated_config_ident
            updated_account = next(
                account for account in updated_config.accounts if account.uid == 1001
            )
            updated_limits = updated_account.limits
            assert updated_limits is not None
            updated_active = ScheduleResolver.for_config(updated_config).resolve(
                updated_limits,
                int(datetime(2026, 10, 5, 9, tzinfo=UTC).timestamp()),
            )
            assert updated_active is not None
            assert updated_active.range.ident == active.range.ident
            assert updated_active.session_rule.ident == active.session_rule.ident

            updated_session_quota = SessionQuota(
                occurrence,
                updated_active.session_rule,
            )
            retained_session = updated_session_quota.start("session-3", 4)
            assert retained_session.limit is SessionLimit.MAX_SESSIONS

            updated_app_rules = {
                rule.ident: rule for rule in updated_active.session_rule.app_rules
            }
            retained_app_rule = updated_app_rules[app_rule.ident]
            retained_app_check = AppQuota(
                occurrence,
                retained_app_rule,
            ).reconcile((41, 42))
            assert retained_app_check.launch_blocked == ()
            assert set(retained_app_check.time_blocked) == {41, 42}

            new_app_quota = AppQuota(
                occurrence,
                updated_app_rules[new_app_ident],
            )
            assert new_app_quota.usage is None
            new_app_check = new_app_quota.reconcile((51,))
            assert new_app_check.allowed
            assert new_app_check.usage is not None
            assert new_app_check.usage.spent == 0

            assert await sync_exchange.run() is True

            replacement_config_ident, replacement_ident = await sync_to_async(
                _replace_and_propagate_app_rule
            )()
            assert replacement_ident > new_app_ident
            assert replacement_ident != app_rule.ident
            assert await sync_exchange.run() is True

            replacement_config = get_config()
            assert replacement_config.ident == replacement_config_ident
            replacement_account = next(
                account
                for account in replacement_config.accounts
                if account.uid == 1001
            )
            replacement_limits = replacement_account.limits
            assert replacement_limits is not None
            replacement_active = ScheduleResolver.for_config(
                replacement_config
            ).resolve(
                replacement_limits,
                int(datetime(2026, 10, 5, 9, tzinfo=UTC).timestamp()),
            )
            assert replacement_active is not None
            replacement_rules = {
                rule.ident: rule for rule in replacement_active.session_rule.app_rules
            }
            assert app_rule.ident not in replacement_rules
            assert replacement_ident in replacement_rules
            replacement_quota = AppQuota(
                occurrence,
                replacement_rules[replacement_ident],
            )
            assert replacement_quota.usage is None

            assert await sync_exchange.run() is True

            changed_rule_config_ident, changed_session_ident = await sync_to_async(
                _change_range_session_rule
            )()
            assert changed_session_ident != active.session_rule.ident
            assert await sync_exchange.run() is True

            changed_rule_config = get_config()
            assert changed_rule_config.ident == changed_rule_config_ident
            changed_rule_account = next(
                account
                for account in changed_rule_config.accounts
                if account.uid == 1001
            )
            changed_rule_limits = changed_rule_account.limits
            assert changed_rule_limits is not None
            changed_rule_active = ScheduleResolver.for_config(
                changed_rule_config
            ).resolve(
                changed_rule_limits,
                int(datetime(2026, 10, 5, 9, tzinfo=UTC).timestamp()),
            )
            assert changed_rule_active is not None
            assert changed_rule_active.range.ident == active.range.ident
            assert changed_rule_active.session_rule.ident == changed_session_ident
            changed_rule_quota = SessionQuota(
                occurrence,
                changed_rule_active.session_rule,
            )
            changed_rule_check = changed_rule_quota.start("session-3", 5)
            assert changed_rule_check.limit is SessionLimit.MAX_SESSIONS

            assert await sync_exchange.run() is True

            changed_range_config_ident, changed_range_ident = await sync_to_async(
                _replace_and_propagate_range
            )(changed_session_ident)
            assert changed_range_ident != active.range.ident
            assert await sync_exchange.run() is True

            changed_range_config = get_config()
            assert changed_range_config.ident == changed_range_config_ident
            changed_range_account = next(
                account
                for account in changed_range_config.accounts
                if account.uid == 1001
            )
            changed_range_limits = changed_range_account.limits
            assert changed_range_limits is not None
            changed_range_active = ScheduleResolver.for_config(
                changed_range_config
            ).resolve(
                changed_range_limits,
                int(datetime(2026, 10, 5, 9, tzinfo=UTC).timestamp()),
            )
            assert changed_range_active is not None
            assert changed_range_active.range.ident == changed_range_ident
            assert changed_range_active.session_rule.ident == changed_session_ident
            changed_range_occurrence = RangeOccurrence(
                range_ident=changed_range_ident,
                date=changed_range_active.occurrence_date,
                sessions=[],
                apps=[],
            )
            changed_range_check = SessionQuota(
                changed_range_occurrence,
                changed_range_active.session_rule,
            ).start("session-3", 6)
            assert changed_range_check.allowed

            assert await sync_exchange.run() is True
            next_config_ident = changed_range_config_ident

        assert not packet.path.exists()
        assert command_store.pending == ()
        assert command_store.results == ()
        await sync_to_async(_assert_server_results)(
            computer_id,
            next_config_ident,
            command_id,
        )

    asyncio.run(exchange())

    assert (pki_root / "ca.crt").is_file()
    assert (pki_root / "client.crt").is_file()
    assert (pki_root / "client.key").is_file()


def test_cancelled_global_update_disappears_from_sanex_after_sync(
    running_sanea: _RunningSanea,
    integration_state_dir: Path,
    monkeypatch,
) -> None:
    accounts = (DiscoveredAccount(uid=1001, login="child", name="Alex"),)
    sanex_root = integration_state_dir / "cancelled-update-sanex"
    _allow_sandbox_pki(monkeypatch)

    async def exchange() -> None:
        registered = await _register_sanex(
            running_sanea,
            sanex_root,
            accounts,
            "update-child",
        )
        update_id, created = await sync_to_async(_activate_global_update)()
        assert created == 1
        state, _, command_store = _build_sync_state(
            sanex_root,
            registered.config_store,
            accounts,
            "update-child",
            "0.1.0",
            b"",
            _KeepCommandsPending(),
        )
        async with SyncHttpClient(registered.pki_paths.client_context()) as client:
            exchange = SyncExchange(
                state=state,
                client=client,
                discovery=SaneaDiscovery(
                    port=running_sanea.discovery_port,
                    targets=("127.0.0.1",),
                ),
            )
            assert await exchange.run() is True
            pending = command_store.pending
            assert len(pending) == 1
            assert pending[0].type == "update"
            assert pending[0].payload == {
                "version": "9.8.7",
                "index_url": "https://packages.example.test/simple",
            }

            assert await sync_to_async(_cancel_global_update)(update_id) == 1
            assert await exchange.run() is True

        assert command_store.pending == ()
        assert command_store.results == ()
        await sync_to_async(_assert_global_update_cancelled)(update_id)

    asyncio.run(exchange())


def test_two_clients_synchronize_and_store_events_concurrently(
    running_sanea: _RunningSanea,
    integration_state_dir: Path,
    monkeypatch,
) -> None:
    _allow_sandbox_pki(monkeypatch)
    first_hostname = "parallel-child-a"
    second_hostname = "parallel-child-b"
    hostnames = (first_hostname, second_hostname)
    first_accounts = (
        DiscoveredAccount(uid=2001, login="child-a", name="Child A"),
    )
    second_accounts = (
        DiscoveredAccount(uid=2002, login="child-b", name="Child B"),
    )

    async def exchange() -> None:
        first = await _register_sanex(
            running_sanea,
            integration_state_dir / "parallel-sanex-a",
            first_accounts,
            first_hostname,
        )
        second = await _register_sanex(
            running_sanea,
            integration_state_dir / "parallel-sanex-b",
            second_accounts,
            second_hostname,
        )
        await sync_to_async(_enable_collection)(hostnames)
        first_packet = _build_session_packet(
            integration_state_dir / "parallel-events-a",
            2001,
            "parallel-session-a",
            1_791_040_000,
        )
        second_packet = _build_session_packet(
            integration_state_dir / "parallel-events-b",
            2002,
            "parallel-session-b",
            1_791_040_100,
        )
        first_request = SyncRequest(
            hostname=first_hostname,
            version="0.2.0",
            config_ident=None,
            accounts=first_accounts,
            command_results=(),
        )
        second_request = SyncRequest(
            hostname=second_hostname,
            version="0.2.0",
            config_ident=None,
            accounts=second_accounts,
            command_results=(),
        )

        async with (
            SyncHttpClient(first.pki_paths.client_context()) as first_client,
            SyncHttpClient(second.pki_paths.client_context()) as second_client,
        ):
            (
                first_response,
                second_response,
                first_upload,
                second_upload,
            ) = await asyncio.gather(
                first_client.sync(running_sanea.base_url, first_request),
                second_client.sync(running_sanea.base_url, second_request),
                first_client.upload_events(running_sanea.base_url, first_packet),
                second_client.upload_events(running_sanea.base_url, second_packet),
            )

        assert first_response.config is not None
        assert second_response.config is not None
        assert first_response.config != second_response.config
        assert first_upload is EventUploadResult.SAVED
        assert second_upload is EventUploadResult.SAVED
        await sync_to_async(_assert_parallel_results)(hostnames)

    asyncio.run(exchange())


def test_system_service_uploads_all_accounts_during_one_active_session(
    running_sanea: _RunningSanea,
    integration_state_dir: Path,
    monkeypatch,
) -> None:
    _allow_sandbox_pki(monkeypatch)
    hostname = "multi-account-child"
    accounts = (
        DiscoveredAccount(uid=3001, login="child-a", name="Child A"),
        DiscoveredAccount(uid=3002, login="child-b", name="Child B"),
    )
    sanex_root = integration_state_dir / "multi-account-sanex"

    async def exchange() -> None:
        registered = await _register_sanex(
            running_sanea,
            sanex_root,
            accounts,
            hostname,
        )
        await sync_to_async(_enable_multi_account_limits)(hostname)
        state, event_store, command_store = _build_sync_state(
            sanex_root,
            registered.config_store,
            accounts,
            hostname,
            "0.3.0",
            b"multi-account synchronization\n",
        )
        event_store.recover(3001, 0)
        event_store.recover(3002, 0)
        _append_session_events(
            event_store,
            3001,
            "previous-session-a",
            1_791_040_000,
            ended=True,
        )
        _append_session_events(
            event_store,
            3002,
            "currently-active-session-b",
            1_791_041_000,
            ended=False,
        )
        discovery = SaneaDiscovery(
            port=running_sanea.discovery_port,
            targets=("127.0.0.1",),
        )

        async with SyncHttpClient(registered.pki_paths.client_context()) as client:
            sync_exchange = SyncExchange(state, client, discovery)
            assert await sync_exchange.run() is True
            assert await sync_exchange.run() is True

        config = registered.config_store.load()
        assert config is not None
        assert [(account.uid, account.collect) for account in config.accounts] == [
            (3001, True),
            (3002, True),
        ]
        assert all(account.apply for account in config.accounts)
        assert all(account.limits is not None for account in config.accounts)
        state_store = RuntimeStateStore(sanex_root / "accounts")
        repository = RuntimeRepository(
            state_store,
            event_store,
            "multi-account-system-boot",
        )
        runtimes = {
            account.uid: AccountRuntime(
                account.uid,
                repository.load(account, config.ident),
            )
            for account in config.accounts
        }
        timestamp = int(datetime(2026, 10, 5, 9, tzinfo=UTC).timestamp())
        resolver = ScheduleResolver.for_config(config)
        spent_by_uid = {3001: 600, 3002: 100}
        app_spent_by_uid = {3001: 500, 3002: 50}
        for account in config.accounts:
            limits = account.limits
            assert limits is not None
            active = resolver.resolve(limits, timestamp)
            assert active is not None
            runtime = runtimes[account.uid]
            occurrence = runtime.ensure_occurrence(active)
            session_quota = SessionQuota(occurrence, active.session_rule)
            check = session_quota.start(
                f"session-{account.uid}",
                timestamp,
            )
            assert check.usage is not None
            assert session_quota.spend(
                check.usage,
                spent_by_uid[account.uid],
            ) is None
            app_quota = AppQuota(occurrence, active.session_rule.app_rules[0])
            assert app_quota.reconcile((account.uid,)).allowed
            assert app_quota.spend(app_spent_by_uid[account.uid]).allowed
            state_store.save(account.uid, runtime.state)

        restarted_repository = RuntimeRepository(
            RuntimeStateStore(sanex_root / "accounts"),
            EventStore(sanex_root / "accounts"),
            "multi-account-system-boot",
        )
        recovered_spent = {}
        recovered_app_spent = {}
        for account in config.accounts:
            recovered = restarted_repository.load(account, config.ident)
            assert len(recovered.occurrences) == 1
            recovered_spent[account.uid] = recovered.occurrences[0].sessions[0].spent
            recovered_app_spent[account.uid] = recovered.occurrences[0].apps[0].spent
        assert recovered_spent == spent_by_uid
        assert recovered_app_spent == app_spent_by_uid
        assert event_store.pending(3001) == ()
        assert event_store.pending(3002) == ()
        assert command_store.pending == ()
        assert command_store.results == ()
        await sync_to_async(_assert_multi_account_results)(hostname)

    asyncio.run(exchange())


def test_maximum_event_count_packet_crosses_real_network_boundary(
    running_sanea: _RunningSanea,
    integration_state_dir: Path,
    monkeypatch,
) -> None:
    _allow_sandbox_pki(monkeypatch)
    hostname = "maximum-event-packet-child"
    accounts = (
        DiscoveredAccount(uid=4001, login="child", name="Child"),
    )
    sanex_root = integration_state_dir / "maximum-event-packet-sanex"

    async def exchange() -> None:
        registered = await _register_sanex(
            running_sanea,
            sanex_root,
            accounts,
            hostname,
        )
        await sync_to_async(_enable_collection)((hostname,))
        state, event_store, _ = _build_sync_state(
            sanex_root,
            registered.config_store,
            accounts,
            hostname,
            "0.4.0",
            b"maximum event packet synchronization\n",
        )
        event_store.recover(4001, 0)
        packet = None
        base_timestamp = 1_791_050_000
        for index in range(MAX_EVENTS_PER_PACKET // 2):
            start_seq = index * 2 + 1
            event_store.append(
                4001,
                SessionStartEvent(
                    seq=start_seq,
                    type=EventType.SESSION_START,
                    timestamp=base_timestamp + index * 2,
                    run_ident=start_seq,
                    sess_ident=f"boundary-session-{index}",
                    meta=SessionStartMeta(existing=False),
                ),
            )
            packet = event_store.append(
                4001,
                SessionEndEvent(
                    seq=start_seq + 1,
                    type=EventType.SESSION_END,
                    timestamp=base_timestamp + index * 2 + 1,
                    run_ident=start_seq,
                    duration=1,
                    meta=EndMeta(actor=Actor.USER),
                ),
            )
        assert packet is not None
        assert packet.first_seq == 1
        assert packet.last_seq == MAX_EVENTS_PER_PACKET
        assert event_store.pending(4001) == (packet,)

        async with SyncHttpClient(registered.pki_paths.client_context()) as client:
            sync_exchange = SyncExchange(
                state,
                client,
                SaneaDiscovery(
                    port=running_sanea.discovery_port,
                    targets=("127.0.0.1",),
                ),
            )
            assert await sync_exchange.run() is True

        assert not packet.path.exists()
        assert event_store.pending(4001) == ()
        await sync_to_async(_assert_maximum_packet_results)(hostname)

    asyncio.run(exchange())
