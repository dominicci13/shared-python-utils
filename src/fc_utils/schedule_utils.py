from __future__ import annotations

from collections.abc import Callable

from apscheduler.schedulers.blocking import BlockingScheduler
from rich import print


def run_on_schedule(
    func: Callable,
    hour: int,
    minute: int,
    day_of_week: str | None = None,
) -> None:
    """Run a function on a recurring cron schedule using APScheduler.

    Blocks the calling thread indefinitely, firing `func` at the specified
    time each day (or on the specified days of the week). Logs the next
    scheduled run time after each execution.

    Args:
        func (Callable): The function to call on each scheduled trigger.
        hour (int): Hour of day to run (0–23, local time).
        minute (int): Minute of hour to run (0–59).
        day_of_week (str | None): APScheduler day-of-week expression (e.g., "mon-fri",
            "mon,wed,fri"). Pass None to run every day. Defaults to None.

    Raises:
        ValueError: If hour or minute are out of valid range.
    """
    scheduler = BlockingScheduler()
    cron_kwargs: dict = {"hour": hour, "minute": minute}
    if day_of_week is not None:
        cron_kwargs["day_of_week"] = day_of_week

    scheduler.add_job(func, "cron", **cron_kwargs)
    print(f"[cyan][INFO][/cyan] Scheduler started.")
    scheduler.start()
