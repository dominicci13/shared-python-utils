"""Unit tests for `seller_automation_utils.ebay.customize_offers_table`'s retry loop.

The loop guards a page that Seller Hub frequently serves in a half-rendered or
errored state, so the tests drive it with a stub driver that raises the real
Selenium exceptions rather than touching a browser.
"""
from __future__ import annotations

import pytest
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)

from seller_automation_utils import ebay


class FakeElement:
    """Clickable stub; optionally raises a given exception on the first N clicks."""

    def __init__(self, raises: Exception | None = None, fail_times: int = 0, text: str = "") -> None:
        self.raises = raises
        self.fail_times = fail_times
        self.clicks = 0
        self.text = text

    def click(self) -> None:
        self.clicks += 1
        if self.raises is not None and self.clicks <= self.fail_times:
            raise self.raises


class FakeDriver:
    def __init__(self, link: FakeElement, dialog_title: str = "Add or review discounts") -> None:
        self.link = link
        self.dialog_title = dialog_title
        self.refreshes = 0
        self.scripts: list[str] = []

    def execute_script(self, script: str, *args: object) -> None:
        self.scripts.append(script)

    def refresh(self) -> None:
        self.refreshes += 1

    def find_element(self, by: object, selector: str) -> FakeElement:
        if selector == "dialog-title":
            return FakeElement(text=self.dialog_title)
        return FakeElement()


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):
    """Replace WebDriverWait/EC so waits resolve or raise instantly.

    `probe_error` controls what the in-handler dialog probe raises; the loop must
    swallow every one of them and fall through to the refresh.
    """
    state: dict[str, object] = {"probe_error": TimeoutException("no dialog")}

    class FakeWait:
        def __init__(self, driver: FakeDriver, timeout: int) -> None:
            self.driver = driver

        def until(self, condition):
            return condition(self.driver)

    def presence(locator):
        def _cond(driver: FakeDriver):
            if locator[1] == ".customize-link":
                return driver.link
            return FakeElement()
        return _cond

    def clickable(locator):
        def _cond(driver: FakeDriver):
            # Only the discount-dialog probe is wired to fail.
            if "lightbox-dialog" in locator[1]:
                error = state["probe_error"]
                if error is not None:
                    raise error
            return FakeElement()
        return _cond

    monkeypatch.setattr(ebay, "WebDriverWait", FakeWait)
    monkeypatch.setattr(ebay.EC, "presence_of_element_located", presence)
    monkeypatch.setattr(ebay.EC, "element_to_be_clickable", clickable)
    monkeypatch.setattr(ebay.time, "sleep", lambda _: None)
    return state


def test_clicks_customize_link_once_when_healthy(patched) -> None:
    driver = FakeDriver(FakeElement())
    ebay.customize_offers_table(driver)
    assert driver.link.clicks == 1
    assert driver.refreshes == 0


def test_scrolls_link_into_view_before_clicking(patched) -> None:
    driver = FakeDriver(FakeElement())
    ebay.customize_offers_table(driver)
    assert "scrollIntoView" in driver.scripts[0]


def test_recovers_after_not_interactable(patched) -> None:
    link = FakeElement(raises=ElementNotInteractableException("nope"), fail_times=2)
    driver = FakeDriver(link)
    ebay.customize_offers_table(driver)
    assert link.clicks == 3
    assert driver.refreshes == 2


@pytest.mark.parametrize(
    "probe_error",
    [
        StaleElementReferenceException("stale"),
        NoSuchElementException("missing"),
        ElementNotInteractableException("not interactable"),
        ElementClickInterceptedException("intercepted"),
        TimeoutException("timeout"),
    ],
)
def test_dialog_probe_failure_never_escapes(patched, probe_error: Exception) -> None:
    """The 2026-07-30 crash: a stale element in the probe killed the whole run."""
    patched["probe_error"] = probe_error
    link = FakeElement(raises=ElementNotInteractableException("nope"), fail_times=1)
    driver = FakeDriver(link)

    ebay.customize_offers_table(driver)

    assert link.clicks == 2
    assert driver.refreshes == 1


def test_gives_up_after_five_attempts(patched) -> None:
    link = FakeElement(raises=ElementNotInteractableException("nope"), fail_times=99)
    driver = FakeDriver(link)

    with pytest.raises(RuntimeError, match="never became clickable"):
        ebay.customize_offers_table(driver)

    assert link.clicks == 5
    assert driver.refreshes == 5


def test_intercepted_click_closes_discount_dialog(patched) -> None:
    link = FakeElement(raises=ElementClickInterceptedException("covered"), fail_times=1)
    driver = FakeDriver(link, dialog_title="Add or review discounts")
    ebay.customize_offers_table(driver)
    assert link.clicks == 2
    assert driver.refreshes == 0


def test_intercepted_click_refreshes_on_unknown_dialog(patched) -> None:
    link = FakeElement(raises=ElementClickInterceptedException("covered"), fail_times=1)
    driver = FakeDriver(link, dialog_title="Something else entirely")
    ebay.customize_offers_table(driver)
    assert link.clicks == 2
    assert driver.refreshes == 1
