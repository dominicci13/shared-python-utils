from __future__ import annotations

import time

from seleniumbase import Driver
from selenium.common.exceptions import SessionNotCreatedException

from fc_utils.custom_functions import kill_app
import logging

log = logging.getLogger(__name__)


def start_browser(
    user_data_dir: str,
    chrome_profile: str = "Default",
    headless: bool = True,
    retry_count: int = 3,
) -> object:
    """Launch a SeleniumBase Chrome instance, retrying automatically on failure.

    Uses a persistent Chrome user profile. If the browser fails to start (e.g., a
    stale Chrome process is blocking the session), kills existing Chrome instances
    and retries after a short delay.

    Args:
        user_data_dir (str): Full path to the Chrome User Data directory.
            Windows: "C:/Users/<name>/AppData/Local/Google/Chrome/User Data"
            macOS:   "/Users/<name>/Library/Application Support/Google/Chrome"
        chrome_profile (str): Profile folder name inside user_data_dir
            (e.g., "Default", "Profile 1", "Profile 2"). Defaults to "Default".
        headless (bool): Run Chrome without a visible UI window. Defaults to True.
        retry_count (int): Maximum number of retry attempts after the first failure.
            Defaults to 3.

    Returns:
        object: SeleniumBase Driver instance ready for automation.

    Raises:
        RuntimeError: If Chrome fails to launch after all retry attempts are exhausted.
    """
    last_error: Exception | None = None

    for attempt in range(retry_count + 1):
        log.info(f"Launching Chrome (profile: [cyan]{chrome_profile}[/cyan], headless: {headless}).")
        try:
            driver = Driver(
                uc=True,
                user_data_dir=user_data_dir,
                chromium_arg=f"--profile-directory={chrome_profile}",
                headless=headless,
            )
            if not headless:
                driver.maximize_window()
            log.success("Chrome launched successfully.")
            return driver
        except (SessionNotCreatedException, RuntimeError) as exc:
            last_error = exc
            log.error(f"Failed to launch Chrome (attempt {attempt + 1}/{retry_count + 1}). Killing existing instances.")
            kill_app("chrome")
            time.sleep(5)

    raise RuntimeError(f"Chrome failed to launch after {retry_count + 1} attempts.") from last_error
