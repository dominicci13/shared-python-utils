"""Unit tests for `seller_automation_utils.excel_utils.refresh_workbook`.

`refresh_workbook` used to save unconditionally after the refresh macro. A failed Power Query
refresh therefore persisted a half-refreshed workbook, and the automation emailed it. Its behaviour
is ported from the reference implementation proven in production in amzn-aged-report (2026-09-17).
A fake xlwings App stands in for Excel, so no Excel is started.
"""
from __future__ import annotations

import threading

import pytest
import pywintypes

from seller_automation_utils import excel_utils
from seller_automation_utils.excel_utils import WorkbookRefreshError


def com_error(message: str = "sharing violation", hresult: int = -2147352567) -> pywintypes.com_error:
    return pywintypes.com_error(hresult, message, None, None)


class FakeBook:
    def __init__(self, app: "FakeApp") -> None:
        self.app = app
        self.saves = 0
        self.closed = False

    def macro(self, name: str):
        self.app.macros_run.append(name)
        return self.app.macro_body

    def save(self) -> None:
        self.app.save_attempts += 1
        if self.app.save_failures:
            self.app.save_failures -= 1
            raise com_error(hresult=FakeApp.save_hresult)
        self.saves += 1

    def close(self) -> None:
        self.closed = True


class FakeApp:
    """Records how it was built and what happened to it. Class attributes configure the next run."""

    macro_result: object = None
    save_failures = 0
    save_hresult = -2147352567
    teardown_error = False
    block: threading.Event | None = None
    start_block: threading.Event | None = None
    instances: list["FakeApp"] = []

    def __init__(self, visible: bool = True, add_book: bool = True) -> None:
        if FakeApp.start_block is not None:
            FakeApp.start_block.wait(5)
        self.visible = visible
        self.add_book = add_book
        self.pid = 4242
        self.display_alerts = True
        self.screen_updating = True
        self.macros_run: list[str] = []
        self.alerts_after_macro: bool | None = None
        self.save_failures = FakeApp.save_failures
        self.save_attempts = 0
        self.book: FakeBook | None = None
        FakeApp.instances.append(self)

        app = self

        class Books:
            def open(self, path: str) -> FakeBook:
                app.book = FakeBook(app)
                return app.book

        self.books = Books()

    def macro_body(self):
        # The real hardened macro's Cleanup hands DisplayAlerts back as True.
        self.display_alerts = True
        if FakeApp.block is not None:
            FakeApp.block.wait(5)
        return FakeApp.macro_result

    def __enter__(self) -> "FakeApp":
        return self

    def __exit__(self, *exc) -> None:
        if FakeApp.teardown_error:
            raise com_error("RPC server unavailable")


@pytest.fixture
def fake_excel(monkeypatch: pytest.MonkeyPatch):
    FakeApp.macro_result = None
    FakeApp.save_failures = 0
    FakeApp.save_hresult = -2147352567
    FakeApp.teardown_error = False
    FakeApp.block = None
    FakeApp.start_block = None
    FakeApp.instances = []
    killed: list[int] = []
    monkeypatch.setattr(excel_utils.xw, "App", FakeApp)
    monkeypatch.setattr(excel_utils, "SAVE_BACKOFF_SEC", 0)
    monkeypatch.setattr(excel_utils, "_kill_pid", killed.append)
    return killed


# --- backward compatibility: an un-hardened `Sub refresh` returns None --------------------------

def test_unhardened_sub_saves_exactly_as_before(fake_excel) -> None:
    excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0)

    app = FakeApp.instances[0]
    assert app.macros_run == ["modUtilities.refresh"]
    assert app.book.saves == 1


def test_hardened_function_empty_string_is_success(fake_excel) -> None:
    FakeApp.macro_result = ""

    excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0)

    assert FakeApp.instances[0].book.saves == 1


# --- the fix: a reported failure is never saved -----------------------------------------------

def test_reported_failure_raises_and_does_not_save(fake_excel) -> None:
    FakeApp.macro_result = "Power Query refresh failed on 'Orders' - error 1004: timeout"

    with pytest.raises(WorkbookRefreshError, match="Report.xlsm: Power Query refresh failed"):
        excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0)

    assert FakeApp.instances[0].book.saves == 0


def test_refresh_error_is_not_a_com_error() -> None:
    """Callers' retry loops catch com_error. A refresh failure must go past them to handle_crash."""
    assert not issubclass(WorkbookRefreshError, pywintypes.com_error)
    assert issubclass(WorkbookRefreshError, RuntimeError)


