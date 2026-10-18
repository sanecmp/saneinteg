"""Generic process-tree grouping from procfs through accounting events."""

from pathlib import Path
from datetime import UTC, date, datetime
import hashlib

from sanelib.protocol import EventType, ProcessStartEvent, encode_event, parse_event_packet

from sanea.core.models import Account, ActivityEvent, AppRule, Computer, Limits, SessionRule

from sanex.accounting.runtime import AccountRuntime
from sanex.accounting.app import AppRuleMatcher
from sanex.applications.desktop import DesktopCatalog
from sanex.model.state import RuntimeState
from sanex.model.window import AtspiWindow, AtspiWindowSnapshot, WindowRole
from sanex.platform.process import ProcessReader, WindowProcessResolver
from sanex.service.applications import create_application_resolver


UID = 1001
BROWSER_EXE = "/usr/lib/example-browser/browser"
BROWSER_CGROUP = (
    "/user.slice/user-1001.slice/user@1001.service/"
    "app.slice/app-example-browser.scope"
)


def test_groups_browser_style_workers_without_application_specific_names(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    write_process(proc_root, 100, 1, "/usr/bin/app-launcher", "launcher", 100)
    write_process(proc_root, 110, 100, BROWSER_EXE, "browser-main", 110)
    write_process(proc_root, 111, 110, BROWSER_EXE, "renderer", 111)
    write_process(proc_root, 112, 111, BROWSER_EXE, "tab-worker", 112)
    write_process(
        proc_root,
        113,
        110,
        "/usr/lib/example-browser/gpu-helper",
        "gpu-helper",
        113,
    )
    write_process(proc_root, 210, 100, BROWSER_EXE, "browser-main", 210)
    write_process(proc_root, 211, 210, BROWSER_EXE, "tab-worker", 211)
    resolver = WindowProcessResolver(ProcessReader(proc_root))
    initial_windows = (
        build_window(110, "/browser/main", "Example Browser"),
        build_window(112, "/browser/tab", "Documentation"),
        build_window(211, "/browser/private", "Private window"),
    )

    initial_groups = resolver.resolve(initial_windows, UID)

    assert [(group.process.pid, len(group.windows)) for group in initial_groups] == [
        (110, 2),
        (210, 1),
    ]
    assert initial_groups[0].process.prc_name == "browser-main"
    assert [owner.pid for owner in initial_groups[0].owners] == [110, 112]
    assert all(group.process.exe == BROWSER_EXE for group in initial_groups)

    runtime = build_runtime()
    first = runtime.reconcile_windows(
        sess_ident="graphical-session",
        groups=initial_groups,
        snapshot=AtspiWindowSnapshot(windows=initial_windows),
        timestamp=1_000,
        elapsed=0,
    )
    assert len(first.started) == 2

    write_process(proc_root, 114, 111, BROWSER_EXE, "replacement-worker", 114)
    replacement_windows = (
        build_window(110, "/browser/main", "Example Browser — renamed"),
        build_window(114, "/browser/replacement", "Documentation — next page"),
        build_window(211, "/browser/private", "Private window"),
    )
    replacement_groups = resolver.resolve(replacement_windows, UID)

    second = runtime.reconcile_windows(
        sess_ident="graphical-session",
        groups=replacement_groups,
        snapshot=AtspiWindowSnapshot(windows=replacement_windows),
        timestamp=1_005,
        elapsed=5,
    )
    events = runtime.create_process_events(
        ended=second.ended,
        min_duration=5,
        timestamp=1_005,
    )

    assert second.started == ()
    assert second.ended == ()
    assert len(runtime.state.prc_runs) == 2
    assert {run.group_ident for run in runtime.state.prc_runs} == {
        "110:110",
        "210:210",
    }
    assert [event.type for event in events] == [
        EventType.PRC_START,
        EventType.PRC_START,
    ]
    assert all(isinstance(event, ProcessStartEvent) for event in events)
    assert {event.prc_name for event in events} == {"browser-main"}
    assert {event.exe for event in events} == {BROWSER_EXE}


def test_identification_reaches_server_reports_and_rules_without_losing_raw_identity(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    write_process(proc_root, 110, 1, "/usr/bin/gjs-console", "gjs-console", 110)
    write_process(proc_root, 120, 1, "/usr/bin/gjs-console", "gjs-console", 120)
    write_process(proc_root, 190, 1, "/steam/steam", "steam", 190)
    write_process(proc_root, 200, 190, "/steam/steamwebhelper", "steamwebhelper", 200)
    write_process(proc_root, 300, 1, "/usr/bin/xdg-dbus-proxy", "xdg-dbus-proxy", 300)
    write_process(proc_root, 400, 190, "/games/native", "native", 400)
    (proc_root / "110" / "cmdline").write_bytes(b"gjs\0/apps/first.js\0")
    (proc_root / "120" / "cmdline").write_bytes(b"gjs\0/apps/second.js\0")
    (proc_root / "400" / "environ").write_bytes(b"SteamAppId=123\0")
    root = proc_root / "300" / "root"
    root.mkdir()
    (root / ".flatpak-info").write_text("[Application]\nname=org.example.PhotoCraft\n")
    resolver = WindowProcessResolver(
        ProcessReader(proc_root), create_application_resolver(proc_root, catalog=DesktopCatalog(directories=())),
    )
    windows = tuple(build_window(pid, f"/window/{pid}", "Synthetic window") for pid in (110, 120, 200, 300, 400))
    groups = resolver.resolve(windows, UID)
    runtime = build_runtime()
    started = int(datetime(2026, 10, 1, 12, tzinfo=UTC).timestamp())
    observed = runtime.reconcile_windows(
        "graphical-session", groups, AtspiWindowSnapshot(windows=windows), started, elapsed=0,
    )
    events = runtime.create_process_events(ended=(), min_duration=0, timestamp=started)
    ended = runtime.reconcile_windows("graphical-session", (), AtspiWindowSnapshot(), started + 60, elapsed=60)
    events += runtime.create_process_events(ended=ended.ended, min_duration=0, timestamp=started + 60)
    content = b"".join(encode_event(event) for event in events)
    computer = Computer.objects.create(hostname="synthetic-identities", status="allowed")
    account = Account.objects.create(computer=computer, uid=UID, collect=True)

    account.store_event_packet(parse_event_packet(content, hashlib.sha256(content).hexdigest()), len(content), None)
    report = ActivityEvent.objects.build_report(Account.objects.filter(pk=account.pk), date(2026, 10, 1), date(2026, 10, 2))

    assert {usage.app_id for usage in report.applications} == {
        "gjs:/apps/first.js", "gjs:/apps/second.js", "steam:client", "flatpak:org.example.PhotoCraft", "steam:game:123",
    }
    assert all(usage.duration == 60 and usage.launches == 1 for usage in report.applications)
    proxy = account.activity_events.get(app_id="flatpak:org.example.PhotoCraft")
    assert (proxy.prc_name, proxy.exe) == ("xdg-dbus-proxy", "/usr/bin/xdg-dbus-proxy")
    steam = next(window for window in observed.windows if window.application.ident == "steam:client")
    assert steam.state.pid == 200
    assert steam.process.pid == 190
    limits = Limits.objects.create(name="Synthetic identity limits")
    session_rule = SessionRule.objects.create(limits=limits, ident=1, name="Synthetic session")
    rule = AppRule.objects.create(session_rule=session_rule, ident=1, name="Steam", max_time=0, app_id="steam:client")
    matcher = AppRuleMatcher(rule.build_client_config())
    assert matcher.matches(steam.process.prc_name, steam.process.exe, steam.observed.title, steam.application.ident)
    assert not matcher.matches("native", "/games/native", "", "steam:game:123")


def build_runtime() -> AccountRuntime:
    """Create isolated runtime state for a synthetic process observation."""
    return AccountRuntime(
        UID,
        RuntimeState(
            config_ident=1,
            boot_ident="synthetic-process-tree",
            wnd_seq=0,
            event_seq=0,
            break_till=None,
            sess_runs=[],
            prc_runs=[],
            wnds=[],
            occurrences=[],
        ),
    )


def write_process(
    proc_root: Path,
    pid: int,
    parent_pid: int,
    exe: str,
    name: str,
    started: int,
) -> None:
    """Write a synthetic minimal procfs entry accepted by ProcessReader."""
    directory = proc_root / f"{pid}"
    directory.mkdir(parents=True)
    stat_fields = ["S", f"{parent_pid}", *("0" for _ in range(17)), f"{started}"]
    directory.joinpath("stat").write_text(
        f"{pid} ({name}) {' '.join(stat_fields)}\n",
        encoding="utf-8",
    )
    directory.joinpath("status").write_text(
        f"Uid:\t{UID}\t{UID}\t{UID}\t{UID}\n",
        encoding="utf-8",
    )
    directory.joinpath("comm").write_text(f"{name}\n", encoding="utf-8")
    directory.joinpath("exe").symlink_to(exe)
    directory.joinpath("cgroup").write_text(
        f"0::{BROWSER_CGROUP}\n",
        encoding="utf-8",
    )


def build_window(pid: int, path: str, title: str) -> AtspiWindow:
    return AtspiWindow(
        bus=":1.50",
        path=path,
        pid=pid,
        title=title,
        role=WindowRole.FRAME,
    )
