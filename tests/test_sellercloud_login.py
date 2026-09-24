"""Unit tests for `seller_automation_utils.accounts.sellercloud` (Delta).

Measured against the live tenant on 2026-09-23: an unauthenticated profile lands on
`/account/login.aspx` with the form showing. A signed-in page (home `/`, or any inner page) carries
a `logout.aspx` link, and the login page does not. The old helper never checked that the login
took. Any failure read as success, and callers that wrapped it in a bare `except` logged "already
logged in", so a run could collect nothing without anyone noticing.

"Signed in" now needs positive proof: https, the tenant's own host, a path that is not the login
page, and the logout link. The tests drive a stub driver, so no browser is needed. Each of those
checks is pinned by at least one test, so reordering or dropping one fails the suite.
"""
from __future__ import annotations

import pytest
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.keys import Keys

from seller_automation_utils import accounts

ROOT = "https://tenant.example.com/"
LOGIN = "https://tenant.example.com/account/login.aspx?ReturnUrl=%2f"
HOME = "https://tenant.example.com/"


class FakeField:
    def __init__(self, driver: "FakeDriver | None" = None) -> None:
        self.driver = driver
        self.keys: list[str] = []

    def send_keys(self, value: str) -> None:
        self.keys.append(value)
        if value == Keys.ENTER and self.driver is not None:
            self.driver.submit()

    @property
    def typed(self) -> str:
        return "".join(k for k in self.keys if k != Keys.ENTER)


class FakeButton:
    def __init__(self, driver: "FakeDriver") -> None:
        self.driver = driver
        self.clicked = False

    def click(self) -> None:
        self.clicked = True
        self.driver.submit()


class FakeDriver:
    """`lands_on` is where the tenant root sends us; `after_submit` is where a submit goes.

    `signed_in` decides whether the page carries the logout link. It becomes True on submit
    only when `submit_signs_in` is set.
    """

    def __init__(self, lands_on: str = LOGIN, form: bool = True, button: bool = True,
                 signed_in: bool = False, after_submit: str = HOME,
                 submit_signs_in: bool = True) -> None:
        self.lands_on = lands_on
        self.current_url = "about:blank"
        self.form = form
        self.signed_in = signed_in
        self.after_submit = after_submit
        self.submit_signs_in = submit_signs_in
        self.user = FakeField()
        self.password = FakeField(self)
        self.button = FakeButton(self) if button else None
        self.visited: list[str] = []

    def submit(self) -> None:
        self.current_url = self.after_submit
        self.signed_in = self.submit_signs_in
        self.form = False

    def get(self, url: str) -> None:
        self.visited.append(url)
        self.current_url = self.lands_on

    def find_element(self, by: object, selector: str) -> FakeField:
        assert selector == "NewFormBody_deltaPass"
        return self.password

    def find_elements(self, by: object, selector: str) -> list:
        if selector == "NewFormBody_deltaUsername":
            return [self.user] if self.form else []
        if "logout.aspx" in selector:
            return [object()] if self.signed_in else []
        if "wizard-btn-container" in selector:
            return [self.button] if self.button else []
        raise AssertionError(f"unexpected selector {selector!r}")


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):
    """Resolve waits against the stub: a falsy condition is a timeout, as in Selenium."""
    monkeypatch.setenv("SELLERCLOUD_DELTA_URL", ROOT)

    class FakeWait:
        def __init__(self, driver: FakeDriver, timeout: int, ignored_exceptions=None) -> None:
            self.driver = driver

        def until(self, condition):
            result = condition(self.driver)
            if not result:
                raise TimeoutException("condition never met")
            return result

    monkeypatch.setattr(accounts, "WebDriverWait", FakeWait)


# --- the measured happy paths ------------------------------------------------------------------

def test_login_types_credentials_clicks_submit_and_confirms(patched) -> None:
    driver = FakeDriver()

    accounts.sellercloud(driver, "user@example.com", "hunter2")

    assert driver.visited == [ROOT]
    assert driver.user.typed == "user@example.com"
    assert driver.password.typed == "hunter2"
    assert driver.button.clicked


def test_missing_submit_button_falls_back_to_enter(patched) -> None:
    """Don't depend on one class name. If the button is gone, Enter still submits."""
    driver = FakeDriver(button=False)

    accounts.sellercloud(driver, "user@example.com", "hunter2")

    assert Keys.ENTER in driver.password.keys


def test_live_session_returns_without_touching_the_form(patched) -> None:
    driver = FakeDriver(lands_on=HOME, form=False, signed_in=True)

    accounts.sellercloud(driver, "user@example.com", "hunter2")

    assert driver.user.keys == [] and driver.button.clicked is False


def test_success_url_whose_query_mentions_the_login_page_is_still_success(patched) -> None:
    """Only the path decides "on the login page", never the query string."""
    driver = FakeDriver(after_submit="https://tenant.example.com/?ReturnUrl=/Account/Login.aspx")

    accounts.sellercloud(driver, "user@example.com", "hunter2")


