"""Tests for the AI summary that `handle_crash` puts above the traceback (Crash Explainer M2, step 3).

codex is never run: `crash_explainer.build_prompt` and `crash_explainer.explain` are replaced in
every test, and an autouse fixture makes any unpatched call to the runner fail the test. The crash
archive is the real `fleet_state` one, pointed at `tmp_path`, so `crash.json` is checked as written.
"""
from __future__ import annotations

import html
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from seller_automation_utils import alert_utils, crash_explainer, crash_notes, fleet_state

NAME = "demo_report"
DISPLAY = "Demo Report"
STAMP = "2026-01-02 03:04:05"

TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "C:\\repos\\demo-report\\run_demo_report.py", line 90, in <module>\n'
    "    main()\n"
    '  File "C:\\repos\\demo-report\\run_demo_report.py", line 42, in main\n'
    "    download_report(driver)\n"
    '  File "C:\\repos\\demo-report\\.venv\\Lib\\site-packages\\seller_automation_utils\\ui_utils.py",'
    " line 10, in wait_for\n"
    '    raise TimeoutError("no element")\n'
    "TimeoutError: no element\n"
)

SUMMARY: dict[str, Any] = {
    "what_happened": "The report download never started, so the run stopped before saving anything.",
    "why": "The portal showed a sign-in page instead of the reports page.",
    "how_to_fix": "Sign in once in the automation's browser profile.\nThen rerun.",
    "technical_fix": {
        "file": "run_demo_report.py",
        "line": 42,
        "change": "Check for the sign-in page after loading the reports page and raise a clear error.",
    },
    "safe_to_rerun_reason": "Nothing was written or emailed before the failure.",
    "safe_to_rerun": "yes_after_check",
    "confidence": "medium",
}


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206 - mirrors datetime.now
        return datetime(2026, 1, 2, 3, 4, 5)


