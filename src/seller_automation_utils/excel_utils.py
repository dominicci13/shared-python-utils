from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pythoncom
import pywintypes
import xlwings as xw
import logging
from rich.markup import escape

log = logging.getLogger(__name__)

# A save can hit a OneDrive sharing violation that clears within seconds. It is retried
# in place, so a busy OneDrive does not cost a whole re-refresh.
SAVE_ATTEMPTS = 4
SAVE_BACKOFF_SEC = 10

# COM errors that mean Excel itself is gone. Retrying a save cannot help once they appear.
_EXCEL_GONE_HRESULTS = frozenset({
    -2147417848,  # RPC_E_DISCONNECTED: the object invoked has disconnected from its clients
    -2147023174,  # RPC_S_SERVER_UNAVAILABLE (0x800706BA)
    -2147023170,  # RPC_S_CALL_FAILED (0x800706BE)
})


class WorkbookRefreshError(RuntimeError):
    """A refresh macro reported a failed Power Query refresh, or the refresh overran its time bound.

    Deliberately not a ``pywintypes.com_error``: callers retry COM errors as transient, and
    neither of these is transient. It must go straight to the caller's crash handler.
    """


def _kill_pid(pid: int) -> None:
    """Terminate one Excel process by pid, ignoring one that has already gone.

    Never ``custom_functions.kill_app("excel")``, which kills every Excel on the machine,
    including another automation's. The filters mean a recycled pid that now belongs to
    some other program is left alone.
    """
    taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
    result = subprocess.run(
        [str(taskkill), "/f", "/fi", f"PID eq {pid}", "/fi", "IMAGENAME eq EXCEL.EXE"],
        capture_output=True,
        # taskkill writes in the OEM code page. Never let a decode error replace the real one.
        encoding="oem",
        errors="replace",
        check=False,
    )
    log.info(f"taskkill Excel pid {pid}: rc={result.returncode} {escape((result.stdout or result.stderr).strip())}")


def _save_workbook(wb: object, name: str, cancelled: threading.Event | None = None) -> bool:
    """Save, retrying COM errors in place (OneDrive sharing violations clear in seconds).

    A COM error that means Excel itself is gone is not retried. The ``cancelled`` fence
    is checked before every attempt, so a refresh the caller has already abandoned is
    never saved, even between retries.

    Returns:
        bool: True if saved, False if abandoned before an attempt.

    Raises:
        pywintypes.com_error: If every attempt failed, or Excel disconnected.
    """
    for attempt in range(1, SAVE_ATTEMPTS + 1):
        if cancelled is not None and cancelled.is_set():
            return False
        try:
            wb.save()
            return True
        except pywintypes.com_error as exc:
            if attempt == SAVE_ATTEMPTS or exc.hresult in _EXCEL_GONE_HRESULTS:
                raise
            log.error(
                f"Saving [cyan]{escape(name)}[/cyan] failed (attempt {attempt}/{SAVE_ATTEMPTS}): "
                f"{escape(str(exc))}. Retrying."
            )
            time.sleep(SAVE_BACKOFF_SEC)
    return False  # unreachable: the last attempt either returns or raises


def _macro_verdict(name: str, result: object) -> WorkbookRefreshError | None:
    """Turn the refresh macro's return value into a failure, or ``None`` for success.

    ``None`` (an un-hardened ``Sub``) and ``""`` mean success. A non-empty string is the
    macro's own failure reason. Anything else (a Boolean, a number, an Excel error value)
    is not a return this contract defines, so it is treated as a failure, not guessed at.
    """
    if result is None or result == "":
        return None
    if isinstance(result, str):
        return WorkbookRefreshError(f"{name}: {result}")
    return WorkbookRefreshError(
        f"{name}: refresh macro returned an unexpected {type(result).__name__} ({result!r}); "
        f'expected "" on success or a failure reason'
    )


