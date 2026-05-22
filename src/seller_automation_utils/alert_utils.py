from __future__ import annotations

import os
import tempfile
from datetime import datetime
from seller_automation_utils import outlook, custom_functions
from seller_automation_utils.config_utils import get_env
import logging

log = logging.getLogger(__name__)


def handle_crash(driver: object | None, error_traceback: str, automation_name: str) -> None:
    """Handle a script crash: capture browser state, send an alert email, and clean up processes.

    Takes a screenshot of the current browser window, collects all open tab URLs,
    sends a detailed crash report via Outlook, deletes the temporary screenshot,
    then forcefully kills Excel, Chrome, and ChromeDriver processes.

    If the driver was never initialized (e.g., Chrome failed to launch), the
    function skips the screenshot and tab collection and sends the email without
    an attachment.

    Args:
        driver (object | None): Active SeleniumBase WebDriver instance, or None if
            the browser was never successfully started.
        error_traceback (str): Full traceback string captured via traceback.format_exc()
            at the point of failure.
        automation_name (str): Human-readable script name used in the email subject.
    """
    alert_email = get_env("ALERT_EMAIL", required=True)
    screenshot_path = None
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

        try:
            tabs = []
            for i, handle in enumerate(driver.window_handles):
                driver.switch_to.window(handle)
                tabs.append(f"Tab {i + 1}: {driver.current_url}")
            tab_info = f"{len(tabs)} tab(s) open at time of crash:\n" + "\n".join(tabs)
        except Exception:
            tab_info = "Could not retrieve tab information."

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    body = f"""
    <b>Automation:</b> {automation_name}<br>
    <b>Timestamp:</b> {timestamp}<br><br>
    <b>Open Tabs:</b><br>
    <pre>{tab_info}</pre><br>
    <b>Full Traceback:</b><br>
    <pre>{error_traceback}</pre>
    """

    log.info(f"Sending crash report for [cyan]{automation_name}[/cyan].")
    attachments = [screenshot_path] if screenshot_path else []
    outlook.send_email(
        account=alert_email,
        subject=f"[CRASH] {automation_name} — {timestamp}",
        body=body,
        to=[alert_email],
        attachments=attachments,
        show=False,
        send=True,
    )

    if screenshot_path and os.path.exists(screenshot_path):
        os.remove(screenshot_path)
        log.info("Temporary screenshot deleted.")

    log.info("Killing automation processes.")
    for process in ["excel", "chrome", "chromedriver"]:
        custom_functions.kill_app(process)

    log.success("Crash report sent and processes cleaned up.")
