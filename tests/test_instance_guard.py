"""Tests for the single-instance guard.

An orphaned scheduler kept firing beside a fresh copy of the same automation,
and both drove Chrome on one profile. The guard must stop the second copy
before any work, must never stop a *different* automation, and must never
report the refusal as a crash. Most of these tests assert a refusal.

Every mutex name here is fabricated and carries a random suffix, because the
``Global\\`` namespace is shared by every session on the machine.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import textwrap
import threading
import uuid
from pathlib import Path

import pytest

from seller_automation_utils import instance_guard, schedule_utils, ui_utils

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="named mutexes are Windows-only")


@pytest.fixture
def fresh(monkeypatch, tmp_path):
    """Isolate module state, the fleet state dir, argv and atexit for one test."""
    monkeypatch.setattr(instance_guard, "_held", {})
    monkeypatch.setattr(instance_guard, "_warned", set())
    monkeypatch.setenv("FC_FLEET_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv(instance_guard.ALLOW_MULTIPLE_ENV, raising=False)
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "run_fab_demo.py")])
    registered: list[tuple] = []
    monkeypatch.setattr(instance_guard.atexit, "register", lambda *a: registered.append(a))
    return registered


class FakeMutex:
    """Stands in for ``_create_mutex``: returns a scripted state and counts calls."""

    def __init__(self, state: str) -> None:
        self.state = state
        self.names: list[str] = []

    def __call__(self, name: str):
        self.names.append(name)
        return (object() if self.state == "acquired" else None), self.state


# --- identity ---------------------------------------------------------------

@pytest.mark.parametrize(
    ("explicit", "argv0", "expected"),
    [
        (None, r"C:\fab\repo\run_fab_demo.py", "fab_demo"),
        (None, "run_fab_demo.py", "fab_demo"),
        (None, "RUN_Fab_Demo.py", "fab_demo"),
        (None, "/fab/repo/run_fab_demo.py", "fab_demo"),
        ("Fab_Demo", "run_other.py", "fab_demo"),
        ("fab demo/x", None, "fab_demo_x"),
        ("   ", "run_fab_demo.py", "fab_demo"),
        ("", "run_fab_demo.py", "fab_demo"),
    ],
)
def test_identity_from_explicit_name_or_run_script(explicit, argv0, expected):
    assert instance_guard.instance_identity(explicit, argv0) == expected


@pytest.mark.parametrize(
    "argv0",
    [
        None,
        "",
        "-c",
        "-m",
        r"C:\fab\.venv\Lib\site-packages\pytest\__main__.py",
        r"C:\fab\.venv\Scripts\pytest.exe",
        "main.py",
        "tool_run_fab.py",
        "run_.py",
        "run.py",
    ],
)
def test_no_identity_means_no_lock(argv0):
    """Two unrelated scripts must never share a fallback name and block each other."""
    assert instance_guard.instance_identity(None, argv0) is None


def test_different_automations_get_different_mutexes():
    a = instance_guard.mutex_name(instance_guard.instance_identity(None, "run_fab_alpha.py"))
    b = instance_guard.mutex_name(instance_guard.instance_identity(None, "run_fab_beta.py"))
    assert a != b


@pytest.mark.parametrize(
    ("identity", "expected_suffix"),
    [
        ("fab_demo", "fab_demo"),
        ("Fab_Demo", "fab_demo"),
        ("fab\\demo", "fab_demo"),
        ("fab demo", "fab_demo"),
    ],
)
def test_mutex_name_is_global_and_has_one_backslash(identity, expected_suffix):
    name = instance_guard.mutex_name(identity)
    assert name == f"Global\\seller_automation_utils.single_instance.{expected_suffix}"
    assert name.count("\\") == 1


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_mutex_name_refuses_blank(blank):
    with pytest.raises(ValueError):
        instance_guard.mutex_name(blank)


# --- the blocked message ----------------------------------------------------

def test_blocked_message_names_automation_and_pid():
    msg = instance_guard.blocked_message("fab_demo", {"pid": 4321, "started": "2026-01-02T03:04:05"})
    assert "'fab_demo'" in msg
    assert "PID 4321" in msg
    assert "Stop-Process -Id 4321" in msg
    assert "security context" not in msg


@pytest.mark.parametrize("holder", [None, {}, {"pid": "4321"}, {"pid": True}, ["not", "a", "dict"]])
def test_blocked_message_without_a_usable_pid(holder):
    msg = instance_guard.blocked_message("fab_demo", holder)
    assert "holder PID unknown" in msg
    assert "never by name" in msg


def test_blocked_message_access_denied():
    msg = instance_guard.blocked_message("fab_demo", None, access_denied=True)
    assert "security context" in msg


def test_blocked_message_escapes_markup_from_the_record():
    msg = instance_guard.blocked_message("fab_demo", {"pid": 1, "started": "[red]x[/red]"})
    assert "\\[red]" in msg


# --- acquire / refuse (fake mutex) ------------------------------------------

def test_acquire_records_holder_and_registers_cleanup(fresh, monkeypatch):
    fake = FakeMutex("acquired")
    monkeypatch.setattr(instance_guard, "_create_mutex", fake)

    instance_guard.ensure_single_instance()

    assert fake.names == ["Global\\seller_automation_utils.single_instance.fab_demo"]
    assert "fab_demo" in instance_guard._held
    record = json.loads(instance_guard.lock_info_path("fab_demo").read_text(encoding="utf-8"))
    assert record["pid"] == os.getpid()
    assert record["name"] == "fab_demo"
    assert record["process_created"] == instance_guard._process_creation_time(os.getpid())
    assert len(fresh) == 1


def test_second_call_in_same_process_is_a_no_op(fresh, monkeypatch):
    """Otherwise ask_user then run_on_schedule would see its own mutex and exit."""
    fake = FakeMutex("acquired")
    monkeypatch.setattr(instance_guard, "_create_mutex", fake)

    instance_guard.ensure_single_instance()
    instance_guard.ensure_single_instance()
    instance_guard.ensure_single_instance(None)

    assert len(fake.names) == 1


@pytest.mark.parametrize("state", ["held", "denied"])
def test_blocked_copy_exits_zero_and_touches_nothing(fresh, monkeypatch, caplog, state):
    monkeypatch.setattr(instance_guard, "_create_mutex", FakeMutex(state))
    from seller_automation_utils import alert_utils, fleet_state, outlook

    def forbidden(*a, **k):
        raise AssertionError("a blocked copy must not alert, mail or beat")

    monkeypatch.setattr(alert_utils, "handle_crash", forbidden)
    monkeypatch.setattr(outlook, "send_email", forbidden)
    monkeypatch.setattr(fleet_state, "write_heartbeat", forbidden)
    monkeypatch.setattr(fleet_state, "clear_heartbeat", forbidden)

    monkeypatch.setattr(instance_guard, "_process_creation_time", lambda pid: 1000 if pid == 4321 else None)

    record = instance_guard.lock_info_path("fab_demo")
    record.parent.mkdir(parents=True)
    original = json.dumps(
        {"name": "fab_demo", "pid": 4321, "started": "2026-01-02T03:04:05", "process_created": 1000}
    )
    record.write_text(original, encoding="utf-8")
    state_root = fleet_state.state_root()
    before = sorted(p.relative_to(state_root) for p in state_root.rglob("*"))

    with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc:
        instance_guard.ensure_single_instance()

    assert exc.value.code == 0
    assert instance_guard._held == {}
    assert record.read_text(encoding="utf-8") == original
    assert sorted(p.relative_to(state_root) for p in state_root.rglob("*")) == before
    assert fresh == []
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert "'fab_demo'" in errors[0].getMessage()
    assert "PID 4321" in errors[0].getMessage()


def test_opt_out_skips_the_mutex_and_warns_once(fresh, monkeypatch, caplog):
    fake = FakeMutex("held")
    monkeypatch.setattr(instance_guard, "_create_mutex", fake)
    monkeypatch.setenv(instance_guard.ALLOW_MULTIPLE_ENV, "1")

    with caplog.at_level("WARNING"):
        instance_guard.ensure_single_instance()
        instance_guard.ensure_single_instance()

    assert fake.names == []
    warnings = [r for r in caplog.records if instance_guard.ALLOW_MULTIPLE_ENV in r.getMessage()]
    assert len(warnings) == 1


def test_empty_opt_out_value_does_not_disable(fresh, monkeypatch):
    """An empty variable is unset, not an opt-in."""
    fake = FakeMutex("held")
    monkeypatch.setattr(instance_guard, "_create_mutex", fake)
    monkeypatch.setenv(instance_guard.ALLOW_MULTIPLE_ENV, "")

    with pytest.raises(SystemExit):
        instance_guard.ensure_single_instance()
    assert len(fake.names) == 1


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", "2", " ", "y", "on"])
def test_opt_out_values_that_do_not_say_yes_keep_the_guard_on(fresh, monkeypatch, value):
    """``FC_ALLOW_MULTIPLE_INSTANCES=0`` must not switch the guard off."""
    fake = FakeMutex("held")
    monkeypatch.setattr(instance_guard, "_create_mutex", fake)
    monkeypatch.setenv(instance_guard.ALLOW_MULTIPLE_ENV, value)

    with pytest.raises(SystemExit):
        instance_guard.ensure_single_instance()
    assert len(fake.names) == 1


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " Yes ", "\ttrue\n"])
def test_opt_out_accepts_one_true_yes(fresh, monkeypatch, value):
    fake = FakeMutex("held")
    monkeypatch.setattr(instance_guard, "_create_mutex", fake)
    monkeypatch.setenv(instance_guard.ALLOW_MULTIPLE_ENV, value)

    instance_guard.ensure_single_instance()
    assert fake.names == []


def test_no_identity_skips_the_mutex_and_warns(fresh, monkeypatch, caplog):
    fake = FakeMutex("held")
    monkeypatch.setattr(instance_guard, "_create_mutex", fake)
    monkeypatch.setattr(sys, "argv", ["-c"])

    with caplog.at_level("WARNING"):
        instance_guard.ensure_single_instance()

    assert fake.names == []
    assert any("not applied" in r.getMessage() for r in caplog.records)


def test_fc_no_prompt_does_not_disable_the_guard(fresh, monkeypatch):
    fake = FakeMutex("held")
    monkeypatch.setattr(instance_guard, "_create_mutex", fake)
    monkeypatch.setenv(ui_utils.NO_PROMPT_ENV, "1")

    with pytest.raises(SystemExit):
        instance_guard.ensure_single_instance()


_VALID = {"name": "fab_demo", "pid": 4321, "started": "2026-01-02T03:04:05", "process_created": 1000}


@pytest.mark.parametrize(
    ("record_text", "live_created", "names_pid"),
    [
        (json.dumps(_VALID), 1000, True),
        (json.dumps(_VALID), 2000, False),
        (json.dumps(_VALID), None, False),
        (json.dumps({k: v for k, v in _VALID.items() if k != "process_created"}), 1000, False),
        (json.dumps({**_VALID, "process_created": "1000"}), 1000, False),
        ("{not json", 1000, False),
        (None, 1000, False),
    ],
    ids=["matching", "pid-reused", "pid-dead", "no-creation-time", "creation-time-not-int", "unreadable", "missing"],
)
def test_blocked_copy_names_the_pid_only_for_the_live_original_holder(
    fresh, monkeypatch, caplog, record_text, live_created, names_pid
):
    """A stale record's PID may now belong to an unrelated process; never tell anyone to stop it."""
    monkeypatch.setattr(instance_guard, "_create_mutex", FakeMutex("held"))
    monkeypatch.setattr(instance_guard, "_process_creation_time", lambda pid: live_created)
    if record_text is not None:
        record = instance_guard.lock_info_path("fab_demo")
        record.parent.mkdir(parents=True)
        record.write_text(record_text, encoding="utf-8")

    with caplog.at_level("ERROR"), pytest.raises(SystemExit):
        instance_guard.ensure_single_instance()

    message = next(r.getMessage() for r in caplog.records if r.levelname == "ERROR")
    if names_pid:
        assert "PID 4321" in message
        assert "Stop-Process -Id 4321" in message
    else:
        assert "4321" not in message
        assert "holder PID unknown" in message
        assert "never by name" in message


