"""Crash cleanup kills only what the crashing automation started.

`handle_crash` used to `taskkill /im` every Excel, Chrome and ChromeDriver on the machine, so one
automation's crash killed any other automation running at the time (and the user's own Chrome).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from seller_automation_utils import _winproc, alert_utils, custom_functions, excel_utils

ME = os.getpid()


def _forbid_kill_app(monkeypatch) -> None:
    def forbidden(name: str) -> None:
        raise AssertionError(f"kill_app({name!r}) kills every {name} on the machine")

    monkeypatch.setattr(custom_functions, "kill_app", forbidden)


class Kills(list):
    """terminate() calls as (pid, label), plus the creation time each kill was bound to."""

    def __init__(self) -> None:
        super().__init__()
        self.created: dict[int, int] = {}

    def terminate(self, pid: int, created: int, label: str = "") -> bool:
        self.append((pid, label))
        self.created[pid] = created
        return True


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Kills:
    """Record terminate() calls; nothing real is ever killed."""
    kills = Kills()
    monkeypatch.setattr(_winproc, "terminate", kills.terminate)
    monkeypatch.setattr(excel_utils, "started_excel", lambda: [])
    _forbid_kill_app(monkeypatch)
    return kills


def _world(monkeypatch, table: list[tuple[int, int, str]], born: dict[int, int | None]) -> None:
    """A fake process table; this process is born at 1000 unless ``born`` says otherwise."""
    monkeypatch.setattr(_winproc, "process_table", lambda: table)
    times = {ME: 1000, **born}
    monkeypatch.setattr(_winproc, "creation_time", lambda pid: times.get(pid))


def test_own_browser_trees_leaves_first_and_own_excel(recorded, monkeypatch) -> None:
    _world(monkeypatch, [
        (101, ME, "chrome.exe"), (111, 101, "chrome.exe"), (112, 101, "chrome.exe"),
        (102, ME, "uc_driver.exe"), (121, 102, "conhost.exe"),
        (103, ME, "conhost.exe"), (104, ME, "python.exe"),
    ], {101: 2000, 111: 2100, 112: 2100, 102: 2000, 121: 2050, 103: 2000, 104: 2000})
    monkeypatch.setattr(excel_utils, "started_excel", lambda: [(201, 777)])

    alert_utils._kill_own_processes()

    assert recorded == [
        (112, "(chrome.exe)"), (111, "(chrome.exe)"), (101, "(chrome.exe)"),
        (121, "(conhost.exe)"), (102, "(uc_driver.exe)"),
        (201, "(EXCEL.EXE)"),
    ]
    # every kill is bound to the creation time that was checked
    assert recorded.created == {112: 2100, 111: 2100, 101: 2000, 121: 2050, 102: 2000, 201: 777}


def test_a_child_born_in_the_same_millisecond_as_its_parent_is_ours(recorded, monkeypatch) -> None:
    _world(monkeypatch, [(101, ME, "chrome.exe"), (111, 101, "chrome.exe")], {101: 2000, 111: 2000})

    alert_utils._kill_own_processes()

    assert recorded == [(111, "(chrome.exe)"), (101, "(chrome.exe)")]


def test_a_root_older_than_us_is_a_reused_parent_pid_not_ours(recorded, monkeypatch) -> None:
    """E.g. the user's Chrome after "Relaunch to update": its recorded parent pid is ours now."""
    _world(monkeypatch, [(101, ME, "chrome.exe"), (102, ME, "chrome.exe"), (103, ME, "chrome.exe")],
           {101: 500, 102: None, 103: 1000})  # older; gone; same millisecond as us

    alert_utils._kill_own_processes()

    assert recorded == []


def test_a_stale_branch_below_our_chrome_is_never_walked_into(recorded, monkeypatch) -> None:
    """taskkill /t would follow a recorded parent pid anywhere down the tree. We don't."""
    _world(monkeypatch, [
        (101, ME, "chrome.exe"),
        (111, 101, "chrome.exe"),          # our renderer
        (900, 111, "chrome.exe"),          # the user's browser, whose dead parent's pid 111 got reused
        (901, 900, "chrome.exe"),          # ...and its own renderer
    ], {101: 2000, 111: 2100, 900: 400, 901: 450})

    alert_utils._kill_own_processes()

    assert recorded == [(111, "(chrome.exe)"), (101, "(chrome.exe)")]


def test_unknown_own_age_kills_no_browser(recorded, monkeypatch) -> None:
    _world(monkeypatch, [(101, ME, "chrome.exe")], {ME: None, 101: 2000})

    alert_utils._kill_own_processes()

    assert recorded == []


def test_handle_crash_never_kills_machine_wide(monkeypatch) -> None:
    monkeypatch.setattr(alert_utils, "get_env", lambda *a, **k: "alerts@example.com")
    monkeypatch.setattr(alert_utils.fleet_state, "record_crash", lambda *a, **k: None)
    monkeypatch.setattr(alert_utils.fleet_state, "automation_name", lambda: "demo")
    monkeypatch.setattr(alert_utils.fleet_state, "mark_crash_emailed", lambda *a, **k: None)
    monkeypatch.setattr(alert_utils.outlook, "send_email", lambda **k: None)
    _forbid_kill_app(monkeypatch)
    scoped: list[bool] = []
    monkeypatch.setattr(alert_utils, "_kill_own_processes", lambda: scoped.append(True))

    alert_utils.handle_crash(None, "Traceback: boom", "Demo Report")

    assert scoped == [True]