def test_alerts_are_off_again_before_the_save(fake_excel, monkeypatch: pytest.MonkeyPatch) -> None:
    """The macro turns alerts back on. With alerts on, a failed save raises a modal Save As."""
    seen: list[bool] = []
    original = FakeBook.save

    def spy(self):
        seen.append(self.app.display_alerts)
        return original(self)

    monkeypatch.setattr(FakeBook, "save", spy)

    excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0)

    assert seen == [False]


def test_no_empty_book_and_no_close(fake_excel) -> None:
    """`add_book=False`: no pathless book for Quit to prompt about. No `close()`, because it can
    raise 1004 after a good save, and App.__exit__ tears Excel down anyway."""
    excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0)

    app = FakeApp.instances[0]
    assert app.visible is False and app.add_book is False
    assert app.book.closed is False


# --- OneDrive sharing violations on save are retried in place -----------------------------------

def test_transient_save_failure_is_retried_in_place(fake_excel) -> None:
    FakeApp.save_failures = 2

    excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0)

    app = FakeApp.instances[0]
    assert app.book.saves == 1
    assert app.macros_run == ["modUtilities.refresh"]  # no second refresh


def test_persistent_save_failure_raises_com_error(fake_excel) -> None:
    FakeApp.save_failures = excel_utils.SAVE_ATTEMPTS

    with pytest.raises(pywintypes.com_error):
        excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0)


# --- opt-in pid and time bound ---------------------------------------------------------------

def test_pid_sink_receives_the_excel_pid(fake_excel) -> None:
    pids: list[int] = []

    excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0, pid_sink=pids)

    assert pids == [4242]


def test_timeout_success_path(fake_excel) -> None:
    excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0, timeout=5)

    assert FakeApp.instances[0].book.saves == 1
    assert fake_excel == []


def test_timeout_kills_only_its_own_excel_and_raises(fake_excel) -> None:
    """A COM call stuck on a hidden modal cannot be interrupted, so the only way out is killing
    this run's Excel pid, never every Excel on the machine."""
    FakeApp.block = threading.Event()
    try:
        with pytest.raises(WorkbookRefreshError, match="exceeded 1s"):
            excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0, timeout=1)
        assert fake_excel == [4242]
    finally:
        FakeApp.block.set()


def test_timeout_path_reraises_worker_errors(fake_excel) -> None:
    FakeApp.macro_result = "Power Query refresh failed"

    with pytest.raises(WorkbookRefreshError, match="Power Query refresh failed"):
        excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0, timeout=5)


def test_timeout_path_treats_a_silent_worker_death_as_failure(fake_excel, monkeypatch) -> None:
    """Success is asserted positively. A worker that ends without finishing must never look like a
    finished refresh, because the caller would then email a stale report."""
    monkeypatch.setattr(excel_utils, "_refresh_once", lambda *a, **k: None)
    monkeypatch.setattr(excel_utils, "_mark_finished", lambda event: None)

    with pytest.raises(WorkbookRefreshError, match="without completing"):
        excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0, timeout=5)

# --- kill scope, teardown, fence, return types -------------------------------------------------

def test_teardown_com_error_cannot_mask_a_reported_failure(fake_excel) -> None:
    """If Quit raises after a failed refresh, the caller must still see WorkbookRefreshError,
    not a com_error its retry loop would catch (and then kill every Excel, forever)."""
    FakeApp.macro_result = "Power Query refresh failed"
    FakeApp.teardown_error = True

    with pytest.raises(WorkbookRefreshError, match="Power Query refresh failed"):
        excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0)


def test_timeout_kills_only_this_calls_pid_not_the_callers_old_ones(fake_excel) -> None:
    """A caller's list may hold a pid Windows has since recycled to another automation's Excel."""
    sink = [1111]
    FakeApp.block = threading.Event()
    try:
        with pytest.raises(WorkbookRefreshError, match="exceeded 1s"):
            excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0, timeout=1, pid_sink=sink)
    finally:
        FakeApp.block.set()

    assert fake_excel == [4242]
    assert sink == [1111, 4242]


def test_timed_out_worker_never_saves(fake_excel) -> None:
    """The caller was told the refresh failed; a worker that unblocks later must not save."""
    FakeApp.block = threading.Event()
    threading.Timer(1.5, FakeApp.block.set).start()

    with pytest.raises(WorkbookRefreshError, match="exceeded 1s"):
        excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0, timeout=1)

    assert FakeApp.instances[0].book.saves == 0