@WINDOWS_ONLY
def test_live_holder_checks_a_real_process_creation_time():
    pid = os.getpid()
    created = instance_guard._process_creation_time(pid)
    assert isinstance(created, int)
    assert instance_guard._process_creation_time(pid) == created

    record = {"pid": pid, "process_created": created}
    assert instance_guard._live_holder(record) is record
    assert instance_guard._live_holder({"pid": pid, "process_created": created - 1}) is None


@WINDOWS_ONLY
def test_process_creation_time_is_none_for_an_exited_process():
    # The Popen object keeps its process handle open, so the PID cannot be
    # reused by another process while this test inspects it.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        proc.wait(timeout=60)
        assert instance_guard._process_creation_time(proc.pid) is None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=60)


def test_remove_lock_info_only_removes_its_own_record(tmp_path):
    record = tmp_path / "fab.json"
    record.write_text(json.dumps({"pid": 111}), encoding="utf-8")

    instance_guard._remove_lock_info(record, 222)
    assert record.exists()

    instance_guard._remove_lock_info(record, 111)
    assert not record.exists()


# --- the two entry points ---------------------------------------------------

def _blocked_guard(calls: list[str]):
    def guard(name=None):
        calls.append("guard")
        raise SystemExit(0)
    return guard


@pytest.mark.parametrize("no_prompt", ["", "1"])
def test_ask_user_takes_the_lock_before_prompting(monkeypatch, no_prompt):
    calls: list[str] = []
    monkeypatch.setattr(instance_guard, "ensure_single_instance", _blocked_guard(calls))
    monkeypatch.setenv(ui_utils.NO_PROMPT_ENV, no_prompt)

    def message_box(*a, **k):
        calls.append("dialog")
        return 6

    user32 = type("_User32", (), {"MessageBoxW": staticmethod(message_box)})()
    monkeypatch.setattr(ui_utils.ctypes, "windll", type("_W", (), {"user32": user32})(), raising=False)

    with pytest.raises(SystemExit) as exc:
        ui_utils.ask_user("Run now?", "Demo")

    assert exc.value.code == 0
    assert calls == ["guard"]