def test_cleanup_failure_never_breaks_the_crash_handler(monkeypatch) -> None:
    monkeypatch.setattr(alert_utils, "get_env", lambda *a, **k: "alerts@example.com")
    monkeypatch.setattr(alert_utils.fleet_state, "record_crash", lambda *a, **k: None)
    monkeypatch.setattr(alert_utils.fleet_state, "automation_name", lambda: "demo")
    monkeypatch.setattr(alert_utils.fleet_state, "mark_crash_emailed", lambda *a, **k: None)
    monkeypatch.setattr(alert_utils.outlook, "send_email", lambda **k: None)
    _forbid_kill_app(monkeypatch)

    def explode() -> None:
        raise OSError("snapshot failed")

    monkeypatch.setattr(alert_utils, "_kill_own_processes", explode)

    alert_utils.handle_crash(None, "Traceback: boom", "Demo Report")  # must not raise


def test_no_tree_kill_or_image_kill_exists_any_more() -> None:
    """Nothing in the cleanup path may walk recorded parent pids unchecked or kill by name."""
    assert not hasattr(_winproc, "taskkill")
    assert not hasattr(_winproc, "children")


# --- the Excel registry ------------------------------------------------------------------------

def test_recycled_excel_pid_is_never_reported(monkeypatch) -> None:
    """A pid Windows reused for another process has a different creation time: not ours."""
    monkeypatch.setattr(excel_utils, "_started_excel", {})
    times = {301: 1000}
    monkeypatch.setattr(_winproc, "creation_time", lambda pid: times.get(pid))
    excel_utils._track_excel(301)

    times[301] = 2000  # the original Excel died and the pid now names something else

    assert excel_utils.started_excel_pids() == []
    assert excel_utils._started_excel == {}


def test_excel_that_teardown_failed_to_end_stays_tracked(monkeypatch) -> None:
    monkeypatch.setattr(excel_utils, "_started_excel", {})
    monkeypatch.setattr(_winproc, "creation_time", lambda pid: 1000)

    excel_utils._track_excel(302)
    excel_utils._untrack_if_gone(302)

    assert excel_utils.started_excel_pids() == [302]


def _fake_excel_app(monkeypatch, seen_during: list[list[int]]) -> None:
    """An xlwings App stand-in whose Excel 'dies' when the `with` block exits."""
    alive = {"yes": True}
    monkeypatch.setattr(_winproc, "creation_time", lambda pid: 1000 if alive["yes"] else None)

    class Sheet:
        def __init__(self) -> None:
            class Pictures:
                def add(self, *a, **k):
                    seen_during.append(excel_utils.started_excel_pids())

            self.pictures = Pictures()

        def range(self, cell):
            class R:
                left = top = 0

            return R()

    class Book:
        sheets = {0: Sheet()}

        def macro(self, name):
            return lambda: seen_during.append(excel_utils.started_excel_pids())

        def save(self):
            pass

        def close(self):
            pass

    class App:
        pid = 4242

        def __init__(self, **kw) -> None:
            self.display_alerts = True
            self.screen_updating = True

            class Books:
                def open(self, path):
                    return Book()

            self.books = Books()

        def __enter__(self):
            alive["yes"] = True
            return self

        def __exit__(self, *exc):
            alive["yes"] = False  # App.__exit__ quits and kills Excel

    monkeypatch.setattr(excel_utils.xw, "App", App)


@pytest.mark.parametrize("helper", ["refresh_workbook", "run_macro", "paste_image_to_sheet"])
def test_every_excel_helper_tracks_its_excel_and_forgets_it_once_gone(monkeypatch, helper) -> None:
    monkeypatch.setattr(excel_utils, "_started_excel", {})
    seen_during: list[list[int]] = []
    _fake_excel_app(monkeypatch, seen_during)

    if helper == "refresh_workbook":
        excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0)
    elif helper == "run_macro":
        excel_utils.run_macro("C:/x/Report.xlsm", "Module1.Format")
    else:
        excel_utils.paste_image_to_sheet("C:/x/Report.xlsm", 0, "B2", "C:/x/pic.png")

    assert seen_during == [[4242]]
    assert excel_utils._started_excel == {}


# --- the Windows helpers, against real processes -----------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows process APIs")
def test_real_verified_tree_and_handle_kill() -> None:
    """A real child with a differently named grandchild: the walk finds both; each dies by handle."""
    sleeper = (
        "import subprocess,sys,time; "
        "g = subprocess.Popen(['ping','-n','60','127.0.0.1'], stdout=subprocess.DEVNULL); "
        "print(g.pid, flush=True); time.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, "-c", sleeper], stdout=subprocess.PIPE, text=True)
    try:
        grandchild = int(proc.stdout.readline())
        tree = _winproc.verified_descendants(proc.pid)
        assert grandchild in [pid for pid, _, _ in tree], tree
        root_born = _winproc.creation_time(proc.pid)
        assert root_born is not None

        assert _winproc.terminate(proc.pid, root_born + 1) is False, "a different creation time must kill nothing"
        assert proc.poll() is None

        for pid, exe, born in reversed(tree):
            _winproc.terminate(pid, born, exe)
        _winproc.terminate(proc.pid, root_born)  # may already have exited with its child
        proc.wait(10)
        time.sleep(1)
        assert _winproc.creation_time(proc.pid) is None
        assert _winproc.creation_time(grandchild) is None
    finally:
        if proc.poll() is None:
            proc.kill()
