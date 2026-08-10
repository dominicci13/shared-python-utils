from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException
from selenium.webdriver.support import expected_conditions as EC
from seller_automation_utils import outlook
from seller_automation_utils.config_utils import get_env, load_config_safe
import logging

log = logging.getLogger(__name__)


def _accounts_config_path() -> Path:
    """Locate ``config/accounts.json`` relative to the entry script.

    Resolving against ``sys.argv[0]`` rather than ``Path.cwd()`` keeps the
    lookup correct even when the script is launched from a different working
    directory (e.g. via Task Scheduler or a parent shell). Falls back to the
    CWD-relative path if the entry script can't be resolved or its sibling
    ``config/`` directory does not exist.

    Returns:
        Path: Resolved path to ``config/accounts.json``.
    """
    if sys.argv and sys.argv[0]:
        script_dir = Path(sys.argv[0]).resolve().parent
        candidate = script_dir / "config" / "accounts.json"
        if candidate.exists():
            return candidate
    return Path.cwd() / "config" / "accounts.json"


_accounts = load_config_safe(_accounts_config_path())

AMAZON_ACCOUNT_NAMES: dict[str, str] = _accounts.get("amazon_account_names", {})
AMAZON_URLS: dict[str, str] = _accounts.get("amazon_urls", {})
EBAY_PROFILES: dict[str, str] = _accounts.get("ebay_profiles", {})


def iter_amazon_accounts() -> Iterator[tuple[str, str, str]]:
    """Iterate over configured Amazon accounts.

    Joins ``AMAZON_URLS`` and ``AMAZON_ACCOUNT_NAMES`` so callers don't repeat
    the lookup pattern ``for account, url in AMAZON_URLS.items(): root = AMAZON_ACCOUNT_NAMES[account]``.

    Yields:
        tuple[str, str, str]: ``(account_key, display_name, url)`` for each
            configured Amazon account.
    """
    for key, url in AMAZON_URLS.items():
        yield key, AMAZON_ACCOUNT_NAMES[key], url

##################################################################################################################################################
def sellercloud(driver: object, username: str, password: str, site: str = "Delta") -> None:
    """Log in to SellerCloud (Delta or Alpha).

    Args:
        driver (object): Active SeleniumBase WebDriver instance.
        username (str): SellerCloud login email.
        password (str): SellerCloud login password.
        site (str): SellerCloud environment — "Delta" or "Alpha". Defaults to "Delta".
    """
    log.info("Logging into [cyan]SellerCloud[/cyan].")

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
def amazon_login(
    driver: object,
    email: str,
    username: str,
    password: str,
    retry_url: str | None = None,
) -> str | None:
    """Log in to Amazon Seller Central and complete OTP two-factor authentication.

    Polls the given Outlook inbox for an "Amazon OTP" email, enters the code into
    the MFA field, and returns the code on success. If the OTP email does not
    arrive within the per-attempt timeout and `retry_url` is provided, re-navigates
    to `retry_url` and tries again up to 5 attempts total.

    Args:
        driver (object): Active SeleniumBase WebDriver instance.
        email (str): Outlook inbox address to poll for the OTP email.
        username (str): Amazon Seller Central login email.
        password (str): Amazon Seller Central login password.
        retry_url (str | None): URL to navigate to before each retry, typically
            the account's Seller Central URL. If None, the function makes a single
            attempt with no retries. Defaults to None.

    Returns:
        str | None: The OTP code used to complete login, or None if no OTP
            arrived after all attempts.

    Raises:
        TimeoutException: If the OTP input field does not appear on the page.
    """
    max_attempts = 5 if retry_url else 1
    for attempt in range(max_attempts):
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
            log.info("Seller Central logged out. Logging in.")
            pass_input.send_keys(password)
            pass_input.send_keys(Keys.ENTER)
        except TimeoutException:
            pass

        code_input = WebDriverWait(driver, 5).until(EC.presence_of_element_located((
            By.CSS_SELECTOR, "#auth-mfa-otpcode"
        )))

        log.info("Waiting for the OTP verification code.")
        code = outlook.get_verification_code(
            account=email,
            sender_contains=email,
            subject_contains="Amazon OTP",
            timeout_sec=60,
            body_extractor=lambda body: body.split(" ")[0],
            consume=True,
        )

        if code:
            code_input.send_keys(code)
            code_input.send_keys(Keys.ENTER)
            log.success("Logged in to Seller Central successfully.")
            return code

        if attempt < max_attempts - 1:
            log.error("Failed to log in to Amazon. Trying again.")
            driver.get(retry_url)
            driver.switch_to_window(0)

    return None

