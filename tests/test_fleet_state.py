"""Tests for the fleet heartbeat and crash archive.

The whole point of this module is that it must never take an automation down
with it, so alongside the happy paths these check that failures degrade to a
False/None return rather than an exception.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from seller_automation_utils import fleet_state


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    """Point all fleet state at a temp dir for every test in this module."""
    monkeypatch.setenv(fleet_state.STATE_DIR_ENV, str(tmp_path))
    return tmp_path


class _FakeJob:
    def __init__(self, job_id: str, next_run):
        self.id = job_id
        self.next_run_time = next_run


class _FakeScheduler:
    def __init__(self, jobs):
        self._jobs = jobs

    def get_jobs(self):
        return self._jobs


class _BrokenScheduler:
    def get_jobs(self):
        raise RuntimeError("scheduler is gone")


# ── automation_name ────────────────────────────────────────────────────────

@pytest.mark.parametrize("argv0,expected", [
    (r"c:/dev/seller-automations/ebay-best-offers/run_ebay_best_offers.py", "ebay_best_offers"),
    (r"C:\dev\seller-automations\amzn-ca-fba-inventory\run_amzn_ca_fba_inventory.py", "amzn_ca_fba_inventory"),
    ("run_inventory_feed_report.py", "inventory_feed_report"),
    ("something_else.py", "something_else"),
])
def test_automation_name_from_entry_script(argv0, expected, monkeypatch):
    """The fleet's run_<module>.py convention round-trips to the logger name."""
    monkeypatch.setattr("sys.argv", [argv0])
    assert fleet_state.automation_name() == expected


def test_automation_name_explicit_wins(monkeypatch):
    monkeypatch.setattr("sys.argv", ["run_ebay_best_offers.py"])
    assert fleet_state.automation_name("override") == "override"


def test_automation_name_falls_back_to_func_module(monkeypatch):
    """A process not launched from a run_*.py still gets a usable name."""
    monkeypatch.setattr("sys.argv", [""])

    def some_job():
        pass

    some_job.__module__ = "pkg.my_automation"
    assert fleet_state.automation_name(func=some_job) == "my_automation"


# ── heartbeat write / read ─────────────────────────────────────────────────

def test_heartbeat_round_trip():
    ok = fleet_state.write_heartbeat(
        "demo", pid=4242, started="2026-07-31T00:00:00-04:00",
        jobs=[{"id": "j1", "next_run": "2026-07-31T17:30:00-04:00"}],
        last_result={"at": "x", "ok": True, "error": None, "job_id": "j1"},
    )
    assert ok

    data = fleet_state.read_heartbeat("demo")
    assert data["name"] == "demo"
    assert data["pid"] == 4242
    assert data["jobs"][0]["next_run"] == "2026-07-31T17:30:00-04:00"
    assert data["last_result"]["ok"] is True
    assert data["beat"]


def test_read_missing_heartbeat_returns_none():
    assert fleet_state.read_heartbeat("never_existed") is None