class Harness:
    """Records every side effect of one handle_crash call, in order."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.sent: list[dict[str, Any]] = []
        self.prompt_kwargs: dict[str, Any] = {}
        self.screenshot: Path | None = None
        self.record_at_send: dict[str, Any] | None = None
        self.crash_dir: Path | None = None

    @property
    def body(self) -> str:
        assert len(self.sent) == 1
        return self.sent[0]["body"]

    def record(self) -> dict[str, Any]:
        assert self.crash_dir is not None
        return json.loads((self.crash_dir / "crash.json").read_text(encoding="utf-8"))


@pytest.fixture
def harness(tmp_path, monkeypatch) -> Harness:
    h = Harness()
    monkeypatch.setenv(fleet_state.STATE_DIR_ENV, str(tmp_path / "state"))
    # CRASH_AI=0 now skips the whole AI step; _never_run_codex below still keeps the real runner out.
    monkeypatch.delenv("CRASH_AI", raising=False)
    monkeypatch.delenv("CRASH_AI_TIMEOUT", raising=False)
    monkeypatch.setattr(alert_utils, "datetime", _FrozenDateTime)
    monkeypatch.setattr(alert_utils, "get_env", lambda *a, **k: "alerts@example.com")
    monkeypatch.setattr(fleet_state, "automation_name", lambda *a, **k: NAME)

    log_file = tmp_path / "demo.log"
    log_file.write_text("2026-01-02 03:04:01 INFO Opening the [cyan]reports[/cyan] page.\n", encoding="utf-8")
    monkeypatch.setattr(crash_explainer, "active_log_file", lambda: log_file)
    monkeypatch.setattr(crash_explainer, "default_kb_dir", lambda: None)
    monkeypatch.setattr(crash_explainer, "_find_codex", lambda: "codex.exe")

    real_record = fleet_state.record_crash

    def record_crash(*args, **kwargs):
        h.events.append("record")
        h.crash_dir = real_record(*args, **kwargs)
        return h.crash_dir

    def build_prompt(crash_dir, **kwargs):
        h.events.append("build_prompt")
        h.prompt_kwargs = {"crash_dir": crash_dir, **kwargs}
        return "PROMPT"

    def send_email(**kwargs):
        h.events.append("send")
        h.record_at_send = h.record() if h.crash_dir is not None else None
        h.sent.append(kwargs)

    monkeypatch.setattr(fleet_state, "record_crash", record_crash)
    monkeypatch.setattr(alert_utils, "_kill_own_processes", lambda: h.events.append("kill"))
    monkeypatch.setattr(crash_explainer, "build_prompt", build_prompt)
    monkeypatch.setattr(alert_utils.outlook, "send_email", send_email)
    h.set_answer = lambda summary, reason: monkeypatch.setattr(  # type: ignore[attr-defined]
        crash_explainer, "explain", _explain(h, summary, reason)
    )
    h.set_answer(SUMMARY, None)
    return h


def _explain(h: Harness, summary: dict[str, Any] | None, reason: str | None):
    def explain(prompt: str, screenshot: Path | None = None):
        h.events.append("explain")
        assert prompt == "PROMPT"
        h.screenshot = screenshot
        return summary, reason
    return explain


@pytest.fixture(autouse=True)
def _never_run_codex(monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("a test reached the real codex runner")
    monkeypatch.setattr(crash_explainer, "_run_codex", forbidden)


class FakeSwitchTo:
    def window(self, handle: str) -> None:
        pass

    def default_content(self) -> None:
        pass

    def parent_frame(self) -> None:
        pass


class FakeDriver:
    """Just enough of a WebDriver for the screenshot, DOM and tab capture."""

    current_url = "https://portal.example.test/reports?a=1&b=<2>"
    page_source = "<html><body>Sign in</body></html>"
    window_handles = ["tab-1"]

    def __init__(self) -> None:
        self.switch_to = FakeSwitchTo()

    def save_screenshot(self, path: str) -> None:
        Path(path).write_bytes(b"\x89PNG fake")

    def find_elements(self, by: str, value: str) -> list[object]:
        return []


# --- R1, R2: the success email ---------------------------------------------------------------


def test_success_block_has_every_field_above_the_unchanged_report(harness) -> None:
    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)
    body = harness.body

    value_cell = f'</b></td><td style="{alert_utils._CELL}">'
    expected = [
        "<b>What happened, in plain English</b>",
        "AI-generated. Check it before acting on it.",
        f"<b>Safe to rerun now? (AI's view){value_cell}"
        '<span style="background:#fef3c7;color:#92400e;padding:2px 8px"><b>Yes, after the check below</b></span></td>',
        f"<b>Confidence{value_cell}Medium</td>",
        f"<b>When{value_cell}{STAMP}</td>",
        f"<b>Which step{value_cell}main in run_demo_report.py, line 42<br>"
        "Last log line: 2026-01-02 03:04:01 INFO Opening the reports page.</td>",
        "<b>What happened</b></p>",
        f">{SUMMARY['what_happened']}</p>",
        "<b>Why</b></p>",
        f">{SUMMARY['why']}</p>",
        "<b>How to fix</b></p>",
        ">Sign in once in the automation&#x27;s browser profile.<br>Then rerun.</p>",
        "<b>Why a rerun is or is not safe</b></p>",
        ">Nothing was written or emailed before the failure.</p>",
        "<b>Technical fix</b></p>",
        "monospace\">run_demo_report.py, line 42</span><br>Check for the sign-in page after loading "
        "the reports page and raise a clear error.</p>",
    ]
    for fragment in expected:
        assert fragment in body, fragment
    positions = [body.index(fragment) for fragment in expected]
    assert positions == sorted(positions)

    report = (
        f"    <b>Automation:</b> {DISPLAY}<br>\n"
        f"    <b>Timestamp:</b> {STAMP}<br><br>\n"
        "    <b>Open Tabs:</b><br>\n"
        "    <pre>Browser was not initialized.</pre><br>\n"
        "    <b>Full Traceback:</b><br>\n"
        f"    <pre>{html.escape(TRACEBACK)}</pre>\n"
    )
    assert report in body
    assert positions[-1] < body.index(report)
    assert "AI summary unavailable" not in body


def test_subject_is_byte_identical_to_the_pre_change_format(harness) -> None:
    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    sent = harness.sent[0]
    assert sent["subject"] == f"[CRASH] {DISPLAY} \u2014 {STAMP}"
    assert sent["subject"].encode("utf-8") == "[CRASH] Demo Report \u2014 2026-01-02 03:04:05".encode("utf-8")
    assert (sent["to"], sent["send"], sent["show"]) == (["alerts@example.com"], True, False)


def test_build_prompt_gets_the_archive_name_and_an_aware_crash_time(harness) -> None:
    alert_utils.handle_crash(FakeDriver(), TRACEBACK, DISPLAY)

    kwargs = harness.prompt_kwargs
    assert kwargs["crash_dir"] == harness.crash_dir
    assert kwargs["name"] == NAME
    assert kwargs["display_name"] == DISPLAY
    assert kwargs["traceback_text"] == TRACEBACK
    assert kwargs["crashed_at"].tzinfo is not None
    assert kwargs["crashed_at"].strftime("%Y-%m-%d %H:%M:%S") == STAMP
    assert kwargs["crash_root"] == fleet_state.crash_root()
    assert kwargs["kb_dir"] is None
    assert kwargs["log_text"] == "2026-01-02 03:04:01 INFO Opening the reports page."
    assert harness.screenshot == harness.crash_dir / "screenshot.png"
    assert harness.screenshot.exists()
    assert sorted(Path(p).name for p in harness.sent[0]["attachments"]) == ["dom.txt", "screenshot.png"]


# --- Order -----------------------------------------------------------------------------------


def test_cleanup_runs_before_the_ai_step_and_the_email_after_it(harness) -> None:
    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert harness.events == ["record", "kill", "build_prompt", "explain", "send"]


def test_cleanup_failure_still_gets_the_summary_and_the_email(harness, monkeypatch) -> None:
    def explode() -> None:
        harness.events.append("kill")
        raise RuntimeError("access denied")
    monkeypatch.setattr(alert_utils, "_kill_own_processes", explode)

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert harness.events == ["record", "kill", "build_prompt", "explain", "send"]
    assert "<b>What happened, in plain English</b>" in harness.body


def test_crash_ai_0_skips_building_the_prompt(harness, monkeypatch) -> None:
    monkeypatch.setenv("CRASH_AI", "0")

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert harness.events == ["record", "kill", "send"]
    assert ">AI summary unavailable: disabled (CRASH_AI=0)</p>" in harness.body
    assert harness.record()["ai_summary_error"] == "disabled (CRASH_AI=0)"


def test_no_codex_skips_the_repo_lookup_and_the_prompt(harness, monkeypatch) -> None:
    monkeypatch.setattr(crash_explainer, "_find_codex", lambda: None)
    monkeypatch.setattr(crash_explainer, "find_repo_root", lambda traceback_text: harness.events.append("git"))

    def default_kb_dir():
        harness.events.append("kb_dir")

    monkeypatch.setattr(crash_explainer, "default_kb_dir", default_kb_dir)

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    # The only knowledge-base lookup is the note step's, after the email.
    assert harness.events == ["record", "kill", "send", "kb_dir"]
    assert ">AI summary unavailable: codex not found</p>" in harness.body
    assert harness.record()["ai_summary_error"] == "codex not found"


# --- One deadline for the whole AI step ------------------------------------------------------


@pytest.mark.parametrize("stage", ["build_prompt", "explain"])
def test_a_hung_ai_step_is_abandoned_at_the_deadline_and_the_email_goes_out(harness, monkeypatch, stage) -> None:
    release = threading.Event()

    def hang(*args, **kwargs):
        release.wait(30)
        # Raise, not return: a released worker must not go on to call explain() after the patches are undone.
        raise RuntimeError("released")

    monkeypatch.setattr(crash_explainer, stage, hang)
    monkeypatch.setattr(crash_explainer, "crash_ai_timeout", lambda: 0)
    monkeypatch.setattr(alert_utils, "_AI_DEADLINE_SLACK", 0.2)
    try:
        alert_utils.handle_crash(None, TRACEBACK, DISPLAY)
    finally:
        release.set()
        for thread in threading.enumerate():
            if thread.name == "crash-ai-summary":
                thread.join(5)

    assert ">AI summary unavailable: timed out after 0.2 s</p>" in harness.body
    assert harness.record()["ai_summary_error"] == "timed out after 0.2 s"
    assert harness.record()["emailed"] is True


class _RecordingThread(threading.Thread):
    joins: list[tuple[str, float | None]] = []

    def join(self, timeout: float | None = None) -> None:
        _RecordingThread.joins.append((self.name, timeout))
        super().join(timeout)


def test_the_deadline_is_the_codex_timeout_plus_30_s(harness, monkeypatch) -> None:
    monkeypatch.setenv("CRASH_AI_TIMEOUT", "45")
    monkeypatch.setattr(_RecordingThread, "joins", [])
    monkeypatch.setattr(alert_utils.threading, "Thread", _RecordingThread)

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    # The note step after the email has its own bound, unaffected by CRASH_AI_TIMEOUT. Other joins
    # (subprocess pipe readers for git) are not crash-handler deadlines.
    deadlines = [join for join in _RecordingThread.joins if join[0].startswith("crash-")]
    assert deadlines == [("crash-ai-summary", 75), ("crash-kb-note", 60)]
    assert "<b>What happened, in plain English</b>" in harness.body


# --- Ctrl+C during the AI step ---------------------------------------------------------------


class _InterruptedThread(threading.Thread):
    def join(self, timeout: float | None = None) -> None:
        super().join(timeout)
        raise KeyboardInterrupt


def test_ctrl_c_during_the_ai_step_still_records_and_emails_then_reraises(harness, monkeypatch) -> None:
    monkeypatch.setattr(alert_utils.threading, "Thread", _InterruptedThread)

    with pytest.raises(KeyboardInterrupt):
        alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert ">AI summary unavailable: interrupted</p>" in harness.body
    record = harness.record()
    assert record["ai_summary_error"] == "interrupted"
    assert record["emailed"] is True
    # Every join is interrupted here, so the note step gives up too, and says so.
    assert record["kb_note_error"] == "interrupted"


# --- R5: every failure becomes one reason line, and the email still goes out -------------------


def _raise(exc: Exception):
    def boom(*args, **kwargs):
        raise exc
    return boom


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("timed out after 120 s", "timed out after 120 s"),
        ("Codex login expired or missing", "Codex login expired or missing"),
        ("rate or usage limit reached", "rate or usage limit reached"),
        ("invalid JSON from the model", "invalid JSON from the model"),
        ("codex not found", "codex not found"),
        ("disabled (CRASH_AI=0)", "disabled (CRASH_AI=0)"),
        (None, "no reason given"),
    ],
)
def test_explain_failure_becomes_the_reason_line(harness, reason, expected) -> None:
    harness.set_answer(None, reason)

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    body = harness.body
    assert f">AI summary unavailable: {html.escape(expected)}</p>" in body
    assert "What happened, in plain English" not in body
    assert f"<pre>{html.escape(TRACEBACK)}</pre>" in body
    record = harness.record()
    assert record["ai_summary_error"] == expected
    assert "ai_summary" not in record
    assert record["emailed"] is True


@pytest.mark.parametrize(
    ("target", "exc", "expected"),
    [
        ("build_prompt", RuntimeError("git exploded"), "unexpected error: RuntimeError"),
        ("explain", ValueError("bad"), "unexpected error: ValueError"),
        ("active_log_file", OSError("gone"), "unexpected error: OSError"),
        ("default_kb_dir", PermissionError("denied"), "unexpected error: PermissionError"),
        ("explain", SystemExit(1), "unexpected error: SystemExit"),
    ],
)
def test_an_exception_in_the_ai_step_becomes_the_reason_line(harness, monkeypatch, target, exc, expected) -> None:
    monkeypatch.setattr(crash_explainer, target, _raise(exc))

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)  # must not raise

    assert f"AI summary unavailable: {expected}" in harness.body
    assert harness.record()["ai_summary_error"] == expected
    assert harness.record()["emailed"] is True


def test_archive_failure_skips_the_model_and_still_sends(harness, monkeypatch) -> None:
    def record_crash(*args, **kwargs):
        harness.events.append("record")
        return None
    monkeypatch.setattr(fleet_state, "record_crash", record_crash)

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert harness.events == ["record", "kill", "send"]
    assert "AI summary unavailable: crash archive unavailable" in harness.body
    assert harness.sent[0]["attachments"] == []


def test_a_summary_that_cannot_render_falls_back_to_the_reason_line(harness) -> None:
    broken = {key: value for key, value in SUMMARY.items() if key != "technical_fix"}
    harness.set_answer(broken, None)

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert "AI summary unavailable: could not render the summary: KeyError" in harness.body
    assert "What happened, in plain English" not in harness.body


def test_an_unhashable_rerun_value_falls_back_to_the_reason_line(harness) -> None:
    harness.set_answer({**SUMMARY, "safe_to_rerun": ["yes"]}, None)

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert "AI summary unavailable: could not render the summary: TypeError" in harness.body
    assert "What happened, in plain English" not in harness.body
    assert harness.record()["emailed"] is True


# --- R6: crash.json --------------------------------------------------------------------------


def test_crash_json_has_the_summary_before_the_email_and_emailed_flips_after(harness) -> None:
    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert harness.record_at_send is not None
    assert harness.record_at_send["ai_summary"] == SUMMARY
    assert harness.record_at_send["emailed"] is False
    record = harness.record()
    assert record["ai_summary"] == SUMMARY
    assert "ai_summary_error" not in record
    assert record["emailed"] is True
    assert record["traceback"] == TRACEBACK


def test_crash_json_has_the_error_before_the_email(harness) -> None:
    harness.set_answer(None, "codex not found")

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert harness.record_at_send is not None
    assert harness.record_at_send["ai_summary_error"] == "codex not found"
    assert harness.record_at_send["emailed"] is False


def test_email_failure_keeps_the_summary_and_leaves_emailed_false(harness, monkeypatch) -> None:
    monkeypatch.setattr(alert_utils.outlook, "send_email", _raise(RuntimeError("Outlook is closed")))

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)  # must not raise

    record = harness.record()
    assert record["ai_summary"] == SUMMARY
    assert record["emailed"] is False


# --- Escaping --------------------------------------------------------------------------------


def test_model_text_reason_traceback_and_tabs_are_escaped(harness) -> None:
    harness.set_answer({**SUMMARY, "why": "<script>alert(1)</script>"}, None)

    alert_utils.handle_crash(FakeDriver(), TRACEBACK, DISPLAY)

    body = harness.body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "<script>" not in body
    assert "line 90, in &lt;module&gt;" in body
    assert "<module>" not in body
    assert "Tab 1: https://portal.example.test/reports?a=1&amp;b=&lt;2&gt;" in body


def test_a_traceback_that_is_not_a_string_still_gets_emailed(harness, monkeypatch) -> None:
    # The harness's sender reads crash.json, which a non-string traceback cannot be written into.
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(alert_utils.outlook, "send_email", lambda **kwargs: sent.append(kwargs))

    alert_utils.handle_crash(None, ValueError("bad <value>"), DISPLAY)  # type: ignore[arg-type]

    assert len(sent) == 1
    assert "<pre>bad &lt;value&gt;</pre>" in sent[0]["body"]


def test_reason_is_escaped(harness) -> None:
    harness.set_answer(None, "codex failed (exit 1): <b>server</b> said no")

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert "AI summary unavailable: codex failed (exit 1): &lt;b&gt;server&lt;/b&gt; said no" in harness.body
    assert "<b>server</b>" not in harness.body


def test_urls_in_the_reason_are_broken_so_they_cannot_auto_link() -> None:
    block = alert_utils._summary_html(None, "codex failed: see https://status.example/x or WWW.example.test", STAMP, "")

    assert "AI summary unavailable: codex failed: see https[:]//status.example/x or WWW[.]example.test</p>" in block
    assert "://" not in block


def test_every_summary_field_is_escaped() -> None:
    hostile = "<img src=x onerror=alert(1)>"
    summary = {
        **SUMMARY,
        "what_happened": hostile,
        "how_to_fix": hostile,
        "safe_to_rerun_reason": hostile,
        "technical_fix": {"file": hostile, "line": 3, "change": hostile},
    }

    block = alert_utils._summary_html(summary, None, STAMP, hostile)

    assert "<img" not in block
    assert block.count(html.escape(hostile)) == 6


def test_urls_in_every_model_field_are_broken_so_they_cannot_auto_link() -> None:
    link = "Open https://evil.example/login or WWW.evil.example now"
    summary = {
        **SUMMARY,
        "what_happened": link,
        "why": link,
        "how_to_fix": link,
        "safe_to_rerun_reason": link,
        "technical_fix": {"file": "ftp://evil.example/x.py", "line": None, "change": link},
    }

    block = alert_utils._summary_html(summary, None, STAMP, "Unknown")

    assert "://" not in block
    assert "www." not in block.lower()
    assert block.count("Open https[:]//evil.example/login or WWW[.]evil.example now") == 5
    assert "ftp[:]//evil.example/x.py</span><br>Open" in block
    assert "AI-generated. Check it before acting on it." in block


def test_hostile_values_cannot_add_styles_or_tags_or_break_the_layout() -> None:
    hostile = """x" style="color:red" ' ; <b>x</b> url(http://e) </span></td></table>"""
    summary = {
        **{key: hostile for key in SUMMARY},
        "technical_fix": {"file": hostile, "line": hostile, "change": hostile},
    }
    # Every style the layout emits, gray fallback badge included, from benign values.
    layout_styles = {
        style
        for value in (*alert_utils._RERUN, "unknown")
        for style in re.findall(
            r'style="([^"]*)"',
            alert_utils._summary_html({**SUMMARY, "safe_to_rerun": value}, None, STAMP, "Unknown"),
        )
    }

    block = alert_utils._summary_html(summary, None, hostile, hostile)

    assert set(re.findall(r'style="([^"]*)"', block)) <= layout_styles
    assert block.count("<span") == block.count("</span>")
    assert set(re.findall(r"</?([a-zA-Z][a-zA-Z0-9]*)", block)) <= {"b", "br", "div", "p", "span", "table", "td", "tr"}