def _refresh_once(
    workbook_path: str,
    macro_name: str,
    wait: int,
    pids: list[int],
    cancelled: threading.Event | None = None,
) -> None:
    """Open, refresh, check the macro's verdict, and save only on success.

    The verdict is raised only after Excel has been torn down, so an error during teardown
    can never turn a reported refresh failure back into a ``com_error``, which callers retry.
    """
    name = Path(workbook_path).name
    log.info(f"Opening workbook: [cyan]{escape(name)}[/cyan]")
    verdict: WorkbookRefreshError | None = None

    try:
        # add_book=False: the xlwings default adds an empty, pathless workbook, and Quit then
        # has something to prompt about on a hidden Excel.
        with xw.App(visible=False, add_book=False) as excel:
            pids.append(excel.pid)

            excel.display_alerts = False
            excel.screen_updating = False

            if cancelled is not None and cancelled.is_set():
                # Excel started only after the caller gave up. Leave it to App.__exit__.
                verdict = WorkbookRefreshError(f"{name}: refresh abandoned after its time bound; not run")
            else:
                wb = excel.books.open(workbook_path)

                log.info(f"Running macro: [cyan]{escape(macro_name)}[/cyan]")
                verdict = _macro_verdict(name, wb.macro(macro_name)())

                # The macro's Cleanup restores DisplayAlerts = True. With alerts on, a save that
                # fails on a sharing violation raises a modal Save As instead of an exception.
                excel.display_alerts = False

                if verdict is not None:
                    # Log now, so the reason survives even if teardown raises on the way out.
                    log.error(escape(str(verdict)))
                else:
                    time.sleep(wait)
                    if not _save_workbook(wb, name, cancelled):
                        # The caller already gave up on this refresh and was told it failed.
                        verdict = WorkbookRefreshError(f"{name}: refresh abandoned after its time bound; not saved")
            # No wb.close(): it can raise 1004 after a good save, and App.__exit__ quits and
            # then kills Excel on every path anyway.
    except pywintypes.com_error as exc:
        if verdict is not None:
            raise verdict from exc
        raise

    if verdict is not None:
        raise verdict

    log.success("Workbook refreshed and saved successfully.")


def _mark_finished(event: threading.Event) -> None:
    event.set()


def refresh_workbook(
    workbook_path: str,
    macro_name: str = "modUtilities.refresh",
    wait: int = 30,
    *,
    timeout: int | None = None,
    pid_sink: list[int] | None = None,
) -> None:
    """Open an Excel workbook, run its refresh macro, and save it only if the refresh succeeded.

    Opens the workbook in a hidden Excel instance and runs the macro. A hardened
    ``Function refresh() As String`` returns ``""`` on success or a reason on failure. On
    a reason nothing is saved and ``WorkbookRefreshError`` is raised, so a half-refreshed
    workbook is never persisted or emailed. An un-hardened ``Sub refresh`` returns
    ``None`` and behaves exactly as before. Any other return value is treated as a
    failure. A save that hits a OneDrive sharing violation is retried in place.

    Args:
        workbook_path (str): Absolute path to the .xlsm or .xlsx workbook.
        macro_name (str): Full macro name to execute. Defaults to "modUtilities.refresh".
        wait (int): Seconds to wait after a successful macro before saving. Hardened macros
            refresh synchronously (BackgroundQuery=False), so callers pass ``wait=0``.
            Defaults to 30 for legacy macros.
        timeout (int | None): Opt-in hard time bound in seconds. The refresh runs on a
            worker thread. If it overruns (typically a modal dialog on the hidden Excel,
            which no COM call can get past), the Excel started by *this call* is killed,
            the worker is told not to save, and ``WorkbookRefreshError`` is raised.
            ``None`` (default) keeps the old same-thread behaviour. It is opt-in because
            every workbook's normal refresh time differs, and no single default is safe
            for all of them.
        pid_sink (list[int] | None): If given, the Excel pid this call starts is appended
            to it, for the caller's records. Only this call's own pid is ever killed, never
            others already in the list.

    Raises:
        WorkbookRefreshError: The macro reported a failed refresh or returned something
            unexpected, the ``timeout`` was exceeded, or the worker ended without completing.
        pywintypes.com_error: Every save attempt failed, or a COM error from the macro.
        FileNotFoundError: If the workbook does not exist at the given path.
    """
    # Only this call's own Excel is ever killed. A caller's list may hold pids from earlier
    # calls that Windows has since recycled, possibly to another automation's Excel.
    own_pids: list[int] = []

    if timeout is None:
        try:
            _refresh_once(workbook_path, macro_name, wait, own_pids)
        finally:
            if pid_sink is not None:
                pid_sink.extend(own_pids)
        return

    if timeout <= 0:
        raise ValueError(f"timeout must be a positive number of seconds, got {timeout!r}")

    def record_pids() -> None:
        if pid_sink is not None:
            pid_sink.extend(p for p in own_pids if p not in pid_sink)

    raised: dict[str, BaseException] = {}
    # Success is only ever asserted positively. A worker that dies without storing an
    # error must never look like one that finished, or the caller emails a stale report.
    finished = threading.Event()
    cancelled = threading.Event()

    def work() -> None:
        try:
            # COM is per-thread and xlwings does not initialise it. Inside the try, so a
            # failure here is reported instead of killing the thread silently.
            pythoncom.CoInitialize()
            try:
                _refresh_once(workbook_path, macro_name, wait, own_pids, cancelled)
            finally:
                pythoncom.CoUninitialize()
        except BaseException as exc:
            raised["error"] = exc
        else:
            _mark_finished(finished)

    worker = threading.Thread(target=work, name="excel-refresh", daemon=True)
    worker.start()
    try:
        worker.join(timeout)
    except BaseException:
        # Interrupted (e.g. Ctrl+C) while waiting: fence, kill our own Excel, then let it propagate.
        cancelled.set()
        for pid in list(own_pids):
            _kill_pid(pid)
        record_pids()
        raise

    name = Path(workbook_path).name
    if worker.is_alive():
        # Fence first: whatever the worker does next, it must not save.
        cancelled.set()
        killed = list(own_pids)
        log.error(
            f"Refreshing [cyan]{escape(name)}[/cyan] exceeded {timeout}s. Excel is stuck, most likely "
            f"on a modal dialog. Killing pid(s) {killed or 'none yet started'}."
        )
        for pid in killed:
            _kill_pid(pid)
        worker.join(30)
        # An Excel the worker started after the first kill (a slow start) is killed too.
        late = [p for p in own_pids if p not in killed]
        for pid in late:
            _kill_pid(pid)
        record_pids()
        if killed:
            outcome = "Excel was killed"
        elif late:
            outcome = "Excel had not started yet; it started late and was killed"
        else:
            outcome = "no Excel had started"
        raise WorkbookRefreshError(f"{name}: refresh exceeded {timeout}s, {outcome}")

    record_pids()
    if "error" in raised:
        raise raised["error"]

    if not finished.is_set():
        raise WorkbookRefreshError(f"{name}: the refresh thread ended without completing or reporting an error")