def test_run_on_schedule_takes_the_lock_before_any_scheduler_state(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(instance_guard, "ensure_single_instance", _blocked_guard(calls))

    def forbidden(*a, **k):
        calls.append("touched")
        raise AssertionError("scheduler state touched by a blocked copy")

    monkeypatch.setattr(schedule_utils, "BackgroundScheduler", forbidden)
    monkeypatch.setattr(schedule_utils.fleet_state, "HeartbeatWriter", forbidden)
    monkeypatch.setattr(schedule_utils.fleet_state, "prune_crashes", forbidden)
    monkeypatch.setattr(schedule_utils.fleet_state, "clear_heartbeat", forbidden)

    with pytest.raises(SystemExit) as exc:
        schedule_utils.run_on_schedule(lambda: None, hour=1, minute=0)

    assert exc.value.code == 0
    assert calls == ["guard"]


def test_run_on_schedule_passes_its_name_to_the_guard(monkeypatch):
    seen: list = []

    def guard(name=None):
        seen.append(name)
        raise SystemExit(0)

    monkeypatch.setattr(instance_guard, "ensure_single_instance", guard)
    with pytest.raises(SystemExit):
        schedule_utils.run_on_schedule(lambda: None, hour=1, minute=0, name="fab_named")
    assert seen == ["fab_named"]


# --- real mutexes ------------------------------------------------------------

@WINDOWS_ONLY
def test_create_mutex_maps_access_denied_and_reraises_the_rest(monkeypatch):
    import pywintypes
    import win32event

    def denied(*a):
        raise pywintypes.error(5, "CreateMutex", "Access is denied.")

    monkeypatch.setattr(win32event, "CreateMutex", denied)
    assert instance_guard._create_mutex("Global\\fab") == (None, "denied")

    def other(*a):
        raise pywintypes.error(87, "CreateMutex", "The parameter is incorrect.")

    monkeypatch.setattr(win32event, "CreateMutex", other)
    with pytest.raises(pywintypes.error):
        instance_guard._create_mutex("Global\\fab")


@WINDOWS_ONLY
def test_handle_survives_gc_and_is_the_lock(fresh):
    import gc

    import pywintypes
    import win32event

    identity = f"fab_gc_{uuid.uuid4().hex[:10]}"
    name = instance_guard.mutex_name(identity)
    synchronize = 0x00100000

    instance_guard.ensure_single_instance(identity)
    gc.collect()

    probe = win32event.OpenMutex(synchronize, False, name)
    probe.Close()

    instance_guard._held.pop(identity).Close()
    with pytest.raises(pywintypes.error):
        win32event.OpenMutex(synchronize, False, name)


CHILD = textwrap.dedent(
    """
    import gc, logging, os, subprocess, sys
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    from seller_automation_utils import ensure_single_instance
    mode = sys.argv[1]
    ensure_single_instance()
    gc.collect()
    print(f"ACQUIRED {os.getpid()}", flush=True)
    if mode == "hold":
        sys.stdin.readline()
    elif mode == "spawn_then_exit":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], close_fds=False
        )
        print(f"GRANDCHILD {child.pid}", flush=True)
    """
)


@pytest.fixture
def child_env(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != instance_guard.ALLOW_MULTIPLE_ENV}
    env["FC_FLEET_STATE_DIR"] = str(tmp_path / "state")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(SRC_DIR), env.get("PYTHONPATH")]))
    env.pop(ui_utils.NO_PROMPT_ENV, None)
    return env