# --- _summary_html branches ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fix", "expected"),
    [
        ({"file": "run_x.py", "line": 7, "change": "Wait longer."}, 'monospace">run_x.py, line 7</span><br>Wait longer.</p>'),
        ({"file": "run_x.py", "line": None, "change": "Wait longer."}, 'monospace">run_x.py</span><br>Wait longer.</p>'),
        ({"file": None, "line": None, "change": "No code change; the site was down."},
         '<b>Technical fix</b></p><p style="margin:0;">No code change; the site was down.</p>'),
    ],
)
def test_technical_fix_rendering(fix, expected) -> None:
    block = alert_utils._summary_html({**SUMMARY, "technical_fix": fix}, None, STAMP, "Unknown")

    assert expected in block


@pytest.mark.parametrize(
    ("value", "label", "background", "color"),
    [
        ("yes", "Yes", "#dcfce7", "#166534"),
        ("yes_after_check", "Yes, after the check below", "#fef3c7", "#92400e"),
        ("no", "No", "#fee2e2", "#991b1b"),
        ("<maybe>", "&lt;maybe&gt;", "#e5e7eb", "#374151"),
    ],
)
def test_rerun_labels(value, label, background, color) -> None:
    block = alert_utils._summary_html({**SUMMARY, "safe_to_rerun": value}, None, STAMP, "Unknown")

    assert f'<span style="background:{background};color:{color};padding:2px 8px"><b>{label}</b></span>' in block
    assert ">Nothing was written or emailed before the failure.</p>" in block


