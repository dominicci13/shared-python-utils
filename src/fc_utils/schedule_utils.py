from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.schedulers.background import BackgroundScheduler
from rich import print


def run_on_schedule(
    func: Callable,
    hour: int,
    minute: int,
    day_of_week: str | None = None,
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

    Args:
        func (Callable): The function to call on each scheduled trigger.
        hour (int): Hour of day to run (0–23, local time).
        minute (int): Minute of hour to run (0–59).
        day_of_week (str | None): APScheduler day-of-week expression
            (e.g., "mon-fri", "mon,wed,fri"). Pass None to run every day.
            Defaults to None.

    Raises:
        ValueError: If hour or minute are out of valid range.
    """
    scheduler = BackgroundScheduler()
    cron_kwargs: dict = {"hour": hour, "minute": minute}
    if day_of_week is not None:
        cron_kwargs["day_of_week"] = day_of_week

    job = scheduler.add_job(func, "cron", **cron_kwargs)
    now = datetime.now(timezone.utc)
    next_run = job.trigger.get_next_fire_time(None, now)
    if next_run is not None:
        local_next = next_run.astimezone()
        day_name = local_next.strftime("%A")
        formatted = local_next.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[cyan][INFO][/cyan] Scheduler started. Next run: [cyan]{day_name}, {formatted}[/cyan]")
    else:
        print("[cyan][INFO][/cyan] Scheduler started.")

    def _listener(event):
        j = scheduler.get_job(event.job_id)
        if event.exception:
            print(f"[bold red][ERROR][/bold red] Scheduled job failed: {event.exception}")
        if j and j.next_run_time:
            day_name = j.next_run_time.strftime("%A")
            formatted = j.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"[cyan][INFO][/cyan] Next run: [cyan]{day_name}, {formatted}[/cyan]")

    scheduler.add_listener(_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    scheduler.start()

    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        print("\n[yellow][WARNING][/yellow] Ctrl+C received, stopping scheduler.")
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        print("[cyan][INFO][/cyan] Scheduler stopped.")