EBAY_CAPTCHA_MARKER = "/splashui/captcha"
EBAY_SIGNIN_MARKER = "signin.ebay.com"

# eBay's step-1 username field. The id has been `userid` for years, but the
# two-step flow could not be captured live (every automated hit on the sign-in
# page currently lands on a captcha), so this is a best-known list rather than a
# verified selector — hence the fall-through and the explicit error below.
_USERNAME_LOCATORS = (
    (By.ID, "userid"),
    (By.CSS_SELECTOR, "input[name='userid']"),
    (By.CSS_SELECTOR, "input[autocomplete='username']"),
)


def _visible_username_field(driver: object) -> object | None:
    """Return eBay's step-1 username input if one is on screen.

    Returns:
        object | None: The first displayed username input, or None when the page
            is not showing the username step.
    """
    for by, selector in _USERNAME_LOCATORS:
        for element in driver.find_elements(by, selector):
            try:
                if element.is_displayed():
                    return element
            except StaleElementReferenceException:
                continue
    return None


def _type_and_submit(element: object, value: str) -> None:
    """Clear a field, type into it, and submit with Enter.

    Enter is used rather than clicking a submit button because eBay renames the
    button between flows while the form has always submitted on Enter.

    Args:
        element (object): The input to fill.
        value (str): Text to enter.
    """
    element.send_keys(Keys.CONTROL + "a")
    element.send_keys(Keys.DELETE)
    element.send_keys(value)
    element.send_keys(Keys.ENTER)


##################################################################################################################################################
def ebay(password: str, driver: object, username: str | None = None) -> None:
    """Log in to an eBay seller account, handling the one- and two-step forms.

    eBay serves two sign-in layouts. When the profile still knows the user it
    asks for the password alone; otherwise it asks for the username first and
    only renders the password field after that is submitted. In the two-step
    layout ``#pass`` is *present but hidden* from the start, so the previous
    `presence_of_element_located` wait resolved immediately and `send_keys`
    raised `ElementNotInteractableException` — this waits for it to be
    **clickable** instead.

    Args:
        password (str): eBay account password.
        driver (object): Active SeleniumBase WebDriver instance.
        username (str | None): Username for the two-step form. Falls back to the
            ``eBay_user`` environment variable. Only needed when the profile's
            session has lapsed far enough that eBay asks who is signing in.

    Raises:
        RuntimeError: If eBay served a captcha, if the username step is showing
            and no username is configured, or if the password field never
            becomes usable.
    """
    if EBAY_CAPTCHA_MARKER in driver.current_url:
        raise RuntimeError(
            "eBay served a bot check instead of the sign-in form. Sign in by hand "
            "in this Chrome profile, then re-run; retrying automatically makes it worse."
        )

    # The common case: the Chrome profile's session is still good and eBay never
    # redirected us. Returning here keeps a healthy run from waiting out the
    # password timeout — and from raising on a page that has no login form at all.
    if EBAY_SIGNIN_MARKER not in driver.current_url:
        log.info("eBay session still active — no sign-in needed.")
        return

    log.info("Logging into [cyan]eBay[/cyan].")

    username_field = _visible_username_field(driver)
    if username_field is not None:
        username = username or get_env("eBay_user", default="")
        if not username:
            raise RuntimeError(
                "eBay is asking for the username and none is configured. Set "
                "`eBay_user` in the repo's .env, or sign this Chrome profile in by hand."
            )
        log.info("eBay asked for the username first — submitting it.")
        _type_and_submit(username_field, username)

    try:
        pass_input = WebDriverWait(driver, 20).until(EC.element_to_be_clickable((
            By.ID,
            "pass"
        )))
    except TimeoutException as exc:
        if EBAY_CAPTCHA_MARKER in driver.current_url:
            raise RuntimeError("eBay served a bot check during sign-in.") from exc
        raise RuntimeError(
            f"eBay's password field never became usable. Current URL: {driver.current_url}"
        ) from exc

    _type_and_submit(pass_input, password)

    log.success("Logged in to [cyan]eBay[/cyan] successfully.")