def _script(tmp_path: Path, stem: str) -> Path:
    path = tmp_path / f"run_{stem}.py"
    path.write_text(CHILD, encoding="utf-8")
    return path


def _run(script: Path, mode: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), mode],
        env=env, capture_output=True, text=True, timeout=60,
    )


class Holder:
    """A child process that takes the lock and holds it until told to stop."""

    def __init__(self, script: Path, env: dict[str, str], mode: str = "hold") -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(script), mode],
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line.strip())

    def expect(self, prefix: str, timeout: float = 60) -> str:
        while True:
            line = self.lines.get(timeout=timeout)
            if line.startswith(prefix):
                return line

    def release(self) -> int:
        self.proc.stdin.close()
        return self.proc.wait(timeout=60)

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=60)


@WINDOWS_ONLY
def test_second_process_is_blocked_then_runs_once_the_first_exits(tmp_path, child_env):
    script = _script(tmp_path, f"fab_{uuid.uuid4().hex[:10]}")
    holder = Holder(script, child_env)
    try:
        # The PID that holds the lock is the interpreter's own, which in a
        # Windows venv is a child of the Scripts\python.exe launcher Popen sees.
        holder_pid = int(holder.expect("ACQUIRED").split()[1])

        blocked = _run(script, "check", child_env)
        assert blocked.returncode == 0
        assert "ACQUIRED" not in blocked.stdout
        assert "already running" in blocked.stderr
        assert f"PID {holder_pid}" in blocked.stderr
        assert not (tmp_path / "state" / "heartbeats").exists()

        assert holder.release() == 0
    finally:
        holder.kill()

    identity = script.stem[len("run_"):]
    assert not (tmp_path / "state" / "locks" / f"{identity}.json").exists()
    after = _run(script, "check", child_env)
    assert after.returncode == 0
    assert "ACQUIRED" in after.stdout