@pytest.mark.parametrize(("value", "label"), [("high", "High"), ("medium", "Medium"), ("low", "Low")])
def test_confidence_labels(value, label) -> None:
    block = alert_utils._summary_html({**SUMMARY, "confidence": value}, None, STAMP, "Unknown")

    assert f"<b>Confidence</b></td><td style=\"padding:6px 10px;border:1px solid #e5e7eb;vertical-align:top;\">{label}</td>" in block


def test_summary_html_makes_no_links_images_or_external_styles() -> None:
    block = alert_utils._summary_html(SUMMARY, None, STAMP, "main in run_x.py, line 1")

    # Inline style attributes are the layout; anything that could fetch or link something is not.
    for forbidden in ("<a ", "<img", "<style", "<link", "http", "url(", "@import", "<hr"):
        assert forbidden not in block


# --- _crash_step -----------------------------------------------------------------------------

LIB_ONLY = (
    "Traceback (most recent call last):\n"
    '  File "C:\\lib\\site-packages\\seleniumbase\\core.py", line 5, in click\n'
    "    raise X\n"
    "X: boom\n"
)
HEADER = "Traceback (most recent call last):\n"
CHAINED = (
    HEADER
    + '  File "C:\\repos\\demo\\run_first.py", line 3, in main\n'
    + "    load()\n"
    + "KeyError: 'id'\n"
    + "\nDuring handling of the above exception, another exception occurred:\n\n"
    + HEADER
    + '  File "C:\\repos\\demo\\run_second.py", line 8, in handle\n'
    + "    report()\n"
    + "ValueError: no report\n"
)


