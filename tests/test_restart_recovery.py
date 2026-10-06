"""Cross-component persistence across a sanex service-process restart."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from sanelib.protocol import (
    Account,
    Actor,
    AppRule,
    Command,
    CommandStatus,
    Config,
    EndMeta,
    EventType,
    Limits,
    MatchType,
    Range,
    SessionEndEvent,
    SessionRule,
    SessionStartEvent,
    SessionStartMeta,
)

from sanex.accounting.app import AppQuota
from sanex.accounting.policy import SessionDenial, SessionPolicyEvaluator
from sanex.accounting.runtime import AccountRuntime
from sanex.accounting.schedule import ScheduleResolver
from sanex.accounting.session import SessionLimit, SessionQuota
from sanex.service.runtime import RuntimeRepository
from sanex.model.state import SessionRun
from sanex.storage.config import ConfigStore
from sanex.storage.events import EventStore
from sanex.storage.state import RuntimeStateStore
from sanex.sync.commands import CommandExecutor, CommandStore


UID = 1001
RANGE_IDENT = 11
SESSION_RULE_IDENT = 17
APP_RULE_IDENT = 23
MONDAY_MORNING = int(datetime(2026, 10, 5, 9, tzinfo=UTC).timestamp())


def test_recovers_complete_offline_state_and_continues_enforcement(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    accounts_root = tmp_path / "accounts"
    commands_path = tmp_path / "commands.json"
    config_store = ConfigStore(config_path)
    config = build_config()
    config_store.save(config.model_dump_json())
    state_store = RuntimeStateStore(accounts_root)
    event_store = EventStore(accounts_root)
    repository = RuntimeRepository(state_store, event_store, "same-system-boot")
    account = config.accounts[0]
    runtime = AccountRuntime(UID, repository.load(account, config.ident))
    active = ScheduleResolver.for_config(config).resolve(
        account.limits,
        MONDAY_MORNING,
    )
    assert active is not None
    occurrence = runtime.ensure_occurrence(active)
    session_quota = SessionQuota(occurrence, active.session_rule)
    session_check = session_quota.start("session-before-restart", MONDAY_MORNING)
    assert session_check.usage is not None
    assert session_quota.spend(session_check.usage, 3_500) is None
    app_rule = active.session_rule.app_rules[0]
    app_quota = AppQuota(occurrence, app_rule)
    assert app_quota.reconcile((41,)).allowed
    assert app_quota.spend(1_100).allowed
    runtime.state.break_till = MONDAY_MORNING + 900
    runtime.state.sess_runs.append(
        SessionRun(
            run_ident=0,
            sess_ident="active-after-restart",
            started=MONDAY_MORNING,
            duration=0,
            terminate_requested=None,
            break_duration=0,
        )
    )

    event_store.append(UID, build_session_start(runtime, "session-before-restart"))
    event_store.append(UID, build_session_end(runtime, 60))
    sealed_packet = event_store.seal(UID)
    assert sealed_packet is not None
    event_store.append(UID, build_session_start(runtime, "open-before-restart"))
    state_store.save(UID, runtime.state)
    command_store = CommandStore(commands_path)
    command_store.accept(
        (),
        (
            Command(
                ident=29,
                type="diagnostic",
                payload={"scope": "restart-integration"},
            ),
        ),
    )

    restarted_config = ConfigStore(config_path).load()
    assert restarted_config is not None
    restarted_account = restarted_config.accounts[0]
    restarted_events = EventStore(accounts_root)
    restarted_state_store = RuntimeStateStore(accounts_root)
    restarted_repository = RuntimeRepository(
        restarted_state_store,
        restarted_events,
        "same-system-boot",
    )
    restarted_runtime = AccountRuntime(
        UID,
        restarted_repository.load(restarted_account, restarted_config.ident),
    )
    restarted_commands = CommandStore(commands_path)

    assert restarted_runtime.state.break_till == MONDAY_MORNING + 900
    assert restarted_runtime.state.event_seq == 3
    assert restarted_events.pending(UID) == (sealed_packet,)
    assert restarted_commands.pending[0].ident == 29
    restarted_active = ScheduleResolver.for_config(restarted_config).resolve(
        restarted_account.limits,
        MONDAY_MORNING,
    )
    assert restarted_active is not None
    restarted_occurrence = restarted_runtime.find_occurrence(
        RANGE_IDENT,
        restarted_active.occurrence_date,
    )
    assert restarted_occurrence is not None
    break_decision = SessionPolicyEvaluator(
        restarted_runtime,
        ScheduleResolver.for_config(restarted_config),
    ).evaluate(
        restarted_account,
        "active-after-restart",
        MONDAY_MORNING,
        elapsed=0,
    )
    assert break_decision.reason is SessionDenial.MANDATORY_BREAK
    assert break_decision.terminate

    restarted_session_quota = SessionQuota(
        restarted_occurrence,
        restarted_active.session_rule,
    )
    recovered_session = restarted_session_quota.usage("session-before-restart")
    assert recovered_session is not None
    assert recovered_session.spent == 3_500
    assert (
        restarted_session_quota.spend(recovered_session, 100)
        is SessionLimit.MAX_DURATION
    )
    assert (
        restarted_session_quota.start("session-after-restart", MONDAY_MORNING + 1)
        .limit
        is SessionLimit.MAX_SESSIONS
    )

    restarted_app_quota = AppQuota(
        restarted_occurrence,
        restarted_active.session_rule.app_rules[0],
    )
    recovered_app = restarted_app_quota.usage
    assert recovered_app is not None
    assert recovered_app.spent == 1_100
    assert restarted_app_quota.spend(100).time_blocked == (41,)
    app_check = restarted_app_quota.reconcile((41, 42))
    assert app_check.launch_blocked == (42,)
    assert app_check.time_blocked == (41, 42)

    next_seq = restarted_runtime.next_event_seq()
    assert next_seq == 4
    restarted_events.append(
        UID,
        SessionEndEvent(
            seq=next_seq,
            type=EventType.SESSION_END,
            timestamp=MONDAY_MORNING + 120,
            run_ident=2,
            duration=120,
            meta=EndMeta(actor=Actor.USER),
        ),
    )
    restarted_state_store.save(UID, restarted_runtime.state)
    asyncio.run(CommandExecutor(restarted_commands, {}).run())

    final_state = RuntimeStateStore(accounts_root).load(UID)
    final_commands = CommandStore(commands_path)
    assert final_state is not None
    assert final_state.event_seq == 4
    assert not final_commands.pending
    assert final_commands.results[0].status is CommandStatus.FAILED
    assert final_commands.results[0].error == (
        "unsupported command type: diagnostic"
    )
    assert EventStore(accounts_root).pending(UID) == (sealed_packet,)


def build_config() -> Config:
    app_rule = AppRule(
        ident=APP_RULE_IDENT,
        name="Browser",
        apply=True,
        max_launches=1,
        max_time=1_200,
        prc_name="firefox",
        prc_name_match=MatchType.EXACT,
        exe=None,
        exe_match=None,
        wnd_title=None,
        wnd_title_match=None,
    )
    session_rule = SessionRule(
        ident=SESSION_RULE_IDENT,
        apply=True,
        max_sessions=1,
        max_duration=3_600,
        break_duration=900,
        app_rules=(app_rule,),
    )
    limits = Limits(
        ranges=(
            Range(
                ident=RANGE_IDENT,
                apply=True,
                weekday=0,
                since=480,
                till=720,
                session_rule_ident=SESSION_RULE_IDENT,
            ),
        ),
        session_rules=(session_rule,),
    )
    return Config(
        ident=7,
        timezone="UTC",
        sync_interval=60,
        discovery_interval=10,
        walk_interval=1,
        save_interval=5,
        min_prc_duration=5,
        ignored_prcs=(),
        accounts=(
            Account(
                uid=UID,
                collect=True,
                apply=True,
                limits=limits,
            ),
        ),
    )


def build_session_start(
    runtime: AccountRuntime,
    session_ident: str,
) -> SessionStartEvent:
    return SessionStartEvent(
        seq=runtime.next_event_seq(),
        type=EventType.SESSION_START,
        timestamp=MONDAY_MORNING,
        run_ident=runtime.state.event_seq,
        sess_ident=session_ident,
        meta=SessionStartMeta(existing=False),
    )


def build_session_end(
    runtime: AccountRuntime,
    duration: int,
) -> SessionEndEvent:
    return SessionEndEvent(
        seq=runtime.next_event_seq(),
        type=EventType.SESSION_END,
        timestamp=MONDAY_MORNING + duration,
        run_ident=1,
        duration=duration,
        meta=EndMeta(actor=Actor.USER),
    )
