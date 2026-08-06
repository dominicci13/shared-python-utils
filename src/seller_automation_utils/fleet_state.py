"""Durable on-disk fleet state: heartbeats and crash records.

Every automation in the fleet is a long-lived process that blocks in
:func:`seller_automation_utils.schedule_utils.run_on_schedule`. Nothing outside
that process can currently tell whether it is healthy: the only failure signal
is the ``[CRASH]`` email from :func:`alert_utils.handle_crash`, which requires
the automation to catch its own exception *and* to still be able to drive
Outlook COM. A killed process, a logged-off Windows session, a scheduler thread
that quietly stopped firing, or a broken Outlook all fail silently today.

This module is the missing signal. It defines a small file contract that
automations write and the ``fleet-control`` dashboard reads:

.. code-block:: text

    %LOCALAPPDATA%\\fc-fleet\\
        heartbeats\\<name>.json
        crashes\\<name>\\<timestamp>\\crash.json
                                     screenshot.png
                                     dom.txt

``fleet-control`` deliberately reads these files directly rather than importing
this package: it is a small FastAPI app, and depending on this library would
drag in selenium, pandas, pyodbc, xlwings and pywin32 to read some JSON. The
duplicated path logic there is the intended cost of that isolation, so **treat
the layout above as a published contract** — changing it breaks the dashboard.

Nothing here ever raises. A monitoring side-channel that can kill the
automation it monitors is worse than no monitoring at all, so every function
swallows its own errors and reports success as a bool.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "automation_name",
    "state_root",
    "heartbeat_dir",
    "heartbeat_path",
    "crash_root",
    "write_heartbeat",
    "read_heartbeat",
    "clear_heartbeat",
    "snapshot_jobs",
    "record_crash",
    "mark_crash_emailed",
    "prune_crashes",
    "HeartbeatWriter",
    "STATE_DIR_ENV",
    "DEFAULT_BEAT_INTERVAL",
    "CRASH_RETENTION_DAYS",
]

STATE_DIR_ENV = "FC_FLEET_STATE_DIR"

# 15s beat against the dashboard's 90s staleness threshold — 6x headroom, so
# neither GC nor a briefly wedged main loop reads as a dead automation.
DEFAULT_BEAT_INTERVAL = 15.0

CRASH_RETENTION_DAYS = 30


def automation_name(explicit: str | None = None, func: Any = None) -> str:
    """Work out which automation this process is, for heartbeat and crash keys.

    Derived rather than passed so the 18 existing repos need no source edit:
    every entry point in the fleet is ``run_<module>.py`` where ``<module>`` is
    also the logger name and the log filename, so stripping the ``run_`` prefix
    off ``sys.argv[0]`` reproduces it exactly.

    Both the heartbeat and the crash archive key off this, which is why it is
    not derived from the display name a caller passes to ``handle_crash``:
    those are human labels ("Amazon CA FBA Inventory" for ``amzn_ca_fba_inventory``)
    and are sometimes built at runtime ("eBay Best Offers (failed on X)"), so
    they cannot be slugified back into a stable identity.

    Args:
        explicit (str | None): Caller-supplied name, which always wins.
        func (Any): The scheduled function, used as a last resort when the
            process was not started from a ``run_*.py`` script.

    Returns:
        str: The automation's name (e.g. ``ebay_best_offers``).
    """
    if explicit:
        return explicit
    try:
        stem = Path(sys.argv[0]).stem
        if stem.startswith("run_") and len(stem) > 4:
            return stem[4:]
        if stem:
            return stem
    except Exception:
        pass
    module = getattr(func, "__module__", "") or ""
    return module.rsplit(".", 1)[-1] or "automation"


def state_root() -> Path:
    """Root directory for all fleet state.

    Returns:
        Path: ``%LOCALAPPDATA%\\fc-fleet``, or the override in
            ``FC_FLEET_STATE_DIR`` (used by the tests, and by anything running
            under an account with no LOCALAPPDATA).
    """
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "fc-fleet"


def heartbeat_dir() -> Path:
    """Directory holding one JSON heartbeat per automation."""
    return state_root() / "heartbeats"


def heartbeat_path(name: str) -> Path:
    """Path to one automation's heartbeat file.

    Args:
        name (str): The automation's logger name (e.g. ``ebay_best_offers``),
            which is also its log filename stem.

    Returns:
        Path: Location of that automation's ``<name>.json``.
    """
    return heartbeat_dir() / f"{name}.json"


def crash_root() -> Path:
    """Root directory of the crash archive."""
    return state_root() / "crashes"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> bool:
    """Write ``payload`` to ``path`` as JSON, atomically.

    Writes to a temporary file in the same directory and then ``os.replace``s
    it over the target, so a reader never observes a half-written file.
    ``os.replace`` is atomic on Windows, but it fails with ``PermissionError``
    if the destination is momentarily open by a reader or an AV scanner — hence
    the short retry rather than a single attempt.

    Args:
        path (Path): Destination file.
        payload (dict[str, Any]): JSON-serializable content.

    Returns:
        bool: True on success. Never raises.
    """
    tmp: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))

        for attempt in range(3):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.05)
        return False
    except Exception:
        log.debug("Could not write fleet state to %s", path, exc_info=True)
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def _now_iso() -> str:
    """Current local time as an ISO-8601 string with UTC offset."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def snapshot_jobs(scheduler: Any) -> list[dict[str, Any]]:
    """Read a scheduler's jobs and their next fire times, live.

    Deliberately re-reads ``scheduler.get_jobs()`` on every call instead of
    caching: if the scheduler's background thread dies while the process stays
    alive, the jobs remain listed but their ``next_run_time`` stops advancing.
    That frozen timestamp is the only externally visible symptom of the failure
    mode, so caching it would hide exactly what this exists to catch.

    Args:
        scheduler (Any): An APScheduler 3.x scheduler.

    Returns:
        list[dict[str, Any]]: One ``{"id", "next_run"}`` entry per job, with
            ``next_run`` an ISO string or None (None means paused, or the
            scheduler was never started). Empty list if the scheduler cannot be
            read at all.
    """
    try:
        jobs = scheduler.get_jobs()
    except Exception:
        log.debug("Could not read scheduler jobs.", exc_info=True)
        return []

    out: list[dict[str, Any]] = []
    for job in jobs:
        try:
            next_run = getattr(job, "next_run_time", None)
            out.append({
                "id": str(getattr(job, "id", "")),
                "next_run": next_run.isoformat(timespec="seconds") if next_run else None,
            })
        except Exception:
            continue
    return out


