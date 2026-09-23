"""Single-instance guard: at most one running copy of each automation per host.

An orphaned scheduler (its terminal closed, so Ctrl+C can no longer reach it)
keeps firing beside a freshly started copy of the same automation. Both drive
Chrome on the same profile and kill each other's session. This module stops the
second copy before it does any work.

The lock is a Windows **named mutex**, not a lock file. The kernel destroys the
object when the last handle to it closes, and every handle closes when its
process dies, however it dies, so there is never a stale lock to clean up after
a crash or a kill. The handle is created non-inheritable, so a Chrome, driver or
Excel child left running after its Python parent died does not keep the lock.

Nothing happens at import time. :func:`ensure_single_instance` is called by
:func:`seller_automation_utils.ui_utils.ask_user` (the "Run now?" prompt, the
first thing every fleet entry point does) and again by
:func:`seller_automation_utils.schedule_utils.run_on_schedule` as a backstop. It
is idempotent within one process, so the second call is a no-op.

A small JSON record is written beside the mutex so a blocked copy can name the
holder's PID::

    %LOCALAPPDATA%\\fc-fleet\\locks\\<identity>.json

That file is **information only**. The mutex is the lock; the file can be stale
or missing without changing anything. The holder removes it on a clean exit.
It also carries the holder's process creation time, and a blocked copy names
the PID only while a live process with that PID and that creation time exists,
so a record left by a killed holder never points at an unrelated process that
reused its PID.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

from rich.markup import escape

from seller_automation_utils import fleet_state

log = logging.getLogger(__name__)

__all__ = [
    "ALLOW_MULTIPLE_ENV",
    "MUTEX_PREFIX",
    "ensure_single_instance",
    "instance_identity",
    "mutex_name",
    "lock_info_path",
    "blocked_message",
]

ALLOW_MULTIPLE_ENV = "FC_ALLOW_MULTIPLE_INSTANCES"

# Global\ rather than Local\: Local\ is per logon session, and a copy orphaned
# in another session (a disconnected remote session, a scheduled task in
# session 0) is exactly the duplicate this exists to catch. Creating a mutex in
# Global\ needs no special privilege; only file mappings and symlinks do.
MUTEX_PREFIX = "Global\\seller_automation_utils.single_instance."

_RUN_PREFIX = "run_"
_UNSAFE = re.compile(r"[^a-z0-9_.-]")
_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_OPT_OUT_VALUES = frozenset({"1", "true", "yes"})

# Holding the PyHANDLE here for the life of the process is the whole lock: a
# garbage-collected handle is closed, and a closed handle releases the mutex.
_held: dict[str, Any] = {}
_warned: set[tuple[str, str | None]] = set()
# Two threads racing past the _held check would each CreateMutex, and the
# second would see ALREADY_EXISTS and exit the process as its own duplicate.
_state_lock = threading.Lock()


def instance_identity(explicit: str | None = None, argv0: str | None = None) -> str | None:
    """Work out which automation this process is, for the purpose of locking.

    Fleet entry points are all ``run_<name>.py`` with a unique ``<name>`` per
    repo, the same rule :func:`fleet_state.automation_name` uses for heartbeats.
    The difference is what happens when that rule does not apply: the heartbeat
    falls back to any name at all, while a lock must not, because two unrelated
    ``main.py`` scripts (or two pytest sessions) would then block each other.
    So anything that is not an explicit name or a ``run_*`` script yields None,
    meaning "no stable identity, do not lock".

    The result is casefolded because Windows paths are case-insensitive and
    mutex names are not: ``python RUN_Demo.py`` and ``python run_demo.py`` start
    the same automation and must contend for the same mutex.

    Args:
        explicit (str | None): Caller-supplied automation name, which wins when
            non-blank.
        argv0 (str | None): The entry script path, normally ``sys.argv[0]``.

    Returns:
        str | None: The identity (e.g. ``demo_report``), or None if the
            process has no stable identity.
    """
    if explicit and explicit.strip():
        return _sanitize(explicit)
    if not argv0:
        return None
    stem = Path(argv0).stem.casefold()
    if not stem.startswith(_RUN_PREFIX) or len(stem) <= len(_RUN_PREFIX):
        return None
    return _sanitize(stem[len(_RUN_PREFIX):])


def _sanitize(name: str) -> str | None:
    """Casefold and reduce ``name`` to characters safe in a mutex and a filename."""
    cleaned = _UNSAFE.sub("_", name.strip().casefold())
    return cleaned or None


def mutex_name(identity: str) -> str:
    """Full kernel object name for an automation's mutex.

    Args:
        identity (str): Output of :func:`instance_identity`.

    Returns:
        str: ``Global\\seller_automation_utils.single_instance.<identity>``.

    Raises:
        ValueError: If ``identity`` is blank, since an empty suffix would make
            every automation share one mutex.
    """
    cleaned = _sanitize(identity or "")
    if not cleaned:
        raise ValueError("Instance identity must not be blank.")
    return f"{MUTEX_PREFIX}{cleaned}"


def lock_info_path(identity: str) -> Path:
    """Where the holder records its PID, beside the fleet heartbeats.

    Args:
        identity (str): Output of :func:`instance_identity`.

    Returns:
        Path: ``<fleet state root>\\locks\\<identity>.json``.
    """
    return fleet_state.state_root() / "locks" / f"{identity}.json"


def blocked_message(identity: str, holder: dict[str, Any] | None, access_denied: bool = False) -> str:
    """Build the log line a blocked second copy emits before exiting.

    Args:
        identity (str): The automation that is already running.
        holder (dict[str, Any] | None): The holder's lock record, already
            checked by :func:`_live_holder`, or None if it could not be read or
            did not match a live process.
        access_denied (bool): True when the mutex exists but belongs to another
            security context (typically the same automation started from an
            elevated terminal), so it could not even be opened.

    Returns:
        str: One self-contained sentence naming the automation, the holder's PID
            when known, and what to do about it.
    """
    pid = holder.get("pid") if isinstance(holder, dict) else None
    if isinstance(pid, int) and not isinstance(pid, bool):
        started = escape(str(holder.get("started") or "unknown time"))
        who = f"PID {pid}, started {started}"
        hint = f" If that copy is an orphan, stop it by PID (Stop-Process -Id {pid})."
    else:
        who = "holder PID unknown"
        hint = " Find it with Get-CimInstance Win32_Process and stop it by PID, never by name."
    context = " under a different security context, for example an elevated terminal" if access_denied else ""
    return (
        f"Another copy of automation '{identity}' is already running{context} ({who}). "
        f"This copy is exiting without doing anything; the running copy is unaffected.{hint}"
    )


def _guard_disabled() -> bool:
    """True only when the opt-out variable is ``1``, ``true`` or ``yes``.

    Anything else, including ``0`` and ``false``, keeps the guard on: a value
    that reads as "no" must never switch the guard off.
    """
    return os.environ.get(ALLOW_MULTIPLE_ENV, "").strip().casefold() in _OPT_OUT_VALUES


def _process_creation_time(pid: int) -> int | None:
    """Creation time of a running process, in microseconds since the epoch.

    Args:
        pid (int): Process to look up.

    Returns:
        int | None: The creation time, or None if ``pid`` is not running or
            cannot be opened from this security context. Never raises.
    """
    try:
        import win32api
        import win32process

        handle = win32api.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except Exception:
        return None
    try:
        if win32process.GetExitCodeProcess(handle) != _STILL_ACTIVE:
            return None
        return round(win32process.GetProcessTimes(handle)["CreationTime"].timestamp() * 1_000_000)
    except Exception:
        return None
    finally:
        handle.Close()


def _live_holder(holder: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return ``holder`` only if its PID is a live process created when it says.

    A holder killed without a clean exit leaves its record behind, and Windows
    reuses PIDs, so a bare PID from the record could name an unrelated process
    that a user would then be told to stop.

    Args:
        holder (dict[str, Any] | None): Lock record from :func:`_read_lock_info`.

    Returns:
        dict[str, Any] | None: ``holder`` unchanged, or None on any mismatch.
    """
    if not isinstance(holder, dict):
        return None
    pid = holder.get("pid")
    created = holder.get("process_created")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    if not isinstance(created, int) or isinstance(created, bool):
        return None
    return holder if _process_creation_time(pid) == created else None


