"""Windows process helpers with no dependency beyond pywin32 and ctypes.

Used to clean up after a crash without touching processes that belong to anyone else:
snapshot the process table, walk a process tree while rejecting stale parent-pid links, and
terminate a process only through a handle that is verified to be the same process that was
checked (a pid plus its creation time).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging

from rich.markup import escape

log = logging.getLogger(__name__)

_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def process_table() -> list[tuple[int, int, str]]:
    """Every running process as ``(pid, recorded parent pid, exe name)``, from one snapshot.

    The parent pid is whatever Windows recorded at creation and is never re-checked, so on
    its own it does not prove parentage. Use ``verified_descendants``. Never raises.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        return []
    found: list[tuple[int, int, str]] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            found.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile))
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    except Exception:
        return found
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def verified_descendants(
    root_pid: int, table: list[tuple[int, int, str]] | None = None
) -> list[tuple[int, str, int]]:
    """Descendants of ``root_pid`` as ``(pid, exe, creation time)``, parents before children.

    Windows records a parent pid once and never re-checks it. So a process whose real parent
    exited looks like a child of whatever later process received that pid. A descendant is
    kept only if it is not older than the parent it hangs under. That cuts off every stale
    branch, however deep, where ``taskkill /t`` would walk straight into it.
    """
    rows = table if table is not None else process_table()
    by_parent: dict[int, list[tuple[int, str]]] = {}
    for pid, ppid, exe in rows:
        if pid != ppid:
            by_parent.setdefault(ppid, []).append((pid, exe))
    born = {root_pid: creation_time(root_pid)}
    found: list[tuple[int, str, int]] = []
    queue = [root_pid]
    while queue:
        parent = queue.pop(0)
        for pid, exe in by_parent.get(parent, []):
            if pid in born:
                continue
            child_born = creation_time(pid)
            if child_born is None or born[parent] is None or child_born < born[parent]:
                continue
            born[pid] = child_born
            found.append((pid, exe, child_born))
            queue.append(pid)
    return found


def _creation_time_of(handle: object) -> int | None:
    import win32process

    if win32process.GetExitCodeProcess(handle) != _STILL_ACTIVE:
        return None
    return round(win32process.GetProcessTimes(handle)["CreationTime"].timestamp() * 1_000_000)


def creation_time(pid: int) -> int | None:
    """Creation time of a running process in microseconds since the epoch (millisecond precision), or None.

    None when ``pid`` is not running or cannot be opened. A pid plus its creation time
    names one process for good; Windows reuses pids but never with the same creation time.
    Never raises.
    """
    try:
        import win32api

        handle = win32api.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except Exception:
        return None
    try:
        return _creation_time_of(handle)
    except Exception:
        return None
    finally:
        handle.Close()


def terminate(pid: int, created: int, label: str = "") -> bool:
    """Terminate ``pid`` only if it is still the process created at ``created``. Never raises.

    The check and the kill go through one open handle. Windows cannot reuse a pid while a
    handle to it is open, so there is no window in which the pid could come to name some
    other process between the check and the kill.

    Returns:
        bool: True if this call terminated the process.
    """
    try:
        import win32api

        handle = win32api.OpenProcess(_PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except Exception:
        return False  # already gone, or not ours to open
    try:
        if _creation_time_of(handle) != created:
            log.info(f"Not killing pid {pid} {escape(label)}: it is no longer the same process.")
            return False
        win32api.TerminateProcess(handle, 1)
        log.info(f"Killed pid {pid} {escape(label)}".rstrip() + ".")
        return True
    except Exception as exc:
        log.warning(f"Could not kill pid {pid} {escape(label)}: {escape(str(exc))}")
        return False
    finally:
        handle.Close()