@pytest.mark.parametrize(
    ("traceback_text", "log_tail_text", "expected"),
    [
        pytest.param(TRACEBACK, "INFO a\nINFO Saving the file.\n\n",
                     "main in run_demo_report.py, line 42\nLast log line: INFO Saving the file.",
                     id="frame-and-log"),
        pytest.param(TRACEBACK, "", "main in run_demo_report.py, line 42", id="frame-no-log"),
        pytest.param(TRACEBACK, "No log file found.", "main in run_demo_report.py, line 42",
                     id="frame-missing-log-phrase"),
        pytest.param(TRACEBACK, "The log file is empty.", "main in run_demo_report.py, line 42",
                     id="frame-empty-log-phrase"),
        pytest.param(LIB_ONLY, "INFO Clicking export.", "Last log line: INFO Clicking export.",
                     id="log-no-run-frame"),
        pytest.param(LIB_ONLY, "", "Unknown", id="neither"),
        pytest.param("", "   \n", "Unknown", id="empty-inputs"),
        pytest.param(HEADER + '  File "/srv/jobs/Run_Nightly.py", line 3, in <module>\n', "",
                     "<module> in Run_Nightly.py, line 3", id="posix-path-any-case"),
        pytest.param(HEADER + '  File "C:\\x\\runner.py", line 3, in main\n', "", "Unknown",
                     id="not-a-run-underscore-script"),
        pytest.param('ValueError: bad\n File "C:\\x\\run_evil.py", line 1, in fake\n', "", "Unknown",
                     id="message-text-posing-as-a-frame"),
        pytest.param('ValueError: bad\n  File "C:\\x\\run_evil.py", line 1, in fake\n', "", "Unknown",
                     id="two-space-message-text-without-a-traceback"),
        pytest.param(TRACEBACK + '  File "C:\\x\\run_evil.py", line 1, in fake\n    evil()\n', "",
                     "main in run_demo_report.py, line 42", id="two-space-fake-frame-in-a-multi-line-message"),
        pytest.param(CHAINED, "", "handle in run_second.py, line 8", id="chained-takes-the-last-section"),
        pytest.param(CHAINED + LIB_ONLY.replace("Traceback", "\nDuring handling, another:\n\nTraceback"), "",
                     "Unknown", id="last-section-without-a-run-frame"),
    ],
)
def test_crash_step(traceback_text, log_tail_text, expected) -> None:
    assert alert_utils._crash_step(traceback_text, log_tail_text) == expected


def test_crash_step_knows_every_no_log_phrase_log_tail_returns(tmp_path) -> None:
    empty = tmp_path / "empty.log"
    empty.write_text("", encoding="utf-8")

    assert crash_explainer.log_tail(None) in alert_utils._NO_LOG_LINES
    assert crash_explainer.log_tail(tmp_path / "missing.log") in alert_utils._NO_LOG_LINES
    assert crash_explainer.log_tail(empty) in alert_utils._NO_LOG_LINES


