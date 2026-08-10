"""Unit tests for `seller_automation_utils.ebay.customize_offers_table`.

Covers the retry loop that guards Seller Hub's half-rendered states, and the
column selection, which must drive each checkbox to an absolute state and verify
the click landed. The stub driver raises the real Selenium exceptions rather than
touching a browser.
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

# What eBay's redesigned dialog leaves checked after "Restore Defaults".
DEFAULT_ON = {"customize-listingSKU", "customize-price"}

ALL_COLUMNS = DEFAULT_ON | {
    "customize-availableQuantity",
    "customize-soldQuantity",
    "customize-visitCount",
    "customize-watchCount",
    "customize-scheduledStartDate",
    "customize-itemSpecifics",
    "customize-listingId",
    "customize-format",
    "customize-promoteListing",
    "customize-unansweredQuestionCount",
    "customize-bidCount",
    "customize-promotions",
}


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


class FakeCheckbox:
    """Checkbox that reports its state, and can refuse to respond to a click."""

    def __init__(self, checked: bool = False, inert: bool = False) -> None:
        self.checked = checked
        self.inert = inert
        self.clicks = 0

    def click(self) -> None:
        self.clicks += 1
        if not self.inert:
            self.checked = not self.checked

    def get_property(self, name: str) -> bool:
        assert name == "checked"
        return self.checked


class FakeLabel:
    """The `label[for=...]` eBay renders next to each checkbox."""

    def __init__(self, box: FakeCheckbox, inert: bool = False) -> None:
        self.box = box
        self.inert = inert

    def click(self) -> None:
        if not self.inert:
            self.box.checked = not self.box.checked


class FakeDriver:
    def __init__(
        self,
        link: FakeElement,
        dialog_title: str = "Add or review discounts",
        missing: set[str] | None = None,
        inert: set[str] | None = None,
        inert_labels: set[str] | None = None,
        js_inert: bool = False,
        save_inert: bool = False,
        save_alert: str = "",
    ) -> None:
        self.save_inert = save_inert
        self.save_alert = save_alert
        self.link = link
        self.dialog_title = dialog_title
        self.refreshes = 0
        self.scripts: list[str] = []
        self.missing = missing or set()
        self.js_inert = js_inert
        inert = inert or set()
        self.inert_labels = inert_labels or set()
        self.boxes = {
            name: FakeCheckbox(checked=name in DEFAULT_ON, inert=name in inert)
            for name in ALL_COLUMNS
        }
        self.clicked: list[str] = []

    def execute_script(self, script: str, *args: object) -> object:
        self.scripts.append(script)
        if "dispatchEvent" in script and args and not self.js_inert:
            box = args[0]
            box.checked = not box.checked
        if "shui-dt-column__" in script:
            # `save_inert` models eBay accepting the selection then serving the
            # default table anyway — the 2026-08-06 14:09 behaviour.
            checked = DEFAULT_ON if self.save_inert else self.state()
            return [n.replace("customize-", "") for n in checked]
        return None

    def refresh(self) -> None:
        self.refreshes += 1

    def find_elements(self, by: object, selector: str) -> list[object]:
        if selector == ".customization-content__alert":
            return [FakeElement(text=self.save_alert)] if self.save_alert else []
        return []

    def find_element(self, by: object, selector: str) -> object:
        if selector == "dialog-title":
            return FakeElement(text=self.dialog_title)
        if selector.startswith("label[for="):
            name = selector.split("'")[1]
            return FakeLabel(self.boxes[name], inert=name in self.inert_labels)
        if selector in self.missing:
            raise NoSuchElementException(f"no such element: {selector}")
        if selector in self.boxes:
            return self.boxes[selector]
        self.clicked.append(selector)
        return FakeElement()

    def state(self) -> set[str]:
        """Ids currently checked."""
        return {name for name, box in self.boxes.items() if box.checked}


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


def test_selects_exactly_the_requested_columns(patched) -> None:
    """The 2026-08-06 data loss: Views/Watchers/Sold were never actually enabled."""
    driver = FakeDriver(FakeElement())

    ebay.customize_offers_table(driver, sold=True, watchers=True, views=True, start_date=True)

    assert driver.state() == DEFAULT_ON | {
        "customize-availableQuantity",
        "customize-soldQuantity",
        "customize-visitCount",
        "customize-watchCount",
        "customize-scheduledStartDate",
    }


def test_unrequested_columns_stay_off(patched) -> None:
    driver = FakeDriver(FakeElement())

    ebay.customize_offers_table(driver)

    assert driver.state() == DEFAULT_ON | {"customize-availableQuantity"}


def test_already_correct_column_is_not_clicked(patched) -> None:
    """Blind toggling is what broke this — a box already right must be left alone."""
    driver = FakeDriver(FakeElement())
    driver.boxes["customize-visitCount"].checked = True

    ebay.customize_offers_table(driver, views=True)

    assert driver.boxes["customize-visitCount"].clicks == 0
    assert driver.boxes["customize-visitCount"].checked is True


def test_label_is_the_primary_click_target(patched) -> None:
    """Clicking the input sets `checked` without React noticing; the label does not."""
    driver = FakeDriver(FakeElement(), inert={"customize-visitCount"})

    ebay.customize_offers_table(driver, views=True)

    assert driver.boxes["customize-visitCount"].checked is True
    assert driver.boxes["customize-visitCount"].clicks == 0


def test_falls_back_to_the_input_when_the_label_ignores_clicks(patched) -> None:
    driver = FakeDriver(FakeElement(), inert_labels={"customize-visitCount"})

    ebay.customize_offers_table(driver, views=True)

    assert driver.boxes["customize-visitCount"].checked is True
    assert driver.boxes["customize-visitCount"].clicks == 1


def test_falls_back_to_js_when_input_and_label_both_ignore_clicks(patched) -> None:
    driver = FakeDriver(
        FakeElement(),
        inert={"customize-visitCount"},
        inert_labels={"customize-visitCount"},
    )

    ebay.customize_offers_table(driver, views=True)

    assert driver.boxes["customize-visitCount"].checked is True


def test_raises_when_a_column_refuses_every_strategy(patched) -> None:
    driver = FakeDriver(
        FakeElement(),
        inert={"customize-visitCount"},
        inert_labels={"customize-visitCount"},
        js_inert=True,
    )

    with pytest.raises(RuntimeError, match="would not change state"):
        ebay.customize_offers_table(driver, views=True)


def test_ebay_save_error_is_surfaced_verbatim(patched) -> None:
    """2026-08-06: every save was rejected, on both accounts, headed and headless."""
    driver = FakeDriver(
        FakeElement(),
        save_alert="We ran into a problem and couldn't complete your action. Please try again.",
    )

    with pytest.raises(RuntimeError, match="eBay refused to save"):
        ebay.customize_offers_table(driver, sold=True, watchers=True, views=True, start_date=True)


def test_raises_when_the_save_does_not_apply(patched) -> None:
    """eBay took the selection and served the default table anyway (14:09 run)."""
    driver = FakeDriver(FakeElement(), save_inert=True)

    with pytest.raises(RuntimeError, match="never appeared"):
        ebay.customize_offers_table(driver, sold=True, watchers=True, views=True, start_date=True)


def test_empty_category_does_not_trip_the_save_check(patched, monkeypatch) -> None:
    """No rows means no evidence either way — must not raise."""
    driver = FakeDriver(FakeElement())
    monkeypatch.setattr(driver, "execute_script", lambda script, *args: [])

    ebay.customize_offers_table(driver, sold=True, watchers=True, views=True, start_date=True)


@pytest.mark.parametrize("element_id", sorted(ebay.OPTIONAL_COLUMNS))
def test_missing_optional_column_is_skipped(patched, element_id: str) -> None:
    """eBay dropped Item number and Format from the dialog on 2026-08-06."""
    driver = FakeDriver(FakeElement(), missing={element_id})

    ebay.customize_offers_table(driver, sold=True, watchers=True, views=True, start_date=True)

    assert "customize-save" in driver.clicked


def test_all_optional_columns_missing_at_once(patched) -> None:
    driver = FakeDriver(FakeElement(), missing=set(ebay.OPTIONAL_COLUMNS))

    ebay.customize_offers_table(driver, sold=True, watchers=True, views=True, start_date=True)

    assert "customize-save" in driver.clicked


@pytest.mark.parametrize(
    "element_id",
    [
        "customize-availableQuantity",
        "customize-soldQuantity",
        "customize-scheduledStartDate",
        "customize-visitCount",
        "customize-watchCount",
    ],
)
def test_missing_required_column_raises(patched, element_id: str) -> None:
    """A column a report actually reads must fail loudly, not insert blank data."""
    driver = FakeDriver(FakeElement(), missing={element_id})

    with pytest.raises(NoSuchElementException):
        ebay.customize_offers_table(driver, sold=True, watchers=True, views=True, start_date=True)