@WINDOWS_ONLY
def test_different_automations_do_not_block_each_other(tmp_path, child_env):
    token = uuid.uuid4().hex[:10]
    holder = Holder(_script(tmp_path, f"fab_alpha_{token}"), child_env)
    try:
        holder.expect("ACQUIRED")
        other = _run(_script(tmp_path, f"fab_beta_{token}"), "check", child_env)
        assert other.returncode == 0
        assert "ACQUIRED" in other.stdout
    finally:
        holder.kill()


@WINDOWS_ONLY
def test_killed_holder_leaves_no_stale_lock(tmp_path, child_env):
    script = _script(tmp_path, f"fab_{uuid.uuid4().hex[:10]}")
    holder = Holder(script, child_env)
    holder.expect("ACQUIRED")
    holder.kill()

    after = _run(script, "check", child_env)
    assert after.returncode == 0
    assert "ACQUIRED" in after.stdout


def _alive(pid: int) -> bool:
    """True while ``pid`` is a running process (Windows)."""
    import pywintypes
    import win32api
    import win32process

    try:
        handle = win32api.OpenProcess(0x1000, False, pid)
    except pywintypes.error:
        return False
    try:
        return win32process.GetExitCodeProcess(handle) == 259
    finally:
        handle.Close()


@WINDOWS_ONLY
def test_orphaned_child_process_does_not_keep_the_lock(tmp_path, child_env):
    """A Chrome or driver outliving its Python parent must not block the next start."""
    script = _script(tmp_path, f"fab_{uuid.uuid4().hex[:10]}")
    holder = Holder(script, child_env, mode="spawn_then_exit")
    grandchild_pid = None
    try:
        holder.expect("ACQUIRED")
        grandchild_pid = int(holder.expect("GRANDCHILD").split()[1])
        holder.proc.wait(timeout=60)
        assert _alive(grandchild_pid), "grandchild died with its parent; the test proves nothing"

        after = _run(script, "check", child_env)
        assert after.returncode == 0
        assert "ACQUIRED" in after.stdout
    finally:
        holder.kill()
        if grandchild_pid is not None:
            try:
                os.kill(grandchild_pid, 9)
            except OSError:
                pass


@WINDOWS_ONLY
def test_import_takes_no_lock(tmp_path, child_env):
    """Importing the package, even from a run_*.py script, must not lock anything."""
    stem = f"fab_{uuid.uuid4().hex[:10]}"
    script = tmp_path / f"run_{stem}.py"
    script.write_text(textwrap.dedent(
        """
        import pywintypes, win32event
        import seller_automation_utils
        from seller_automation_utils import instance_guard, schedule_utils, ui_utils
        assert instance_guard._held == {}, instance_guard._held
        name = instance_guard.mutex_name(instance_guard.instance_identity(None, __file__))
        try:
            win32event.OpenMutex(0x00100000, False, name)
        except pywintypes.error:
            print("CLEAN")
        """
    ), encoding="utf-8")

    result = _run(script, "check", child_env)
    assert result.returncode == 0, result.stderr
    assert "CLEAN" in result.stdout
    assert not (tmp_path / "state" / "locks").exists()