def write_heartbeat(
    name: str,
    *,
    pid: int,
    started: str,
    jobs: list[dict[str, Any]],
    last_result: dict[str, Any] | None = None,
) -> bool:
    """Write one automation's heartbeat file.

    Args:
        name (str): Automation logger name (e.g. ``ebay_best_offers``).
        pid (int): The automation's process id, so the dashboard can confirm
            the process is not merely a stale file left by a dead run.
        started (str): ISO timestamp of when this process started.
        jobs (list[dict[str, Any]]): Output of :func:`snapshot_jobs`.
        last_result (dict[str, Any] | None): Most recent job outcome, as
            ``{"at", "ok", "error", "job_id"}``. None until the first run.

    Returns:
        bool: True if the file was written. Never raises.
    """
    return _atomic_write_json(heartbeat_path(name), {
        "name": name,
        "pid": pid,
        "started": started,
        "beat": _now_iso(),
        "beat_interval": DEFAULT_BEAT_INTERVAL,
        "jobs": jobs,
        "last_result": last_result,
    })


def clear_heartbeat(name: str) -> None:
    """Remove one automation's heartbeat file on clean shutdown.

    Lets the dashboard tell a deliberate stop (file gone the moment Ctrl+C is
    handled) from a kill or a crash (file left behind, then ageing into
    staleness). Without this every clean stop would look like a failure for the
    length of the staleness window.

    Args:
        name (str): Automation logger name.
    """
    try:
        heartbeat_path(name).unlink(missing_ok=True)
    except OSError:
        log.debug("Could not clear heartbeat for %s", name, exc_info=True)


def read_heartbeat(name: str) -> dict[str, Any] | None:
    """Read back one heartbeat file.

    Args:
        name (str): Automation logger name.

    Returns:
        dict[str, Any] | None: The parsed heartbeat, or None if it is missing
            or unreadable. Never raises.
    """
    try:
        return json.loads(heartbeat_path(name).read_text(encoding="utf-8"))
    except Exception:
        return None