def test_read_corrupt_heartbeat_returns_none(state_dir):
    path = fleet_state.heartbeat_path("corrupt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert fleet_state.read_heartbeat("corrupt") is None


def test_clear_heartbeat_removes_file():
    fleet_state.write_heartbeat("demo", pid=1, started="s", jobs=[])
    assert fleet_state.heartbeat_path("demo").exists()

    fleet_state.clear_heartbeat("demo")
    assert not fleet_state.heartbeat_path("demo").exists()

    # Clearing something already gone must not raise.
    fleet_state.clear_heartbeat("demo")


def test_write_heartbeat_never_raises(monkeypatch):
    """A disk failure returns False instead of killing the automation."""
    def boom(*args, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(fleet_state.Path, "mkdir", boom)
    assert fleet_state.write_heartbeat("demo", pid=1, started="s", jobs=[]) is False


# ── HeartbeatWriter ────────────────────────────────────────────────────────

def test_writer_rate_limits_beats():
    """Called every tick, it must only touch disk once per interval."""
    writer = fleet_state.HeartbeatWriter("demo", interval=60.0)
    writer.beat([{"id": "j1", "next_run": None}], force=True)
    first = fleet_state.read_heartbeat("demo")["beat"]

    for _ in range(5):
        writer.beat([{"id": "j1", "next_run": None}])

    assert fleet_state.read_heartbeat("demo")["beat"] == first


def test_writer_beats_again_after_interval():
    writer = fleet_state.HeartbeatWriter("demo", interval=0.01)
    writer.beat([], force=True)
    time.sleep(0.05)
    writer.beat([{"id": "j1", "next_run": "later"}])

    assert fleet_state.read_heartbeat("demo")["jobs"] == [{"id": "j1", "next_run": "later"}]


def test_record_result_keeps_last_known_jobs():
    """Regression: a result flush must not blank the job list.

    record_result fires from APScheduler's listener thread and forces a write.
    If it wrote an empty jobs list it would also reset the rate limiter, so the
    heartbeat would advertise "no scheduled jobs" for a full interval — exactly
    what the dashboard reads as a dead scheduler thread.
    """
    writer = fleet_state.HeartbeatWriter("demo", interval=60.0)
    writer.beat([{"id": "j1", "next_run": "2026-08-01T05:15:00-04:00"}], force=True)

    writer.record_result(ok=False, error="ValueError: nope", job_id="j1")

    data = fleet_state.read_heartbeat("demo")
    assert data["jobs"] == [{"id": "j1", "next_run": "2026-08-01T05:15:00-04:00"}]
    assert data["last_result"]["ok"] is False
    assert data["last_result"]["error"] == "ValueError: nope"


def test_record_result_flushes_immediately():
    """A failure must not wait out the beat interval to become visible."""
    writer = fleet_state.HeartbeatWriter("demo", interval=3600.0)
    writer.beat([], force=True)
    writer.record_result(ok=True, job_id="j1")

    assert fleet_state.read_heartbeat("demo")["last_result"]["ok"] is True


# ── snapshot_jobs ──────────────────────────────────────────────────────────

def test_snapshot_jobs_serializes_next_run():
    when = datetime(2026, 7, 31, 17, 30, tzinfo=timezone.utc)
    jobs = fleet_state.snapshot_jobs(_FakeScheduler([_FakeJob("best_offers", when)]))
    assert jobs == [{"id": "best_offers", "next_run": "2026-07-31T17:30:00+00:00"}]


def test_snapshot_jobs_handles_paused_job():
    """next_run_time is None on a paused job — not an error, but worth seeing."""
    jobs = fleet_state.snapshot_jobs(_FakeScheduler([_FakeJob("paused", None)]))
    assert jobs == [{"id": "paused", "next_run": None}]


def test_snapshot_jobs_survives_broken_scheduler():
    assert fleet_state.snapshot_jobs(_BrokenScheduler()) == []


def test_snapshot_jobs_reports_every_job():
    """inventory-feed-report runs two jobs in one process; both must show."""
    when = datetime(2026, 7, 31, 10, 45, tzinfo=timezone.utc)
    scheduler = _FakeScheduler([_FakeJob("Walmart", when), _FakeJob("Amazon", when)])
    assert [j["id"] for j in fleet_state.snapshot_jobs(scheduler)] == ["Walmart", "Amazon"]


# ── crash archive ──────────────────────────────────────────────────────────

def test_record_crash_moves_artifacts(tmp_path):
    shot = tmp_path / "temp_crash.png"
    shot.write_bytes(b"PNG-ISH")
    dom = tmp_path / "temp_dom.txt"
    dom.write_text("<html>broken</html>", encoding="utf-8")

    crash_dir = fleet_state.record_crash(
        "ebay_best_offers",
        traceback_text="Traceback...\nValueError: boom",
        display_name="eBay Best Offers",
        tabs="1 tab open",
        screenshot_path=str(shot),
        dom_path=str(dom),
    )

    assert crash_dir is not None
    assert (crash_dir / "screenshot.png").read_bytes() == b"PNG-ISH"
    assert (crash_dir / "dom.txt").read_text(encoding="utf-8") == "<html>broken</html>"
    # Moved, not copied — handle_crash used to delete these right after emailing.
    assert not shot.exists()
    assert not dom.exists()

    record = json.loads((crash_dir / "crash.json").read_text(encoding="utf-8"))
    assert record["name"] == "ebay_best_offers"
    assert record["display_name"] == "eBay Best Offers"
    assert "ValueError: boom" in record["traceback"]
    assert record["emailed"] is False
    assert record["artifacts"] == {"screenshot": "screenshot.png", "dom": "dom.txt"}


def test_record_crash_without_artifacts():
    """Chrome never launched: no screenshot, no DOM, still a full record."""
    crash_dir = fleet_state.record_crash("demo", traceback_text="boom")

    assert crash_dir is not None
    record = json.loads((crash_dir / "crash.json").read_text(encoding="utf-8"))
    assert record["artifacts"] == {}
    assert record["traceback"] == "boom"


def test_mark_crash_emailed_flips_flag():
    crash_dir = fleet_state.record_crash("demo", traceback_text="boom")
    fleet_state.mark_crash_emailed(crash_dir)

    record = json.loads((crash_dir / "crash.json").read_text(encoding="utf-8"))
    assert record["emailed"] is True
    assert record["traceback"] == "boom"


def test_mark_crash_emailed_tolerates_none():
    """record_crash returning None must not break the email path."""
    fleet_state.mark_crash_emailed(None)


def test_prune_crashes_removes_only_old(state_dir):
    fresh = fleet_state.record_crash("demo", traceback_text="new")
    stale = fleet_state.crash_root() / "demo" / "20260101-000000"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "crash.json").write_text("{}", encoding="utf-8")
    old = (datetime.now() - timedelta(days=45)).timestamp()
    import os
    os.utime(stale, (old, old))

    assert fleet_state.prune_crashes(retention_days=30) == 1
    assert not stale.exists()
    assert fresh.exists()


def test_prune_crashes_on_empty_archive():
    assert fleet_state.prune_crashes() == 0
