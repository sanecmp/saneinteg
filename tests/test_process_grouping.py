"""Generic process-tree grouping from procfs through accounting events."""

from pathlib import Path

from sanelib.protocol import EventType, ProcessStartEvent

from sanex.accounting.runtime import AccountRuntime
from sanex.model.state import RuntimeState
from sanex.model.window import AtspiWindow, AtspiWindowSnapshot, WindowRole
from sanex.platform.process import ProcessReader, WindowProcessResolver


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

    runtime = AccountRuntime(
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