def run_macro(workbook_path: str, macro_name: str) -> None:
    """Open an Excel workbook, run a named macro, and save it.

    Intended for synchronous macros that complete immediately (e.g., formatting,
    chart resizing). For macros that trigger async data refreshes, use
    refresh_workbook() which includes a configurable wait period.

    Args:
        workbook_path (str): Absolute path to the .xlsm or .xlsx workbook.
        macro_name (str): Full macro name to execute (e.g., "Module1.FormatSheet").

    Raises:
        FileNotFoundError: If the workbook does not exist at the given path.
        xlwings.XlwingsError: If the macro cannot be found or fails to run.
    """
    log.info(f"Running macro [cyan]{macro_name}[/cyan] in [cyan]{Path(workbook_path).name}[/cyan]")

    with xw.App(visible=False) as excel:
        excel.display_alerts = False
        excel.screen_updating = False

        wb = excel.books.open(workbook_path)
        wb.macro(macro_name)()
        wb.save()
        wb.close()

        excel.display_alerts = True
        excel.screen_updating = True

    log.success(f"Macro [cyan]{macro_name}[/cyan] completed successfully.")


def paste_image_to_sheet(workbook_path: str, sheet: str | int, cell: str, image_path: str) -> None:
    """Open an Excel workbook and insert an image anchored at a given cell.

    Opens the workbook in a hidden Excel instance, inserts the image with its
    top-left corner aligned to the target cell, then saves and closes.

    Args:
        workbook_path (str): Absolute path to the .xlsm or .xlsx workbook.
        sheet (str | int): Sheet name (str) or index (int, zero-based) to insert into.
        cell (str): Cell address to anchor the image's top-left corner (e.g., "B5").
        image_path (str): Absolute path to the image file to insert.

    Raises:
        FileNotFoundError: If the workbook or image file does not exist.
        xlwings.XlwingsError: If the sheet cannot be found or the image fails to insert.
    """
    log.info(f"Inserting image into [cyan]{Path(workbook_path).name}[/cyan] at {cell}.")

    with xw.App(visible=False) as excel:
        excel.display_alerts = False
        excel.screen_updating = False

        wb = excel.books.open(workbook_path)
        ws = wb.sheets[sheet]
        ws.pictures.add(image_path, left=ws.range(cell).left, top=ws.range(cell).top)
        wb.save()
        wb.close()

        excel.display_alerts = True
        excel.screen_updating = True

    log.success(f"Image inserted at [cyan]{cell}[/cyan] successfully.")