class HeartbeatWriter:
    """Rate-limited heartbeat emitter for a scheduler's main loop.

    ``run_on_schedule`` ticks once a second; writing a file that often is
    pointless churn. :meth:`beat` is therefore safe to call every tick and only
    touches the disk once per ``interval``.

    Args:
        name (str): Automation logger name.
        interval (float): Minimum seconds between disk writes.
    """

    def __init__(self, name: str, interval: float = DEFAULT_BEAT_INTERVAL) -> None:
        self.name = name
        self.interval = interval
        self.pid = os.getpid()
        self.started = _now_iso()
        self._last_write = 0.0
        self._last_result: dict[str, Any] | None = None
        self._last_jobs: list[dict[str, Any]] = []

    def record_result(self, *, ok: bool, error: str | None = None, job_id: str | None = None) -> None:
        """Record a job outcome and flush it immediately.

        Called from the scheduler's event listener, so a failure reaches the
        dashboard on the next poll rather than waiting out the beat interval.

        Reuses the cached job snapshot rather than re-reading the scheduler:
        this runs on APScheduler's own listener thread, and the cache both
        avoids reaching back into the scheduler from inside its callback and
        keeps ``jobs`` populated. Writing an empty list here would blank the
        next-run times for a full interval, which is precisely the signature
        the dashboard reads as a dead scheduler.

        Args:
            ok (bool): Whether the job completed without raising.
            error (str | None): Exception summary when ``ok`` is False.
            job_id (str | None): Which job, for multi-job automations.
        """
        self._last_result = {
            "at": _now_iso(),
            "ok": ok,
            "error": error,
            "job_id": job_id,
        }
        self._write(self._last_jobs, force=True)

    def beat(self, jobs: list[dict[str, Any]], force: bool = False) -> None:
        """Emit a heartbeat if the interval has elapsed.

        Args:
            jobs (list[dict[str, Any]]): Output of :func:`snapshot_jobs`.
            force (bool): Write even if the interval has not elapsed.
        """
        if jobs:
            self._last_jobs = jobs
        self._write(jobs or self._last_jobs, force=force)

    def _write(self, jobs: list[dict[str, Any]], force: bool) -> None:
        """Rate-limit gate in front of :func:`write_heartbeat`."""
        now = time.monotonic()
        if not force and (now - self._last_write) < self.interval:
            return
        self._last_write = now
        write_heartbeat(
            self.name,
            pid=self.pid,
            started=self.started,
            jobs=jobs,
            last_result=self._last_result,
        )


def record_crash(
    name: str,
    *,
    traceback_text: str,
    display_name: str | None = None,
    tabs: str | None = None,
    screenshot_path: str | None = None,
    dom_path: str | None = None,
) -> Path | None:
    """Archive a crash to disk, preserving the screenshot and DOM capture.

    Called by :func:`alert_utils.handle_crash` *before* it attempts to send the
    alert email, because that email goes out over Outlook COM — and a broken
    Outlook is itself one of the failures worth hearing about. Writing first
    means the traceback survives even when the notification does not.

    The screenshot and DOM files are **moved** into the archive rather than
    copied, since ``handle_crash`` deletes them immediately afterwards; before
    this existed, the richest debugging artifacts lived only as mail
    attachments.

    Args:
        name (str): Automation logger name.
        traceback_text (str): Full formatted traceback.
        display_name (str | None): Human-readable name for the dashboard.
        tabs (str | None): Open-tab summary captured at crash time.
        screenshot_path (str | None): Temp path of the crash screenshot.
        dom_path (str | None): Temp path of the captured DOM.

    Returns:
        Path | None: The created crash directory, or None on failure. Never
            raises.
    """
    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = crash_root() / name / stamp
        target.mkdir(parents=True, exist_ok=True)

        stored: dict[str, str] = {}
        for source, filename in ((screenshot_path, "screenshot.png"), (dom_path, "dom.txt")):
            if not source:
                continue
            try:
                if os.path.exists(source):
                    shutil.move(source, target / filename)
                    stored[filename.split(".")[0]] = filename
            except OSError:
                log.debug("Could not archive %s", source, exc_info=True)

        _atomic_write_json(target / "crash.json", {
            "name": name,
            "display_name": display_name or name,
            "at": _now_iso(),
            "traceback": traceback_text,
            "tabs": tabs,
            "artifacts": stored,
            "emailed": False,
        })
        return target
    except Exception:
        log.debug("Could not record crash for %s", name, exc_info=True)
        return None


def mark_crash_emailed(crash_dir: Path | None) -> None:
    """Flip a crash record's ``emailed`` flag after the alert email is sent.

    Lets the dashboard distinguish "this crashed and you were told" from "this
    crashed and the notification itself failed" — the second case being the one
    that silently loses incidents today.

    Args:
        crash_dir (Path | None): Directory returned by :func:`record_crash`.
    """
    if crash_dir is None:
        return
    try:
        record_file = crash_dir / "crash.json"
        payload = json.loads(record_file.read_text(encoding="utf-8"))
        payload["emailed"] = True
        _atomic_write_json(record_file, payload)
    except Exception:
        log.debug("Could not mark crash emailed in %s", crash_dir, exc_info=True)


def prune_crashes(retention_days: int = CRASH_RETENTION_DAYS) -> int:
    """Delete crash directories older than ``retention_days``.

    Args:
        retention_days (int): Age past which a crash directory is removed.

    Returns:
        int: How many directories were deleted. Never raises.
    """
    removed = 0
    cutoff = time.time() - (retention_days * 86400)
    try:
        root = crash_root()
        if not root.exists():
            return 0
        for automation_dir in root.iterdir():
            if not automation_dir.is_dir():
                continue
            for crash_dir in automation_dir.iterdir():
                try:
                    if crash_dir.is_dir() and crash_dir.stat().st_mtime < cutoff:
                        shutil.rmtree(crash_dir, ignore_errors=True)
                        removed += 1
                except OSError:
                    continue
    except Exception:
        log.debug("Crash prune failed.", exc_info=True)
    return removed
