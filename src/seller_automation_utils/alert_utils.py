"""Crash handling for fleet automations: capture, archive, clean up, explain, alert, record.

:func:`handle_crash` is the one entry point. It captures the browser state,
archives it through :mod:`fleet_state` before anything else can fail, kills only
the processes this automation started, asks :mod:`crash_explainer` for a
plain-English summary, emails all of it via Outlook, and then writes the
crash's knowledge-base note through :mod:`crash_notes`. The summary block sits
above the unchanged traceback; when there is no summary, one "AI summary
unavailable: <reason>" line takes its place and the email still goes out.
"""
from __future__ import annotations

import html
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from seller_automation_utils import _winproc, crash_explainer, crash_notes, excel_utils, fleet_state, outlook
from seller_automation_utils.config_utils import get_env

log = logging.getLogger(__name__)

# Outlook rejects oversized attachments, and a runaway page should never be the reason
# a crash alert fails to send.
MAX_DOM_CHARS = 8_000_000

# Frames nested deeper than this are almost always ad/tracking chrome, not the app.
MAX_FRAME_DEPTH = 3

# Added to CRASH_AI_TIMEOUT for the AI step as a whole: git, file reads and codex's own kill wait
# run outside codex's timeout.
_AI_DEADLINE_SLACK = 30

# Seconds for the knowledge-base note step. The vault is a OneDrive folder, so a sync lock or a slow
# disk could otherwise hold the crashed process open indefinitely after the email went out.
_NOTE_DEADLINE = 60


def _collect_dom(driver: object, max_depth: int = MAX_FRAME_DEPTH) -> str:
    """Serialize the current tab's DOM, descending into every iframe.

    `driver.page_source` returns only the top-level document. A page that renders
    its content inside iframes (Seller Central's program-eligibility widgets, for
    one) therefore looks empty in a crash report unless each frame is visited
    explicitly — which is the blind spot this exists to close.

    The driver is left on the top-level document. Individual frames that cannot be
    entered or read are noted inline and skipped rather than aborting the capture.

    Args:
        driver (object): Active SeleniumBase WebDriver instance.
        max_depth (int): How many levels of nested frames to descend.

    Returns:
        str: The top-level document followed by one labelled section per frame
            reached, or an empty string if not a single document could be read —
            an attachment of nothing but error markers is noise next to the
            traceback already in the email body.
    """
    parts: list[str] = []
    captured = 0

    def visit(path: str, depth: int) -> None:
        nonlocal captured
        if captured >= MAX_DOM_CHARS:
            return

        try:
            source = driver.page_source or ""
        except Exception as exc:
            parts.append(f"\n{'=' * 78}\n{path}\n{'=' * 78}\n<unreadable: {exc!r}>\n")
            return

        parts.append(f"\n{'=' * 78}\n{path}  ({len(source):,} chars)\n{'=' * 78}\n{source}")
        captured += len(source)

        if depth >= max_depth or captured >= MAX_DOM_CHARS:
            return

        try:
            frame_count = len(driver.find_elements("tag name", "iframe"))
        except Exception:
            return

        for index in range(frame_count):
            try:
                # Re-find every pass: returning from a nested frame can invalidate
                # references captured before the switch.
                frames = driver.find_elements("tag name", "iframe")
                if index >= len(frames):
                    break
                frame = frames[index]
                label = (
                    f"{path} > iframe[{index}] "
                    f"class={frame.get_attribute('class') or ''!r} "
                    f"src={frame.get_attribute('src') or ''}"
                )
                driver.switch_to.frame(frame)
            except Exception as exc:
                parts.append(f"\n{'=' * 78}\n{path} > iframe[{index}]\n{'=' * 78}\n<could not enter: {exc!r}>\n")
                continue

            visit(label, depth + 1)

            try:
                driver.switch_to.parent_frame()
            except Exception:
                # Lost our place in the frame tree; anything further would be misattributed.
                driver.switch_to.default_content()
                return

    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    visit("MAIN DOCUMENT", 0)

    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    if captured == 0:
        return ""

    if captured >= MAX_DOM_CHARS:
        parts.append(f"\n\n<truncated at {MAX_DOM_CHARS:,} chars>\n")

    return "".join(parts)