def test_last_log_line_is_read_before_handle_crash_logs_anything(harness, tmp_path, monkeypatch) -> None:
    import logging
    from logging.handlers import RotatingFileHandler

    log_file = tmp_path / "live.log"
    log_file.write_text("INFO Uploading the feed.\n", encoding="utf-8")
    handler = RotatingFileHandler(log_file, encoding="utf-8")
    old_level = alert_utils.log.level
    alert_utils.log.addHandler(handler)
    alert_utils.log.setLevel(logging.DEBUG)
    monkeypatch.setattr(crash_explainer, "active_log_file", lambda: log_file)
    try:
        alert_utils.handle_crash(None, TRACEBACK, DISPLAY)
    finally:
        alert_utils.log.removeHandler(handler)
        alert_utils.log.setLevel(old_level)
        handler.close()

    assert "Asking the AI model to explain the crash." in log_file.read_text(encoding="utf-8")
    assert "Last log line: INFO Uploading the feed.</td>" in harness.body
    # The prompt gets the same pre-crash tail, not the file as it reads after handle_crash's own lines.
    assert harness.prompt_kwargs["log_text"] == "INFO Uploading the feed."


# --- M3: the knowledge-base note, written after the email ----------------------------------


REPO_ROOT = Path("C:/repos/demo-report")


class _SteppingClock(datetime):
    """fleet_state's clock, one minute later on every call, so two crashes get two archive folders."""

    ticks = 0

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206 - mirrors datetime.now
        cls.ticks += 1
        at = datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc) + timedelta(minutes=cls.ticks)
        return at if tz is not None else at.replace(tzinfo=None)


@pytest.fixture
def kb(harness, tmp_path, monkeypatch) -> Path:
    """A temporary vault the note step finds, a fixed repo root, and a recorded note write."""
    root = tmp_path / "kb"
    root.mkdir()
    monkeypatch.setattr(crash_explainer, "default_kb_dir", lambda: root)
    harness.repo_lookups = 0

    def find_repo_root(traceback_text):
        harness.repo_lookups += 1
        return REPO_ROOT

    monkeypatch.setattr(crash_explainer, "find_repo_root", find_repo_root)
    real_write = crash_notes._write_note

    def write_note(*args, **kwargs):
        harness.events.append("note")
        harness.note_kwargs = kwargs
        return real_write(*args, **kwargs)

    monkeypatch.setattr(crash_notes, "_write_note", write_note)
    return root


def _notes(kb: Path) -> list[Path]:
    return sorted((kb / "Crashes").rglob("*.md"))


def test_the_note_is_written_after_the_email(harness, kb) -> None:
    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert harness.events == ["record", "kill", "build_prompt", "explain", "send", "note"]
    # The email went out before the note existed, so crash.json at send time has no note fields.
    assert "kb_note" not in harness.record_at_send and "kb_note_error" not in harness.record_at_send
    [note] = _notes(kb)
    # The date is crash.json's real "at", which the frozen alert_utils clock does not reach.
    assert note.parent == kb / "Crashes" / "demo-report"
    assert note.name.endswith(" TimeoutError in run_demo_report-main.md")
    record = harness.record()
    assert record["kb_note"] == str(note)
    assert "kb_note_error" not in record
    assert SUMMARY["why"] in note.read_text(encoding="utf-8")


def test_the_screenshot_reaches_the_model_and_the_email_but_never_the_vault(harness, kb) -> None:
    alert_utils.handle_crash(FakeDriver(), TRACEBACK, DISPLAY)

    screenshot = harness.crash_dir / "screenshot.png"
    assert harness.screenshot == screenshot and screenshot.read_bytes() == b"\x89PNG fake"
    assert "screenshot.png" in [Path(p).name for p in harness.sent[0]["attachments"]]
    [note] = _notes(kb)
    assert [path for path in kb.rglob("*") if path.is_file()] == [note]
    text = note.read_text(encoding="utf-8")
    assert "![[" not in text and ".png" not in text
    assert str(harness.crash_dir) in text


def test_the_repo_root_is_found_once_and_shared_by_the_prompt_and_the_note(harness, kb) -> None:
    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert harness.repo_lookups == 1
    assert harness.prompt_kwargs["repo_root"] == REPO_ROOT
    assert harness.note_kwargs["repo_root"] == REPO_ROOT
    assert harness.note_kwargs["kb_dir"] == kb
    assert harness.note_kwargs["log_tail_text"] == "2026-01-02 03:04:01 INFO Opening the reports page."


def test_crash_ai_0_still_writes_a_note_that_says_the_summary_was_disabled(harness, kb, monkeypatch) -> None:
    monkeypatch.setenv("CRASH_AI", "0")

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert harness.events == ["record", "kill", "send", "note"]
    [note] = _notes(kb)
    assert "AI summary unavailable: disabled (CRASH_AI=0)" in note.read_text(encoding="utf-8")
    assert harness.record()["kb_note"] == str(note)