def _create_mutex(name: str) -> tuple[Any, str]:
    """Create or open the named mutex and report who got there first.

    ``bInitialOwner`` is False because ownership is irrelevant here: the lock is
    the *existence* of the named object, and ownership is per thread, which
    would tie the lock to whichever thread happened to call this.

    Args:
        name (str): Full mutex name from :func:`mutex_name`.

    Returns:
        tuple[Any, str]: ``(handle, "acquired")`` if this process created it;
            ``(None, "held")`` if it already existed; ``(None, "denied")`` if it
            exists but cannot be opened from this security context.

    Raises:
        pywintypes.error: Any other Win32 failure. Carrying on unguarded would
            silently reopen the hazard this module exists to close.
    """
    import pywintypes
    import win32api
    import win32event

    try:
        handle = win32event.CreateMutex(None, False, name)
    except pywintypes.error as exc:
        if exc.winerror == _ERROR_ACCESS_DENIED:
            return None, "denied"
        raise
    if win32api.GetLastError() == _ERROR_ALREADY_EXISTS:
        handle.Close()
        return None, "held"
    return handle, "acquired"


def _read_lock_info(path: Path) -> dict[str, Any] | None:
    """Read a holder record. Never raises."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _remove_lock_info(path: Path, pid: int) -> None:
    """Delete the holder record on exit, but only if it is still ours."""
    info = _read_lock_info(path)
    if info is None or info.get("pid") != pid:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        log.debug("Could not remove lock record %s", path, exc_info=True)


def ensure_single_instance(name: str | None = None) -> None:
    """Hold this automation's single-instance lock, or exit if another copy has it.

    Call before any work. Safe to call repeatedly: once this process holds the
    lock for an identity, later calls return at once.

    A blocked copy logs one ERROR line and exits with status 0. It does not
    call ``handle_crash``, send mail, write a heartbeat or leave any file
    behind: a duplicate start is the guard working, not a failure to report.
    Exit status 0 also keeps a launcher that restarts on failure from
    relaunching straight into the same refusal.

    Does nothing, with a WARNING logged once, when
    ``FC_ALLOW_MULTIPLE_INSTANCES`` is ``1``, ``true`` or ``yes`` (any other
    value keeps the guard on), or when the process has no stable identity (not
    started from a ``run_*.py`` script and no ``name`` given). ``FC_NO_PROMPT``
    has no effect here: an unattended start is exactly when a duplicate goes
    unnoticed.

    Args:
        name (str | None): Explicit automation name. Defaults to the one
            derived from ``sys.argv[0]`` (``run_demo_report.py`` ->
            ``demo_report``).

    Raises:
        SystemExit: With code 0, when another live process holds the lock.
        pywintypes.error: If the mutex cannot be created for any reason other
            than already existing.
    """
    identity = instance_identity(name, sys.argv[0] if sys.argv else None)
    with _state_lock:
        if identity is None:
            _warn_once(("no-identity", None), (
                "Single-instance guard not applied: this process was not started from a "
                "run_*.py script and no automation name was given."
            ))
            return
        if identity in _held:
            return
        if _guard_disabled():
            _warn_once(("disabled", identity), (
                f"{ALLOW_MULTIPLE_ENV} is set: single-instance guard is OFF for '{identity}'. "
                f"A second copy of this automation would not be stopped."
            ))
            return

        handle, state = _create_mutex(mutex_name(identity))
        if state != "acquired":
            info_path = lock_info_path(identity)
            holder = _live_holder(_read_lock_info(info_path))
            log.error(blocked_message(identity, holder, access_denied=state == "denied"))
            raise SystemExit(0)

        _held[identity] = handle

    info_path = lock_info_path(identity)
    pid = os.getpid()
    fleet_state._atomic_write_json(info_path, {
        "name": identity,
        "pid": pid,
        "started": fleet_state._now_iso(),
        "process_created": _process_creation_time(pid),
        "mutex": mutex_name(identity),
    })
    atexit.register(_remove_lock_info, info_path, pid)
    log.info(f"Single-instance lock held for [cyan]{identity}[/cyan] (PID {pid}).")


def _warn_once(key: tuple[str, str | None], message: str) -> None:
    """Log ``message`` at WARNING the first time ``key`` is seen in this process."""
    if key in _warned:
        return
    _warned.add(key)
    log.warning(message)
