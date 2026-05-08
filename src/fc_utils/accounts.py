from __future__ import annotations

import time
from rich import print
from pathlib import Path
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support import expected_conditions as EC
from fc_utils import outlook
from fc_utils.config_utils import get_env, load_config_safe

_accounts = load_config_safe(Path.cwd() / "config" / "accounts.json")

AMAZON_ACCOUNT_NAMES: dict[str, str] = _accounts.get("amazon_account_names", {})
AMAZON_URLS: dict[str, str] = _accounts.get("amazon_urls", {})
EBAY_PROFILES: dict[str, str] = _accounts.get("ebay_profiles", {})

##################################################################################################################################################
def sellercloud(driver: object, username: str, password: str, site: str = "Delta") -> None:
    """Log in to SellerCloud (Delta or Alpha).

    Args:
        driver (object): Active SeleniumBase WebDriver instance.
        username (str): SellerCloud login email.
        password (str): SellerCloud login password.
        site (str): SellerCloud environment — "Delta" or "Alpha". Defaults to "Delta".
    """
    print("[cyan][INFO][/cyan] Logging into [bold]SellerCloud[/bold].")

    if site == "Delta":
        url = get_env("SELLERCLOUD_DELTA_URL", required=True)
        driver.get(url)

        try:
            UserBox = WebDriverWait(driver, 5).until(EC.presence_of_element_located((
                By.ID,
                "NewFormBody_deltaUsername"
            )))
            UserBox.send_keys(username)

            PasswordBox = driver.find_element(By.ID, "NewFormBody_deltaPass")
            PasswordBox.send_keys(password)

            driver.find_element(By.CLASS_NAME, "wizard-btn-container").click()
        except TimeoutException:
            pass

    elif site == "Alpha":
        url = get_env("SELLERCLOUD_ALPHA_URL", required=True)
        driver.get(url)

        try:
            UserBox = WebDriverWait(driver, 5).until(EC.presence_of_element_located((
                By.CSS_SELECTOR,
                "#ContentPlaceHolder1_txtEmail"
            )))
            UserBox.send_keys(username)

            PasswordBox = driver.find_element(By.CSS_SELECTOR, "#ContentPlaceHolder1_txtPwd")
            PasswordBox.send_keys(password)
            PasswordBox.send_keys(Keys.ENTER)
        except TimeoutException:
            pass

##################################################################################################################################################
def amazon_login(driver: object, email: str, username: str, password: str) -> str | None:
    """Log in to Amazon Seller Central and complete OTP two-factor authentication.

    Polls the given Outlook inbox for an "Amazon OTP" email, enters the code into
    the MFA field, and returns the code on success.

    Args:
        driver (object): Active SeleniumBase WebDriver instance.
        email (str): Outlook inbox address to poll for the OTP email.
        username (str): Amazon Seller Central login email.
        password (str): Amazon Seller Central login password.

    Returns:
        str | None: The OTP code used to complete login, or None if retries exceeded.
    """
    try:
        email_input = WebDriverWait(driver, 5).until(EC.presence_of_element_located((
            By.CSS_SELECTOR, "#ap_email"
        )))
        email_input.send_keys(username)
        email_input.send_keys(Keys.ENTER)
    except TimeoutException:
        pass

    try:
        pass_input = WebDriverWait(driver, 5).until(EC.presence_of_element_located((
            By.CSS_SELECTOR, "#ap_password"
        )))
        print("[cyan][INFO][/cyan] Seller Central logged out. Logging in.")
        pass_input.send_keys(password)
        pass_input.send_keys(Keys.ENTER)
    except TimeoutException:
        pass

    code_input = WebDriverWait(driver, 5).until(EC.presence_of_element_located((
        By.CSS_SELECTOR, "#auth-mfa-otpcode"
    )))

    # Open the inbox once and reuse it across all retry attempts.
    inbox = outlook.get_account(email, "Inbox")
    code = None

    for _ in range(6):
        inbox.Sort("[ReceivedTime]", True)
        for msg in inbox:
            if msg.Subject == "Amazon OTP":
                msg.Unread = False
                code = msg.Body.split(" ")[0]
                msg.Delete()
                break
        if code:
            break
        print("[cyan][INFO][/cyan] Waiting for the OTP verification code.")
        time.sleep(10)

    if not code:
        return None

    code_input.send_keys(code)
    code_input.send_keys(Keys.ENTER)

    print("[green][SUCCESS][/green] Logged in to Seller Central successfully.")
    return code

##################################################################################################################################################
def ebay(password: str, driver: object) -> None:
    """Log in to an eBay seller account.

    Waits for the password field to be present (up to 15 s), clears it,
    enters the password, and submits the form.

    Args:
        password (str): eBay account password.
        driver (object): Active SeleniumBase WebDriver instance.
    """
    pass_input = WebDriverWait(driver, 15).until(EC.presence_of_element_located((
        By.ID,
        "pass"
    )))
    print("[cyan][INFO][/cyan] Logging into [bold]eBay[/bold].")

    # Clear and enter password
    pass_input.send_keys(Keys.CONTROL + "a")
    pass_input.send_keys(Keys.DELETE)
    pass_input.send_keys(password)
    pass_input.send_keys(Keys.ENTER)

    print("[green][SUCCESS][/green] Logged in to [bold]eBay[/bold] successfully.")