def test_the_same_error_twice_gives_one_note_with_two_occurrences(harness, kb, monkeypatch) -> None:
    monkeypatch.setattr(_SteppingClock, "ticks", 0)
    monkeypatch.setattr(fleet_state, "datetime", _SteppingClock)

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)
    first = harness.crash_dir
    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert harness.crash_dir != first
    [note] = _notes(kb)
    text = note.read_text(encoding="utf-8")
    assert "\noccurrences: 2\n" in text
    assert "## Occurrence 2:" in text
    assert str(first) in text and str(harness.crash_dir) in text
    assert harness.record()["kb_note"] == str(note)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (RuntimeError("disk full"), "unexpected error: RuntimeError"),
        (SystemExit(1), "unexpected error: SystemExit"),
    ],
)
def test_a_failing_note_step_never_costs_the_email(harness, kb, monkeypatch, exc, expected) -> None:
    def explode(*args, **kwargs):
        harness.events.append("note")
        raise exc

    monkeypatch.setattr(crash_notes, "_write_note", explode)

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)  # must not raise

    assert harness.events[-2:] == ["send", "note"]
    assert len(harness.sent) == 1
    record = harness.record()
    assert record["emailed"] is True
    assert record["ai_summary"] == SUMMARY
    assert record["kb_note_error"] == expected
    assert "kb_note" not in record


def test_a_hung_note_step_is_abandoned_at_its_deadline(harness, kb, monkeypatch) -> None:
    release = threading.Event()

    def hang(*args, **kwargs):
        release.wait(30)
        raise RuntimeError("released")

    monkeypatch.setattr(crash_notes, "_write_note", hang)
    monkeypatch.setattr(alert_utils, "_NOTE_DEADLINE", 0.2)
    try:
        alert_utils.handle_crash(None, TRACEBACK, DISPLAY)
    finally:
        release.set()
        for thread in threading.enumerate():
            if thread.name == "crash-kb-note":
                thread.join(5)

    assert len(harness.sent) == 1
    record = harness.record()
    assert record["emailed"] is True
    assert record["kb_note_error"] == "timed out after 0.2 s"


@pytest.mark.parametrize("folder", [None, "missing"], ids=["none-found", "mistyped-override"])
def test_no_vault_is_recorded_and_nothing_is_created(harness, tmp_path, monkeypatch, folder) -> None:
    target = tmp_path / folder if folder else None
    monkeypatch.setattr(crash_explainer, "default_kb_dir", lambda: target)

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    expected = f"knowledge base folder not found: {target}" if target else "no knowledge base folder"
    assert harness.record()["kb_note_error"] == expected
    assert target is None or not target.exists()
    assert harness.record()["emailed"] is True


@pytest.mark.parametrize(
    ("folder", "why"),
    [
        ("relative-kb", "a relative path"),
        ("//nas.example.invalid/share/kb", "a network (UNC) path"),
        ("\\\\nas.example.invalid\\share\\kb", "a network (UNC) path"),
    ],
    ids=["relative", "unc-forward-slashes", "unc-backslashes"],
)
def test_an_unusable_kb_folder_is_refused_before_it_is_touched(harness, kb, monkeypatch, folder, why) -> None:
    monkeypatch.setattr(crash_explainer, "default_kb_dir", lambda: Path(folder))
    touched: list[str] = []
    real_is_dir = Path.is_dir

    def is_dir(self, *args, **kwargs):
        touched.append(str(self))
        return real_is_dir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_dir", is_dir)

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    record = harness.record()
    assert record["kb_note_error"] == f"knowledge base folder not usable: {why}"
    assert record["emailed"] is True
    assert not any("nas.example.invalid" in path or path == "relative-kb" for path in touched)
    assert "note" not in harness.events
    # The AI step applies the same rule: the prompt is built without the unusable folder.
    assert harness.prompt_kwargs["kb_dir"] is None


def test_a_kb_folder_inside_the_crashed_repo_is_refused(harness, kb, tmp_path, monkeypatch) -> None:
    repo = tmp_path / "demo-report"
    inside = repo / "notes"
    inside.mkdir(parents=True)
    monkeypatch.setattr(crash_explainer, "find_repo_root", lambda traceback_text: repo)
    monkeypatch.setattr(crash_explainer, "default_kb_dir", lambda: inside)

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert harness.record()["kb_note_error"] == "knowledge base folder not usable: inside the crashed repo"
    assert list(inside.iterdir()) == []
    # Windows paths compare without case, so a differently cased spelling is still inside.
    assert alert_utils._kb_dir_refusal(Path(str(inside).upper()), repo) == (
        "knowledge base folder not usable: inside the crashed repo"
    )


@pytest.mark.parametrize("where", ["kb inside the archive", "archive inside the kb"])
def test_a_kb_folder_overlapping_the_crash_archive_is_refused(harness, kb, monkeypatch, where) -> None:
    state = fleet_state.state_root()
    state.mkdir(parents=True, exist_ok=True)
    folder = state / "kb" if where == "kb inside the archive" else state.parent
    folder.mkdir(exist_ok=True)
    before = sorted(folder.rglob("*"))
    monkeypatch.setattr(crash_explainer, "default_kb_dir", lambda: folder)

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert harness.record()["kb_note_error"] == "knowledge base folder not usable: overlaps the crash archive"
    assert "note" not in harness.events
    assert harness.prompt_kwargs["kb_dir"] is None
    if where == "kb inside the archive":
        assert sorted(folder.rglob("*")) == before
    # Windows paths compare without case.
    assert alert_utils._kb_dir_refusal(Path(str(folder).upper()), None) == (
        "knowledge base folder not usable: overlaps the crash archive"
    )
    # A sibling of the archive is not an overlap.
    assert alert_utils._kb_dir_refusal(kb, None) is None


def test_a_refused_note_records_why_and_is_left_alone(harness, kb) -> None:
    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)
    [note] = _notes(kb)
    note.write_text("hand-edited, data line gone\n", encoding="utf-8")

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert harness.record()["kb_note_error"] == "the existing note's data line is missing or unreadable"
    assert note.read_text(encoding="utf-8") == "hand-edited, data line gone\n"