@pytest.mark.parametrize("returned", [True, False, 1, 0, -2146826246])
def test_non_string_macro_return_is_a_failure_not_a_guess(fake_excel, returned) -> None:
    FakeApp.macro_result = returned

    with pytest.raises(WorkbookRefreshError, match="unexpected"):
        excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0)

    assert FakeApp.instances[0].book.saves == 0


def test_failure_does_not_wait_before_raising(fake_excel, monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(excel_utils.time, "sleep", slept.append)
    FakeApp.macro_result = "Power Query refresh failed"

    with pytest.raises(WorkbookRefreshError):
        excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=30)

    assert 30 not in slept


def test_save_is_attempted_exactly_save_attempts_times(fake_excel) -> None:
    FakeApp.save_failures = 99

    with pytest.raises(pywintypes.com_error):
        excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0)

    assert FakeApp.instances[0].save_attempts == 4 == excel_utils.SAVE_ATTEMPTS


@pytest.mark.parametrize("hresult", [-2147417848, -2147023174, -2147023170])
def test_save_is_not_retried_once_excel_has_disconnected(fake_excel, hresult: int) -> None:
    """RPC_E_DISCONNECTED, RPC_S_SERVER_UNAVAILABLE, RPC_S_CALL_FAILED: Excel is gone."""
    FakeApp.save_failures = 99
    FakeApp.save_hresult = hresult

    with pytest.raises(pywintypes.com_error):
        excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0)

    assert FakeApp.instances[0].save_attempts == 1

def test_kill_targets_one_pid_and_only_excel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never `/im EXCEL.EXE` (every Excel on the machine): one pid, filtered to Excel."""
    calls: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = "SUCCESS"
        stderr = ""

    monkeypatch.setattr(excel_utils.subprocess, "run", lambda argv, **kw: calls.append(argv) or Done())

    excel_utils._kill_pid(4242)

    (argv,) = calls
    assert argv[0].lower().endswith(r"system32\taskkill.exe")
    assert argv[1:] == ["/f", "/fi", "PID eq 4242", "/fi", "IMAGENAME eq EXCEL.EXE"]
    assert "/im" not in [a.lower() for a in argv]


def test_pid_sink_is_filled_on_a_failed_refresh_too(fake_excel) -> None:
    FakeApp.macro_result = "Power Query refresh failed"
    sink: list[int] = []

    with pytest.raises(WorkbookRefreshError):
        excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0, pid_sink=sink)

    assert sink == [4242]


@pytest.mark.parametrize("bad", [0, -5])
def test_non_positive_timeout_is_rejected(fake_excel, bad: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0, timeout=bad)


def test_fence_holds_between_save_retries(fake_excel, monkeypatch: pytest.MonkeyPatch) -> None:
    """A save that failed once, then retries after the caller gave up, must not save."""
    FakeApp.save_failures = 1
    monkeypatch.setattr(excel_utils, "SAVE_BACKOFF_SEC", 2)

    with pytest.raises(WorkbookRefreshError, match="exceeded 1s"):
        excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0, timeout=1)

    assert FakeApp.instances[0].book.saves == 0


def test_timeout_before_excel_starts_runs_nothing_and_kills_the_late_excel(fake_excel) -> None:
    FakeApp.start_block = threading.Event()
    threading.Timer(1.5, FakeApp.start_block.set).start()

    with pytest.raises(WorkbookRefreshError, match="had not started yet; it started late and was killed"):
        excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0, timeout=1)

    app = FakeApp.instances[0]
    assert app.macros_run == [] and app.book is None
    assert fake_excel == [4242]


def test_interrupt_while_waiting_kills_own_excel_and_never_saves(fake_excel, monkeypatch) -> None:
    import time as _time

    class InterruptingThread(threading.Thread):
        def join(self, timeout=None):
            if timeout == 1:
                _time.sleep(0.5)
                raise KeyboardInterrupt
            return super().join(timeout)

    monkeypatch.setattr(excel_utils.threading, "Thread", InterruptingThread)
    FakeApp.block = threading.Event()
    sink: list[int] = []

    with pytest.raises(KeyboardInterrupt):
        excel_utils.refresh_workbook("C:/x/Report.xlsm", wait=0, timeout=1, pid_sink=sink)

    assert fake_excel == [4242] and sink == [4242]
    FakeApp.block.set()
    _time.sleep(0.5)
    assert FakeApp.instances[0].book.saves == 0