def _write_dom_file(driver: object, automation_name: str, timestamp: str) -> str | None:
    """Write the captured DOM to a temp `.txt` for attaching to the crash email.

    Args:
        driver (object): Active SeleniumBase WebDriver instance.
        automation_name (str): Human-readable script name, used in the filename.
        timestamp (str): Crash timestamp, recorded in the file header.

    Returns:
        str | None: Path to the written file, or None if capture failed.
    """
    try:
        try:
            current_url = driver.current_url
        except Exception:
            current_url = "<unknown>"

        dom = _collect_dom(driver)
        if not dom.strip():
            return None

        path = os.path.join(
            tempfile.gettempdir(),
            f"{automation_name.replace(' ', '_')}_crash_dom.txt"
        )
        header = (
            f"Automation: {automation_name}\n"
            f"Timestamp:  {timestamp}\n"
            f"URL:        {current_url}\n"
            f"Note:       sections below are the main document followed by each iframe.\n"
        )
        with open(path, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(header + dom)

        log.info(f"Crash DOM saved to [cyan]{path}[/cyan].")
        return path
    except Exception:
        log.warning("Could not capture DOM.")
        return None


def handle_crash(driver: object | None, error_traceback: str, automation_name: str) -> None:
    """Handle a script crash: capture browser state, archive it, clean up, explain it, alert.

    In order:

    1. Screenshot, live DOM (main document plus every iframe) and open tab URLs.
    2. **Archive all of it to disk** (``fleet_state.record_crash``).
    3. Force-kill the Chrome, driver and Excel processes **this automation** started
       (1.8.8+; see ``_kill_own_processes``). Other automations' processes, and the
       user's own Chrome and Excel, are left alone.
    4. Find the crashed repo's checkout from the traceback
       (``crash_explainer.find_repo_root``), then ask an AI model for a
       plain-English summary (``crash_explainer``, at most
       ``CRASH_AI_TIMEOUT`` + 30 seconds for the whole step; skipped outright,
       git included, when ``CRASH_AI=0``) and record it, or why there is none,
       in ``crash.json``.
    5. Send the crash report via Outlook: the summary block (or one "AI summary
       unavailable" line) above the tabs and traceback.
    6. Write the crash's knowledge-base note (``crash_notes.write_crash_note``,
       at most 60 seconds, the repo lookup included when step 4 did not finish
       it) and record its path as ``kb_note``, or why there is none as
       ``kb_note_error``, in ``crash.json``. ``CRASH_AI=0`` does not skip it:
       the note is written by code and then says the summary was disabled.

    A Ctrl+C during step 4 or 6 becomes the reason "interrupted" for that
    step; every later step still runs and the first ``KeyboardInterrupt`` is
    re-raised at the end.

    The archive is written *before* anything else can fail, and the screenshot
    and DOM are moved into it rather than deleted. The email goes out over
    Outlook COM, so a broken Outlook used to lose the traceback outright — the
    one failure that most needs reporting. The artifacts also used to survive
    only as mail attachments, which put them out of reach of the
    ``fleet-control`` dashboard and of any later debugging session.

    The cleanup runs before the AI step so a stuck Excel or Chrome is freed
    during the wait rather than after it. The AI step can only cost time: any
    failure there becomes a reason line and the email still goes out. The note
    comes last for the same reason: a OneDrive or disk problem in the vault
    must never delay or lose the email.

    If the driver was never initialized (e.g., Chrome failed to launch), the
    function skips the screenshot, DOM capture, and tab collection, and sends the
    email without attachments.

    Args:
        driver (object | None): Active SeleniumBase WebDriver instance, or None if
            the browser was never successfully started.
        error_traceback (str): Full traceback string captured via traceback.format_exc()
            at the point of failure.
        automation_name (str): Human-readable script name used in the email subject.

    Raises:
        Whatever ``get_env`` raises when ``ALERT_EMAIL`` is not set, and the
            ``KeyboardInterrupt`` caught during step 4 or 6, once the email
            is sent and the note step is over. Nothing else raises.
    """
    alert_email = get_env("ALERT_EMAIL", required=True)
    crashed_at = datetime.now().astimezone()
    timestamp = crashed_at.strftime("%Y-%m-%d %H:%M:%S")
    # Before handle_crash logs anything itself, or the "last log line" would be one of its own.
    log_tail_text = _log_tail_now()
    screenshot_path = None
    dom_path = None
    tab_info = "Browser was not initialized."

    if driver is not None:
        try:
            screenshot_path = os.path.join(
                tempfile.gettempdir(),
                f"{automation_name.replace(' ', '_')}_crash.png"
            )
            driver.save_screenshot(screenshot_path)
            log.info(f"Crash screenshot saved to [cyan]{screenshot_path}[/cyan].")
        except Exception:
            log.warning("Could not capture screenshot.")
            screenshot_path = None

        # Before the tab sweep below, which leaves the driver on the last handle
        # rather than the one that crashed.
        dom_path = _write_dom_file(driver, automation_name, timestamp)

        try:
            tabs = []
            for i, handle in enumerate(driver.window_handles):
                driver.switch_to.window(handle)
                tabs.append(f"Tab {i + 1}: {driver.current_url}")
            tab_info = f"{len(tabs)} tab(s) open at time of crash:\n" + "\n".join(tabs)
        except Exception:
            tab_info = "Could not retrieve tab information."

    # Archive before anything else. Everything below depends on Outlook COM or
    # codex, and neither failing may also erase the evidence of the crash.
    name = fleet_state.automation_name()
    crash_dir = fleet_state.record_crash(
        name,
        traceback_text=error_traceback,
        display_name=automation_name,
        tabs=tab_info,
        screenshot_path=screenshot_path,
        dom_path=dom_path,
    )
    if crash_dir is not None:
        log.info(f"Crash archived to [cyan]{crash_dir}[/cyan].")

    try:
        _kill_own_processes()
    except Exception:
        # The archive is done and the email must still go out; never let cleanup be what fails here.
        log.exception("Cleaning up this automation's processes failed.")

    interrupt: KeyboardInterrupt | None = None
    found_root: list[Path] = []
    try:
        summary, reason = _ai_summary(
            crash_dir, name, automation_name, error_traceback, crashed_at, log_tail_text, found_root
        )
    except KeyboardInterrupt as exc:
        # Ctrl+C during the wait must not cost the crash its email; it is honoured once that is sent.
        interrupt = exc
        summary, reason = None, "interrupted"
    if summary is not None:
        fleet_state.update_crash(crash_dir, ai_summary=summary)
    else:
        fleet_state.update_crash(crash_dir, ai_summary_error=reason)

    try:
        summary_block = _summary_html(summary, reason, timestamp, _crash_step(error_traceback, log_tail_text))
    except Exception as exc:
        log.exception("Could not render the AI summary.")
        summary_block = _summary_html(None, f"could not render the summary: {type(exc).__name__}", timestamp, "")

    # Escaped: a traceback's `in <module>` is otherwise parsed as an HTML tag and vanishes.
    body = f"""
    {summary_block}
    <b>Automation:</b> {automation_name}<br>
    <b>Timestamp:</b> {timestamp}<br><br>
    <b>Open Tabs:</b><br>
    <pre>{html.escape(tab_info)}</pre><br>
    <b>Full Traceback:</b><br>
    <pre>{html.escape(str(error_traceback))}</pre>
    """

    log.info(f"Sending crash report for [cyan]{automation_name}[/cyan].")
    # Attach from the archive, not the temp paths — record_crash moved them.
    attachments = [
        str(crash_dir / filename)
        for filename in ("screenshot.png", "dom.txt")
        if crash_dir is not None and (crash_dir / filename).exists()
    ]
    try:
        outlook.send_email(
            account=alert_email,
            subject=f"[CRASH] {automation_name} — {timestamp}",
            body=body,
            to=[alert_email],
            attachments=attachments,
            show=False,
            send=True,
        )
        fleet_state.mark_crash_emailed(crash_dir)
        log.success("Crash report sent.")
    except Exception:
        # Never let the alert channel be the thing that kills the crash handler:
        # the archive already holds everything.
        log.error("Crash email failed — the crash is archived on disk regardless.")

    try:
        note, note_error = _crash_note(crash_dir, error_traceback, found_root, log_tail_text)
    except KeyboardInterrupt as exc:
        # The email is already out; only the note is given up, and the first Ctrl+C is the one raised.
        if interrupt is None:
            interrupt = exc
        log.warning("Crash note not written: interrupted.")
        note, note_error = None, "interrupted"
    if note is not None:
        fleet_state.update_crash(crash_dir, kb_note=str(note))
    else:
        fleet_state.update_crash(crash_dir, kb_note_error=note_error)

    log.success("Crash handling complete.")
    if interrupt is not None:
        raise interrupt


def _log_tail_now() -> str:
    """The last 50 lines of this process's log file, or ``""`` when they cannot be read.

    The same text fills the prompt's log slot, so it holds as many lines as that slot shows.

    Returns:
        str: :func:`crash_explainer.log_tail` of :func:`crash_explainer.active_log_file`.
    """
    try:
        return crash_explainer.log_tail(crash_explainer.active_log_file())
    except Exception:
        log.debug("Could not read the log tail.", exc_info=True)
        return ""


def _ai_summary(
    crash_dir: Path | None,
    name: str,
    display_name: str,
    traceback_text: str,
    crashed_at: datetime,
    log_text: str,
    found_root: list[Path],
) -> tuple[dict[str, Any] | None, str | None]:
    """Ask the AI model to explain the crash, within one overall deadline. Raises only KeyboardInterrupt.

    Finding the repo, building the prompt and running codex happen in a daemon
    thread, joined for at most ``crash_ai_timeout() + 30`` seconds. codex has
    its own timeout and kill inside that thread; the deadline also covers a
    hang before codex starts (git, a slow disk, the knowledge base).

    Args:
        crash_dir (Path | None): The crash archive, or None when archiving failed.
        name (str): The fleet_state name the crash was archived under.
        display_name (str): Human-readable automation name.
        traceback_text (str): The formatted traceback.
        crashed_at (datetime): When the crash happened, timezone-aware.
        log_text (str): The log tail read before handle_crash logged anything.
        found_root (list[Path]): Receives the crashed repo's checkout once
            this step has found it, for the note step after the email.

    Returns:
        tuple[dict[str, Any] | None, str | None]: ``(summary, None)`` or
            ``(None, reason)``, as :func:`crash_explainer.explain` returns them,
            or ``(None, "timed out after N s")`` when the deadline passes.

    Raises:
        KeyboardInterrupt: Ctrl+C while waiting; ``handle_crash`` handles it.
    """
    if crash_dir is None:
        log.warning("AI summary unavailable: crash archive unavailable.")
        return None, "crash archive unavailable"
    if not crash_explainer.enabled():
        log.info("AI summary skipped: CRASH_AI=0.")
        return None, "disabled (CRASH_AI=0)"
    outcome: list[tuple[dict[str, Any] | None, str | None]] = []

    def work() -> None:
        try:
            repo_root = _repo_root(traceback_text)
            if repo_root is not None:
                found_root.append(repo_root)
            kb_dir = crash_explainer.default_kb_dir()
            # Same rule as the note step: an unusable folder is never touched; the prompt goes without it.
            if kb_dir is not None and _kb_dir_refusal(kb_dir, repo_root):
                kb_dir = None
            prompt = crash_explainer.build_prompt(
                crash_dir,
                name=name,
                display_name=display_name,
                traceback_text=traceback_text,
                crashed_at=crashed_at,
                log_file=crash_explainer.active_log_file(),
                kb_dir=kb_dir,
                crash_root=fleet_state.crash_root(),
                log_text=log_text,
                repo_root=repo_root,
            )
            outcome.append(crash_explainer.explain(prompt, crash_dir / "screenshot.png"))
        except BaseException as exc:  # anything escaping a thread would leave no answer and no reason
            log.exception("The AI summary step failed.")
            outcome.append((None, f"unexpected error: {type(exc).__name__}"))

    log.info("Asking the AI model to explain the crash.")
    started = time.monotonic()
    deadline = crash_explainer.crash_ai_timeout() + _AI_DEADLINE_SLACK
    # Daemon: a worker stuck past the deadline must not keep the crashed process alive.
    worker = threading.Thread(target=work, name="crash-ai-summary", daemon=True)
    try:
        worker.start()
    except RuntimeError as exc:
        log.exception("Could not start the AI summary step.")
        return None, f"unexpected error: {type(exc).__name__}"
    worker.join(deadline)
    elapsed = time.monotonic() - started
    if worker.is_alive():
        log.warning(f"AI summary unavailable: no answer within {deadline} s.")
        return None, f"timed out after {deadline} s"
    summary, reason = outcome[0]
    if summary is None:
        reason = reason or "no reason given"
        log.warning(f"AI summary unavailable after {elapsed:.1f} s: {reason}")
        return None, reason
    log.info(f"AI summary ready in {elapsed:.1f} s.")
    return summary, None


def _repo_root(traceback_text: str) -> Path | None:
    """:func:`crash_explainer.find_repo_root`, or None when it fails. Raises only KeyboardInterrupt.

    Only called inside the AI and note steps' deadline threads, never before
    the email on the main thread: git on this host can outlast its own timeout.
    """
    try:
        return crash_explainer.find_repo_root(traceback_text)
    except Exception:
        log.warning("Could not find the crashed repo from the traceback.", exc_info=True)
        return None


def _crash_note(
    crash_dir: Path | None, traceback_text: str, found_root: list[Path], log_text: str
) -> tuple[Path | None, str | None]:
    """Write the crash's knowledge-base note, within one deadline. Raises only KeyboardInterrupt.

    Finding the vault, finding the repo when the AI step did not, and writing
    the note happen in a daemon thread, joined for at most
    :data:`_NOTE_DEADLINE` seconds. ``CRASH_AI`` is not consulted: the note is
    written by code, and records why there is no summary.

    Args:
        crash_dir (Path | None): The crash archive, or None when archiving failed.
        traceback_text (str): The formatted traceback, to find the repo from.
        found_root (list[Path]): The crashed repo's checkout as the AI step
            found it, or empty when it did not get that far.
        log_text (str): The log tail read before handle_crash logged anything.

    Returns:
        tuple[Path | None, str | None]: ``(note, None)``, or ``(None, reason)``:
            no archive, no knowledge base folder, a folder refused by
            :func:`_kb_dir_refusal`, a refusal or failure from ``crash_notes``,
            or ``"timed out after N s"``.

    Raises:
        KeyboardInterrupt: Ctrl+C while waiting; ``handle_crash`` handles it.
    """
    if crash_dir is None:
        log.warning("Crash note not written: crash archive unavailable.")
        return None, "crash archive unavailable"
    outcome: list[tuple[Path | None, str | None]] = []

    def work() -> None:
        try:
            kb_dir = crash_explainer.default_kb_dir()
            if kb_dir is None:
                outcome.append((None, "no knowledge base folder"))
                return
            repo_root = found_root[0] if found_root else _repo_root(traceback_text)
            if refusal := _kb_dir_refusal(kb_dir, repo_root):
                outcome.append((None, refusal))
                return
            outcome.append(
                crash_notes._write_note(crash_dir, kb_dir=kb_dir, repo_root=repo_root, log_tail_text=log_text)
            )
        except BaseException as exc:  # anything escaping a thread would leave no answer and no reason
            log.exception("The crash note step failed.")
            outcome.append((None, f"unexpected error: {type(exc).__name__}"))

    log.info("Writing the crash note to the knowledge base.")
    worker = threading.Thread(target=work, name="crash-kb-note", daemon=True)
    try:
        worker.start()
    except RuntimeError as exc:
        log.exception("Could not start the crash note step.")
        return None, f"unexpected error: {type(exc).__name__}"
    worker.join(_NOTE_DEADLINE)
    if worker.is_alive():
        log.warning(f"Crash note not written: no answer within {_NOTE_DEADLINE} s.")
        return None, f"timed out after {_NOTE_DEADLINE} s"
    note, reason = outcome[0]
    if note is None:
        reason = reason or "no reason given"
        log.warning(f"Crash note not written: {reason}")
        return None, reason
    log.info(f"Crash note written to [cyan]{note}[/cyan].")
    return note, None


def _kb_dir_refusal(kb_dir: Path, repo_root: Path | None) -> str | None:
    """Why no crash note may be written under ``kb_dir``, or None when one may.

    Args:
        kb_dir (Path): The knowledge base folder, normally from ``CRASH_AI_KB_DIR``.
        repo_root (Path | None): The crashed repo's checkout, when known.

    Returns:
        str | None: ``"knowledge base folder not usable: <why>"`` for a
            network (UNC), relative or inside-the-repo path,
            ``"knowledge base folder not found: <path>"`` when it does not
            exist, or None.
    """
    # Checked as text before any filesystem call: is_dir() on \\host\share already connects to that host.
    if str(kb_dir).replace("/", "\\").startswith("\\\\"):
        return "knowledge base folder not usable: a network (UNC) path"
    if not kb_dir.is_absolute():
        return "knowledge base folder not usable: a relative path"
    # A note inside the crashed repo is one `git add` away from being published with its code.
    resolved = crash_explainer._resolved
    if repo_root is not None and resolved(kb_dir).is_relative_to(resolved(repo_root)):
        return "knowledge base folder not usable: inside the crashed repo"
    # A mistyped CRASH_AI_KB_DIR must not create a stray vault wherever it points.
    if not kb_dir.is_dir():
        return f"knowledge base folder not found: {kb_dir}"
    return None


# Python's own frame line. Only matched inside a traceback's frames section: a multi-line exception
# message can carry a line with the same two-space indent.
_TRACE_FRAME = re.compile(r'  File "([^"\r\n]+)", line (\d+), in (.+)')
# What crash_explainer.log_tail returns when there is no line to show.
_NO_LOG_LINES = ("No log file found.", "The log file is empty.")


def _crash_step(traceback_text: str, log_tail_text: str) -> str:
    """Which step the run was on, taken from the traceback and the log, never from the model.

    Args:
        traceback_text (str): The formatted traceback.
        log_tail_text (str): The last log lines, read before handle_crash logged anything.

    Returns:
        str: "<func> in run_x.py, line N" for the innermost frame in a
            ``run_*.py`` file within the last traceback section (the exception
            that actually ended the run), then "Last log line: <line>" when
            there is one, one per line; "Unknown" when neither is available.
    """
    parts: list[str] = []
    sections = crash_explainer._traceback_sections(traceback_text)
    run_frames = [
        match for line in (sections[-1] if sections else [])
        if (match := _TRACE_FRAME.fullmatch(line)) and _is_run_script(match.group(1))
    ]
    if run_frames:
        path, line, func = run_frames[-1].groups()
        parts.append(f"{func.strip()} in {PureWindowsPath(path).name}, line {line}")
    log_lines = [text.strip() for text in log_tail_text.splitlines() if text.strip()]
    if log_lines and log_lines[-1] not in _NO_LOG_LINES:
        parts.append(f"Last log line: {log_lines[-1]}")
    return "\n".join(parts) or "Unknown"


def _is_run_script(path: str) -> bool:
    """Whether a frame's file is an automation entry script (``run_*.py``)."""
    filename = PureWindowsPath(path).name.lower()
    return filename.startswith("run_") and filename.endswith(".py")


_RERUN = {"yes": "Yes", "yes_after_check": "Yes, after the check below", "no": "No"}
_RERUN_COLORS = {
    "yes": ("#dcfce7", "#166534"),
    "yes_after_check": ("#fef3c7", "#92400e"),
    "no": ("#fee2e2", "#991b1b"),
}
# Outlook renders HTML through Word, which ignores most margins: spacing comes from empty
# zero-margin paragraphs, and every paragraph is zeroed so Word adds none of its own.
_FONT = "font-family:Calibri,Arial,sans-serif;font-size:11pt;"
_P = "margin:0;"
_CELL = "padding:6px 10px;border:1px solid #e5e7eb;vertical-align:top;"
_GAP = f'<p style="{_P}">&nbsp;</p>'
# Not <hr>: the mail server's signature stamp reads <hr> as the start of a quoted reply and
# puts the signature above it, in the middle of the crash report.
_RULE = '<table cellpadding="0" cellspacing="0" width="100%"><tr><td style="border-bottom:1px solid #9ca3af;font-size:1px">&nbsp;</td></tr></table>'


_WWW = re.compile(r"(www)\.", re.I)


def _html_text(value: object) -> str:
    """Escape text for the email body, keeping its line breaks."""
    return html.escape(str(value)).replace("\r\n", "\n").replace("\n", "<br>")


def _model_html(value: object) -> str:
    """:func:`_html_text` with URLs broken (``[:]//``, ``www[.]``) so mail clients cannot auto-link them.

    The model read scraped page text, so a link in its answer may be one a page planted.
    """
    return _WWW.sub(r"\1[.]", _html_text(value).replace("://", "[:]//"))


def _summary_html(summary: dict[str, Any] | None, reason: str | None, timestamp: str, step: str) -> str:
    """The block that sits above the traceback in the crash email.

    Every model-derived string and the reason are escaped here: the model read
    scraped page text, and a reason may quote codex's own error output. Model
    text and the reason also have their URLs broken (:func:`_model_html`).

    Args:
        summary (dict[str, Any] | None): An answer matching
            :data:`crash_explainer.RESPONSE_SCHEMA`, or None when there is none.
        reason (str | None): Why there is no summary.
        timestamp (str): The crash timestamp, as in the subject.
        step (str): :func:`_crash_step` output.

    Returns:
        str: Inline HTML: a header bar, an at-a-glance table (rerun badge,
            confidence, when, which step), one titled section per answer field,
            and a rule that separates them from the report below; or the one
            "AI summary unavailable" line.

    Raises:
        KeyError: When ``summary`` lacks a schema field; ``handle_crash``
            falls back to the reason line.
    """
    if summary is None:
        return (
            f'<p style="{_P}background:#fef3c7;color:#92400e;padding:6px 10px">'
            f"AI summary unavailable: {_model_html(reason or 'no reason given')}</p>{_GAP}"
        )

    fix = summary["technical_fix"]
    where = ""
    if fix["file"]:
        where = fix["file"] if fix["line"] is None else f"{fix['file']}, line {fix['line']}"
    rerun = summary["safe_to_rerun"]
    background, color = _RERUN_COLORS.get(rerun, ("#e5e7eb", "#374151"))
    badge = (
        f'<span style="background:{background};color:{color};padding:2px 8px">'
        f"<b>{_model_html(_RERUN.get(rerun, rerun))}</b></span>"
    )
    glance = (
        ("Safe to rerun now? (AI's view)", badge),
        ("Confidence", _model_html(str(summary["confidence"]).capitalize())),
        ("When", _html_text(timestamp)),
        ("Which step", _html_text(step)),
    )
    glance_rows = "".join(
        f'<tr><td style="{_CELL}background:#f3f4f6;width:220px"><b>{label}</b></td>'
        f'<td style="{_CELL}">{value}</td></tr>'
        for label, value in glance
    )
    technical = _model_html(fix["change"])
    if where:
        technical = f'<span style="font-family:Consolas,monospace">{_model_html(where)}</span><br>{technical}'
    sections = (
        ("What happened", _model_html(summary["what_happened"])),
        ("Why", _model_html(summary["why"])),
        ("How to fix", _model_html(summary["how_to_fix"])),
        ("Why a rerun is or is not safe", _model_html(summary["safe_to_rerun_reason"])),
        ("Technical fix", technical),
    )
    body = "".join(
        f'<p style="{_P}font-size:12pt;color:#1f2937"><b>{title}</b></p><p style="{_P}">{text}</p>{_GAP}'
        for title, text in sections
    )
    return (
        f'<table cellpadding="0" cellspacing="0" width="100%" style="border-collapse:collapse;{_FONT}">'
        f'<tr><td style="background:#1f2937;color:#ffffff;padding:10px 14px">'
        f'<span style="font-size:14pt"><b>What happened, in plain English</b></span><br>'
        f'<span style="font-size:9pt;color:#d1d5db">AI-generated. Check it before acting on it.</span>'
        f"</td></tr></table>{_GAP}"
        f'<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;{_FONT}">{glance_rows}</table>{_GAP}'
        f'<div style="{_FONT}">{body}</div>{_RULE}{_GAP}'
    )


# Browser-side executables a fleet automation starts itself. SeleniumBase launches its driver
# (uc_driver in UC mode, chromedriver otherwise) as a direct child of Python; in UC mode Chrome is
# a direct child too, otherwise Chrome is the driver's child and is reached by the tree walk.
_OWN_BROWSER_EXES = frozenset({"chrome.exe", "chromedriver.exe", "uc_driver.exe"})


def _kill_own_processes() -> None:
    """Kill the Chrome/driver trees and Excel instances this process started, and nothing else.

    It used to ``taskkill /im`` every Excel, Chrome and ChromeDriver on the machine. So one
    automation's crash also killed any other automation running at the time: on 2026-09-09 and
    2026-09-14 a second automation failed minutes after another crashed, with "invalid session
    id: the browser has closed the connection". It also closed the user's own Chrome.

    The browser-side processes this process started are its direct children, each killed with
    its verified descendants. COM-launched Excel is not a child of Python, so only the
    instances this process started through ``excel_utils`` are killed. Every kill goes through
    a handle checked to be the same process (pid + creation time).
    """
    log.info("Killing this automation's own browser and Excel processes.")
    me = os.getpid()
    my_birth = _winproc.creation_time(me)
    if my_birth is not None:
        table = _winproc.process_table()
        for pid, ppid, exe in table:
            if ppid != me or pid == me or exe.lower() not in _OWN_BROWSER_EXES:
                continue
            born = _winproc.creation_time(pid)
            # Windows records a parent pid once and never re-checks it. A process whose real
            # parent exited (e.g. the user's Chrome after "Relaunch to update") looks like our
            # child if Windows later gave us that pid. A real child is younger than us.
            if born is None or born <= my_birth:
                continue
            # Not `taskkill /t`: it walks descendants by recorded parent pid alone, so a stale
            # branch anywhere under our Chrome would be killed. Each descendant is kept only if
            # it is younger than its parent. Each kill goes through a handle verified to be the
            # same process (pid + creation time), leaves first.
            for child, child_exe, child_born in reversed(_winproc.verified_descendants(pid, table)):
                _winproc.terminate(child, child_born, f"({child_exe})")
            _winproc.terminate(pid, born, f"({exe})")
    for pid, created in excel_utils.started_excel():
        _winproc.terminate(pid, created, "(EXCEL.EXE)")