def test_the_note_is_written_even_when_the_email_fails(harness, kb, monkeypatch) -> None:
    monkeypatch.setattr(alert_utils.outlook, "send_email", _raise(RuntimeError("Outlook is closed")))

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    [note] = _notes(kb)
    record = harness.record()
    assert record["emailed"] is False
    assert record["kb_note"] == str(note)


def test_no_archive_means_no_note_attempt(harness, kb, monkeypatch) -> None:
    monkeypatch.setattr(fleet_state, "record_crash", lambda *a, **k: None)

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert "note" not in harness.events
    assert not (kb / "Crashes").exists()
    assert len(harness.sent) == 1


class _NoteInterruptedThread(threading.Thread):
    def join(self, timeout: float | None = None) -> None:
        super().join(timeout)
        if self.name == "crash-kb-note":
            raise KeyboardInterrupt


def test_ctrl_c_during_the_note_step_keeps_the_summary_and_the_email_then_reraises(harness, kb, monkeypatch) -> None:
    monkeypatch.setattr(alert_utils.threading, "Thread", _NoteInterruptedThread)

    with pytest.raises(KeyboardInterrupt):
        alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert len(harness.sent) == 1
    assert "<b>What happened, in plain English</b>" in harness.body
    record = harness.record()
    assert record["ai_summary"] == SUMMARY
    assert record["emailed"] is True
    assert record["kb_note_error"] == "interrupted"


_REAL_JOIN = threading.Thread.join


def _release_and_join(release: threading.Event) -> None:
    release.set()
    for thread in threading.enumerate():
        if thread.name.startswith("crash-"):
            # The real join: a test's Thread subclass may make join() raise.
            _REAL_JOIN(thread, 5)


class _AiWaitInterrupted(threading.Thread):
    def join(self, timeout: float | None = None) -> None:
        if self.name == "crash-ai-summary":
            raise KeyboardInterrupt
        super().join(timeout)


def test_ctrl_c_while_the_ai_step_is_still_finding_the_repo_still_emails_and_writes_the_note(
    harness, kb, monkeypatch
) -> None:
    release = threading.Event()
    lookups: list[str] = []

    def find_repo_root(traceback_text):
        lookups.append(threading.current_thread().name)
        if len(lookups) == 1:
            release.wait(30)
            # SystemExit, not Exception: _repo_root would swallow an Exception and go on to build the prompt.
            raise SystemExit("released")
        return REPO_ROOT

    monkeypatch.setattr(crash_explainer, "find_repo_root", find_repo_root)
    monkeypatch.setattr(alert_utils.threading, "Thread", _AiWaitInterrupted)
    try:
        with pytest.raises(KeyboardInterrupt):
            alert_utils.handle_crash(None, TRACEBACK, DISPLAY)
        events = list(harness.events)
    finally:
        _release_and_join(release)

    assert events == ["record", "kill", "send", "note"]
    assert ">AI summary unavailable: interrupted</p>" in harness.body
    record = harness.record()
    assert record["ai_summary_error"] == "interrupted"
    assert record["emailed"] is True
    # The AI step never handed a root over, so the note step found it itself, after the email.
    assert lookups == ["crash-ai-summary", "crash-kb-note"]
    [note] = _notes(kb)
    assert note.parent.name == "demo-report"
    assert record["kb_note"] == str(note)


def test_a_hung_repo_lookup_cannot_delay_the_email(harness, kb, monkeypatch) -> None:
    """git on this host can outlast its own timeout, so the lookup runs only inside the steps' deadlines."""
    release = threading.Event()

    def hang(traceback_text):
        release.wait(30)
        raise SystemExit("released")

    sent_after: list[float] = []
    real_send = alert_utils.outlook.send_email

    def send_email(**kwargs):
        sent_after.append(time.monotonic() - started)
        real_send(**kwargs)

    monkeypatch.setattr(crash_explainer, "find_repo_root", hang)
    monkeypatch.setattr(alert_utils.outlook, "send_email", send_email)
    monkeypatch.setattr(crash_explainer, "crash_ai_timeout", lambda: 0)
    monkeypatch.setattr(alert_utils, "_AI_DEADLINE_SLACK", 0.2)
    monkeypatch.setattr(alert_utils, "_NOTE_DEADLINE", 0.2)
    started = time.monotonic()
    try:
        alert_utils.handle_crash(None, TRACEBACK, DISPLAY)
    finally:
        _release_and_join(release)

    assert len(sent_after) == 1 and sent_after[0] < 5
    record = harness.record()
    assert record["ai_summary_error"] == "timed out after 0.2 s"
    assert record["emailed"] is True
    assert record["kb_note_error"] == "timed out after 0.2 s"


def test_crash_ai_0_makes_no_repo_lookup_before_the_email(harness, kb, monkeypatch) -> None:
    monkeypatch.setenv("CRASH_AI", "0")

    def find_repo_root(traceback_text):
        harness.events.append("find_repo_root")
        return REPO_ROOT

    monkeypatch.setattr(crash_explainer, "find_repo_root", find_repo_root)

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert harness.events == ["record", "kill", "send", "find_repo_root", "note"]
    assert _notes(kb)[0].parent.name == "demo-report"


def test_a_failing_repo_lookup_still_gets_the_summary_email_and_note(harness, kb, monkeypatch) -> None:
    monkeypatch.setattr(crash_explainer, "find_repo_root", _raise(RuntimeError("git exploded")))

    alert_utils.handle_crash(None, TRACEBACK, DISPLAY)

    assert harness.events == ["record", "kill", "build_prompt", "explain", "send", "note"]
    assert harness.prompt_kwargs["repo_root"] is None
    assert harness.record()["kb_note"] == str(_notes(kb)[0])