# --- a login that did not take must raise --------------------------------------------------------

def test_rejected_login_raises(patched) -> None:
    driver = FakeDriver(after_submit=LOGIN, submit_signs_in=False)

    with pytest.raises(RuntimeError, match="did not go through"):
        accounts.sellercloud(driver, "user@example.com", "wrong")


def test_off_login_page_without_logout_link_raises(patched) -> None:
    """E.g. a password-expiry or 2FA page at another path on the same host."""
    driver = FakeDriver(after_submit="https://tenant.example.com/account/ChangePassword.aspx",
                        submit_signs_in=False)

    with pytest.raises(RuntimeError, match="did not go through"):
        accounts.sellercloud(driver, "user@example.com", "hunter2")


def test_submit_landing_on_a_foreign_host_raises(patched) -> None:
    driver = FakeDriver(after_submit="https://elsewhere.example.net/", submit_signs_in=True)

    with pytest.raises(RuntimeError, match="did not go through"):
        accounts.sellercloud(driver, "user@example.com", "hunter2")


def test_errors_never_carry_credentials(patched) -> None:
    driver = FakeDriver(after_submit=LOGIN + "&x=1", submit_signs_in=False)

    with pytest.raises(RuntimeError) as info:
        accounts.sellercloud(driver, "user@example.com", "hunter2")

    assert "hunter2" not in str(info.value)
    assert "user@example.com" not in str(info.value)
    assert "ReturnUrl" not in str(info.value)  # query strings are dropped from the message


# --- "no form" is only a live session with positive proof -------------------------------------

@pytest.mark.parametrize("landing", [
    "about:blank",
    "chrome-error://chromewebdata/",
    "https://elsewhere.example.net/",
    "http://tenant.example.com/",                       # not https
    "https://tenant.example.com/maintenance.aspx",      # our host, but no logout link
    "https://tenant.example.com/Account/Login.aspx",    # login page, form never rendered
])
def test_no_form_without_proof_of_a_session_raises(patched, landing: str) -> None:
    driver = FakeDriver(lands_on=landing, form=False, signed_in=False)

    with pytest.raises(RuntimeError, match="neither its login form nor a signed-in page"):
        accounts.sellercloud(driver, "user@example.com", "hunter2")


def test_logout_link_on_a_foreign_host_is_not_a_session(patched) -> None:
    driver = FakeDriver(lands_on="https://elsewhere.example.net/", form=False, signed_in=True)

    with pytest.raises(RuntimeError, match="neither its login form nor a signed-in page"):
        accounts.sellercloud(driver, "user@example.com", "hunter2")

# --- each check pinned: credentials only ever go to the tenant's own https login form ----------

@pytest.mark.parametrize("landing", [
    "http://tenant.example.com/account/login.aspx",         # our host, but plaintext
    "https://elsewhere.example.net/account/login.aspx",     # same form, foreign host
])
def test_form_off_our_https_host_never_receives_credentials(patched, landing: str) -> None:
    driver = FakeDriver(lands_on=landing, form=True)

    with pytest.raises(RuntimeError, match="neither its login form nor a signed-in page"):
        accounts.sellercloud(driver, "user@example.com", "hunter2")

    assert driver.user.keys == [] and driver.password.keys == []


@pytest.mark.parametrize("login_path", [
    "https://tenant.example.com/account/login.aspx",
    "https://tenant.example.com/Account/Login.aspx",   # path compared case-insensitively
])
def test_login_page_is_never_signed_in_even_with_a_logout_link(patched, login_path: str) -> None:
    driver = FakeDriver(lands_on=login_path, form=False, signed_in=True)

    with pytest.raises(RuntimeError, match="neither its login form nor a signed-in page"):
        accounts.sellercloud(driver, "user@example.com", "hunter2")


def test_error_location_drops_userinfo_query_and_fragment() -> None:
    class Driver:
        current_url = "https://user:pw@tenant.example.com:8443/Some/Page.aspx?token=abc#frag"

    assert accounts._sellercloud_where(Driver()) == "https://tenant.example.com:8443/Some/Page.aspx"

def test_redirect_between_wait_and_typing_never_receives_credentials(patched) -> None:
    """The form is checked again right before typing, so a late redirect gets nothing."""

    class RedirectingDriver(FakeDriver):
        lookups = 0

        def find_elements(self, by: object, selector: str) -> list:
            found = super().find_elements(by, selector)
            if selector == "NewFormBody_deltaUsername":
                self.lookups += 1
                if self.lookups == 1:  # the wait saw our form; now the page moves
                    self.current_url = "https://elsewhere.example.net/account/login.aspx"
            return found

    driver = RedirectingDriver()

    with pytest.raises(RuntimeError, match="changed before sign-in could start"):
        accounts.sellercloud(driver, "user@example.com", "hunter2")

    assert driver.user.keys == [] and driver.password.keys == []
