from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.schedulers.background import BackgroundScheduler
import logging

from seller_automation_utils import fleet_state
from seller_automation_utils.fleet_state import automation_name

log = logging.getLogger(__name__)


def run_on_schedule(
    func: Callable,
    hour: int,
    minute: int,
    day_of_week: str | None = None,
    name: str | None = None,
) -> None:
    """Run a function on a recurring cron schedule using APScheduler.

    Starts a ``BackgroundScheduler`` and blocks the main thread with a
    1-second ``time.sleep`` loop. ``time.sleep`` on Windows is interruptible
    by Ctrl+C, so ``KeyboardInterrupt`` propagates immediately and the
    scheduler is shut down cleanly. Using ``BlockingScheduler`` here instead
    swallows Ctrl+C on Windows: its internal C-level ``threading.Event.wait``
    blocks Python signal delivery until the wait returns on its own —
    sometimes hours later.

    The main-thread loop uses near-zero CPU (the kernel parks the thread
    between ticks) — the 1-second tick only sets the Ctrl+C response
    latency. The scheduler's own daemon thread blocks efficiently until the
    next job fire time.

    The job is added with ``misfire_grace_time=30``. APScheduler's default
    grace is 1 second: if normal scheduler jitter delays the fire past that
    1-second window (commonly ~1–2 seconds on Windows), the run is flagged
    "missed" and silently skipped. A 30-second window absorbs that jitter so
    scheduled runs actually execute, while still being short enough that a
    badly delayed fire (e.g. recovery after a reboot) waits for its next
    clean slot instead of colliding with other automations. ``coalesce=True``
    keeps a recovered backlog to a single run rather than replaying each
    missed fire.

    Every tick also emits a heartbeat to
    :mod:`seller_automation_utils.fleet_state`, which is what lets the
    ``fleet-control`` dashboard see this process at all. The heartbeat carries
    each job's live ``next_run_time``, so a scheduler thread that has died
    inside a still-running process is externally visible — the one failure mode
    the ``[CRASH]`` email can never report.

    Args:
        func (Callable): The function to call on each scheduled trigger.
        hour (int): Hour of day to run (0–23, local time).
        minute (int): Minute of hour to run (0–59).
        day_of_week (str | None): APScheduler day-of-week expression
            (e.g., "mon-fri", "mon,wed,fri"). Pass None to run every day.
            Defaults to None.
        name (str | None): Automation name for the heartbeat file. Defaults to
            the name derived from the entry script.

    Raises:
        ValueError: If hour or minute are out of valid range.
    """
    scheduler = BackgroundScheduler()
    cron_kwargs: dict = {"hour": hour, "minute": minute}
    if day_of_week is not None:
        cron_kwargs["day_of_week"] = day_of_week

    job = scheduler.add_job(
        func, "cron", misfire_grace_time=30, coalesce=True, **cron_kwargs
    )
    now = datetime.now(timezone.utc)
    next_run = job.trigger.get_next_fire_time(None, now)
    if next_run is not None:
        local_next = next_run.astimezone()
        day_name = local_next.strftime("%A")
        formatted = local_next.strftime("%Y-%m-%d %H:%M:%S")
        log.info(f"Scheduler started. Next run: [cyan]{day_name}, {formatted}[/cyan]")
    else:
        log.info("Scheduler started.")

    heartbeat = fleet_state.HeartbeatWriter(automation_name(name, func))
    fleet_state.prune_crashes()

    def _listener(event):
        j = scheduler.get_job(event.job_id)
        if event.exception:
            log.error(f"Scheduled job failed: {event.exception}")
        heartbeat.record_result(
            ok=not event.exception,
            error=f"{type(event.exception).__name__}: {event.exception}" if event.exception else None,
            job_id=str(event.job_id),
        )
        if j and j.next_run_time:
            day_name = j.next_run_time.strftime("%A")
            formatted = j.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
            log.info(f"Next run: [cyan]{day_name}, {formatted}[/cyan]")

    scheduler.add_listener(_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    scheduler.start()
    heartbeat.beat(fleet_state.snapshot_jobs(scheduler), force=True)

    try:
        while True:
            heartbeat.beat(fleet_state.snapshot_jobs(scheduler))
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        log.info("\n[yellow][WARNING][/yellow] Ctrl+C received, stopping scheduler.")
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        # Leave no heartbeat behind: a stale file would read as a live process
        # to the dashboard until its beat aged out.
        fleet_state.clear_heartbeat(heartbeat.name)
        log.info("Scheduler stopped.")
