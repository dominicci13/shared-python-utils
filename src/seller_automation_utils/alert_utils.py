from __future__ import annotations

import os
import tempfile
from datetime import datetime
from seller_automation_utils import _winproc, excel_utils, fleet_state, outlook
from seller_automation_utils.config_utils import get_env
import logging

log = logging.getLogger(__name__)

# Outlook rejects oversized attachments, and a runaway page should never be the reason
# a crash alert fails to send.
MAX_DOM_CHARS = 8_000_000

# Frames nested deeper than this are almost always ad/tracking chrome, not the app.
MAX_FRAME_DEPTH = 3


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
    """Handle a script crash: capture browser state, archive it, alert, and clean up.

    Takes a screenshot of the current browser window, saves the live DOM (main
    document plus every iframe) to a text file, collects all open tab URLs,
    **archives all of it to disk**, sends a detailed crash report via Outlook,
    then force-kills the Chrome, driver and Excel processes **this automation** started
    (1.8.8+; see ``_kill_own_processes``). Other automations' processes, and the user's own
    Chrome and Excel, are left alone.

    The archive is written *before* the email is attempted, and the screenshot
    and DOM are moved into it rather than deleted. The email goes out over
    Outlook COM, so a broken Outlook used to lose the traceback outright — the
    one failure that most needs reporting. The artifacts also used to survive
    only as mail attachments, which put them out of reach of the
    ``fleet-control`` dashboard and of any later debugging session.

    If the driver was never initialized (e.g., Chrome failed to launch), the
    function skips the screenshot, DOM capture, and tab collection, and sends the
    email without attachments.

    Args:
        driver (object | None): Active SeleniumBase WebDriver instance, or None if
            the browser was never successfully started.
        error_traceback (str): Full traceback string captured via traceback.format_exc()
            at the point of failure.
        automation_name (str): Human-readable script name used in the email subject.
    """
    alert_email = get_env("ALERT_EMAIL", required=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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

    # Archive before emailing. Everything below this point depends on Outlook
    # COM, and a broken Outlook must not also erase the evidence of the crash.
    crash_dir = fleet_state.record_crash(
        fleet_state.automation_name(),
        traceback_text=error_traceback,
        display_name=automation_name,
        tabs=tab_info,
        screenshot_path=screenshot_path,
        dom_path=dom_path,
    )
    if crash_dir is not None:
        log.info(f"Crash archived to [cyan]{crash_dir}[/cyan].")

    body = f"""
    <b>Automation:</b> {automation_name}<br>
    <b>Timestamp:</b> {timestamp}<br><br>
    <b>Open Tabs:</b><br>
    <pre>{tab_info}</pre><br>
    <b>Full Traceback:</b><br>
    <pre>{error_traceback}</pre>
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
        # the archive already holds everything, and the cleanup below still
        # needs to run.
        log.error("Crash email failed — the crash is archived on disk regardless.")

    try:
        _kill_own_processes()
    except Exception:
        # The archive and the email are already done; never let cleanup be what fails here.
        log.exception("Cleaning up this automation's processes failed.")

    log.success("Crash handling complete.")


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
