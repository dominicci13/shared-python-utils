"""Unit tests for `seller_automation_utils.accounts.ebay`.

eBay serves two sign-in layouts and a captcha splash. The tests drive a stub
driver so every branch is covered without touching a browser.
"""
from __future__ import annotations

import pytest
from selenium.common.exceptions import TimeoutException

from seller_automation_utils import accounts

HUB_URL = "https://www.ebay.com/sh/lst/active?limit=200"
SIGNIN_URL = "https://signin.ebay.com/ws/eBayISAPI.dll?SignIn&UsingSSL=1"
CAPTCHA_URL = "https://www.ebay.com/splashui/captcha?ap=1&appName=orch"


class FakeField:
    """Records everything typed into it."""

    def __init__(self, displayed: bool = True) -> None:
        self.displayed = displayed
        self.keys: list[str] = []

    def is_displayed(self) -> bool:
        return self.displayed

    def send_keys(self, value: str) -> None:
        self.keys.append(value)

    @property
    def typed(self) -> str:
        """Just the literal text, with the clear/submit control keys dropped."""
        return "".join(k for k in self.keys if all(ch < "" for ch in k))


class FakeDriver:
    def __init__(
        self,
        current_url: str = SIGNIN_URL,
        username_field: FakeField | None = None,
        password_field: FakeField | None = None,
    ) -> None:
        self.current_url = current_url
        self.username_field = username_field
        self.password_field = password_field

    def find_elements(self, by: object, selector: str) -> list[FakeField]:
        return [self.username_field] if self.username_field else []


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):
    """Make the password wait resolve to the driver's field, or time out."""

    class FakeWait:
        def __init__(self, driver: FakeDriver, timeout: int) -> None:
            self.driver = driver

        def until(self, condition):
            return condition(self.driver)

    def clickable(locator):
        def _cond(driver: FakeDriver):
            if driver.password_field is None:
                raise TimeoutException("password field never became clickable")
            return driver.password_field
        return _cond

    monkeypatch.setattr(accounts, "WebDriverWait", FakeWait)
    monkeypatch.setattr(accounts.EC, "element_to_be_clickable", clickable)


def test_live_session_returns_without_touching_the_form(patched) -> None:
    """The healthy path: eBay never redirected, so there is nothing to sign in to.

    Regression guard — raising or waiting out the password timeout here would
    break every eBay automation's normal run, not just a lapsed one.
    """
    password = FakeField()
    driver = FakeDriver(current_url=HUB_URL, password_field=password)

    accounts.ebay(password="hunter2", driver=driver)

    assert password.keys == []


def test_password_only_form(patched) -> None:
    password = FakeField()
    driver = FakeDriver(password_field=password)

    accounts.ebay(password="hunter2", driver=driver)

    assert password.typed == "hunter2"


def test_two_step_form_submits_username_then_password(patched) -> None:
    """The 2026-08-06 failure: eBay asks who is signing in before showing #pass."""
    user, password = FakeField(), FakeField()
    driver = FakeDriver(username_field=user, password_field=password)

    accounts.ebay(password="hunter2", driver=driver, username="seller@example.com")

    assert user.typed == "seller@example.com"
    assert password.typed == "hunter2"


def test_two_step_falls_back_to_env_username(patched, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("eBay_user", "from-env@example.com")
    user, password = FakeField(), FakeField()
    driver = FakeDriver(username_field=user, password_field=password)

    accounts.ebay(password="hunter2", driver=driver)

    assert user.typed == "from-env@example.com"


def test_two_step_without_username_raises_clearly(patched, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("eBay_user", raising=False)
    driver = FakeDriver(username_field=FakeField(), password_field=FakeField())

    with pytest.raises(RuntimeError, match="none is configured"):
        accounts.ebay(password="hunter2", driver=driver)


def test_hidden_username_field_is_ignored(patched) -> None:
    """`#pass` and the username input both exist in the DOM; only visibility decides."""
    password = FakeField()
    driver = FakeDriver(username_field=FakeField(displayed=False), password_field=password)

    accounts.ebay(password="hunter2", driver=driver)

    assert password.typed == "hunter2"


def test_captcha_before_signin_raises(patched) -> None:
    driver = FakeDriver(current_url=CAPTCHA_URL, password_field=FakeField())

    with pytest.raises(RuntimeError, match="bot check"):
        accounts.ebay(password="hunter2", driver=driver)


def test_captcha_during_signin_raises(patched) -> None:
    driver = FakeDriver(password_field=None)
    driver.current_url = CAPTCHA_URL

    with pytest.raises(RuntimeError, match="bot check"):
        accounts.ebay(password="hunter2", driver=driver)


def test_password_field_never_usable_names_the_url(patched) -> None:
    driver = FakeDriver(current_url="https://signin.ebay.com/ws/eBayISAPI.dll", password_field=None)

    with pytest.raises(RuntimeError, match="never became usable"):
        accounts.ebay(password="hunter2", driver=driver)
