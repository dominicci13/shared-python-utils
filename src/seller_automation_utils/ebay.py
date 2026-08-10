from __future__ import annotations

import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, ElementClickInterceptedException, ElementNotInteractableException, StaleElementReferenceException
import logging

log = logging.getLogger(__name__)

# Columns eBay may drop from the Customize dialog without notice. "Item number"
# (listingId) and "Format" both vanished on 2026-08-06 and took the nightly runs
# down with them. No report reads either one — the item number comes off the
# row's `data-id` — so a missing checkbox here must not kill a run. Anything NOT
# listed feeds a column a report actually extracts, and stays fatal so a real
# DOM change still fails loudly instead of inserting blank data.
OPTIONAL_COLUMNS = frozenset({
    "customize-itemSpecifics",
    "customize-listingId",
    "customize-format",
    "customize-promotions",
})


def _checked(element: object) -> bool | None:
    """Read a checkbox's live state.

    Returns:
        bool | None: The ``checked`` property, or None if the driver cannot
            report it (in which case the caller cannot verify its own clicks).
    """
    try:
        return element.get_property("checked")
    except Exception:
        return None


def _ordering_values(driver: object) -> list[str] | None:
    """Column keys in the dialog's "Arrange the order of the columns" list.

    This list is rendered from the state eBay serializes on Save, so it — not the
    checkbox's ``checked`` property — is the honest answer to "what will be saved".

    Args:
        driver (object): Active SeleniumBase WebDriver instance.

    Returns:
        list[str] | None: Column keys in order, or None if the list is not on the
            page (older dialog layouts, or the dialog already closed).
    """
    try:
        return driver.execute_script(
            "const list = document.querySelector(\"select[id$='columns-ordering']\");"
            "return list ? Array.from(list.options).map(option => option.value) : null;"
        )
    except Exception:
        return None


def _dialog_alert(driver: object) -> str:
    """Text eBay put in the Customize dialog's alert strip, if any.

    Args:
        driver (object): Active SeleniumBase WebDriver instance.

    Returns:
        str: The alert text, or "" when the strip is absent or empty.
    """
    try:
        for node in driver.find_elements(By.CSS_SELECTOR, ".customization-content__alert"):
            text = node.text.strip()
            if text:
                return text
    except Exception:
        log.debug("Could not read the dialog alert.", exc_info=True)
    return ""


def _applied_columns(driver: object) -> list[str]:
    """Column keys actually rendered in the listings grid.

    Args:
        driver (object): Active SeleniumBase WebDriver instance.

    Returns:
        list[str]: Column keys from the first data row's cells. Empty when no row
            is rendered, which proves nothing either way.
    """
    try:
        return driver.execute_script(
            "const row = document.querySelector('tr.grid-row[data-id]');"
            "return row ? Array.from(row.querySelectorAll('td'))"
            ".map(td => (td.className.match(/shui-dt-column__(\\w+)/) || [])[1])"
            ".filter(Boolean) : [];"
        ) or []
    except Exception:
        log.debug("Could not read back the applied columns.", exc_info=True)
        return []


def _wait_for_columns(driver: object, wanted: set[str], attempts: int = 10) -> list[str]:
    """Poll the grid until every wanted column is rendered.

    Saving the dialog updates the view server-side and the grid re-renders on its
    own schedule, so a single read straight after the click catches the old table.

    Args:
        driver (object): Active SeleniumBase WebDriver instance.
        wanted (set[str]): Column keys that must be present.
        attempts (int): How many times to re-read, 2s apart.

    Returns:
        list[str]: The last column set read, complete or not.
    """
    applied: list[str] = []
    for _ in range(attempts):
        applied = _applied_columns(driver)
        if applied and wanted.issubset(applied):
            return applied
        time.sleep(2)
    return applied


def _is_set(driver: object, element_id: str) -> bool:
    """Whether a column checkbox is currently checked, tolerating a missing box.

    Args:
        driver (object): Active SeleniumBase WebDriver instance.
        element_id (str): DOM id of the checkbox.

    Returns:
        bool: True only if the box exists and reports itself checked.
    """
    try:
        return bool(_checked(driver.find_element(By.ID, element_id)))
    except Exception:
        return False


def _set_column(driver: object, element_id: str, desired: bool) -> None:
    """Put one column checkbox into ``desired`` state, verifying it took effect.

    Clicks only when the box is not already where it needs to be, then confirms
    the state actually changed. eBay wraps each input in ``span.checkbox`` with a
    sibling ``label``, so a native click on the input can land without toggling
    anything; the label and JS fallbacks cover that. Every strategy is verified,
    which is what turns a silently-wrong table into a loud failure.

    Args:
        driver (object): Active SeleniumBase WebDriver instance.
        element_id (str): DOM id of the checkbox.
        desired (bool): Whether the column should end up selected.

    Raises:
        NoSuchElementException: If a checkbox outside ``OPTIONAL_COLUMNS`` is
            missing from the dialog.
        RuntimeError: If the checkbox is present but refuses to change state.
    """
    try:
        box = driver.find_element(By.ID, element_id)
    except NoSuchElementException:
        if element_id not in OPTIONAL_COLUMNS:
            raise
        log.warning(f"Column [cyan]{element_id}[/cyan] is no longer offered by eBay. Skipping it.")
        return

    before = _checked(box)
    if before is None:
        # Can't read the state, so can't verify a click either. Best effort:
        # select when asked, leave alone otherwise.
        log.warning(f"Could not read the state of [cyan]{element_id}[/cyan].")
        if desired:
            box.click()
        return

    if before == desired:
        return

    # Label first, deliberately. Clicking the input sets its `checked` property
    # without eBay's React state noticing, so the box reads correct, Save
    # serializes the state React still holds, and the table comes back with the
    # Restore-Defaults columns (2026-08-06 14:40). Clicking the label makes the
    # browser dispatch the click on the input itself, which React does handle.
    strategies = (
        ("label click", lambda element: driver.find_element(
            By.CSS_SELECTOR, f"label[for='{element_id}']"
        ).click()),
        ("native click", lambda element: element.click()),
        ("JS click", lambda element: driver.execute_script(
            "arguments[0].click();"
            "arguments[0].dispatchEvent(new Event('change', {bubbles: true}));",
            element,
        )),
    )

    for name, action in strategies:
        try:
            action(box)
        except Exception:
            log.debug(f"{name} failed for {element_id}.", exc_info=True)
            continue

        box = driver.find_element(By.ID, element_id)
        if _checked(box) == desired:
            if name != "label click":
                log.warning(f"Column [cyan]{element_id}[/cyan] needed the {name} fallback.")
            return

    raise RuntimeError(
        f"Column {element_id} would not change state — eBay's Customize dialog may have changed."
    )


###############################################################################################################################################
def customize_offers_table(driver: object, sold: bool = False, watchers: bool = False, views: bool = False, start_date: bool = False) -> None:
    """Customize the Active Listings table columns in the eBay seller dashboard.

    Clicks the 'Customize table' button, resets to defaults, then selects the
    specific columns needed for automation. Handles banners and intercepted clicks
    by closing dialogs or refreshing the page.

    Columns in ``OPTIONAL_COLUMNS`` are skipped with a warning when eBay stops
    offering them; every other checkbox is required and a missing one raises.

    Args:
        driver (object): Active SeleniumBase WebDriver instance.
        sold (bool, optional): Include the 'Sold Quantity' column. Defaults to False.
        watchers (bool, optional): Include the 'Watchers' column. Defaults to False.
        views (bool, optional): Include the 'Views (30 days)' column. Defaults to False.
        start_date (bool, optional): Include the 'Start Date' column. Defaults to False.

    Raises:
        RuntimeError: If the Customize-table link never becomes clickable.
        NoSuchElementException: If a required column checkbox is missing.
    """
    log.info("Customizing the offers table.")
    for attempt in range(1, 6):
        try:
            customize_tbl = WebDriverWait(driver, 15).until(EC.presence_of_element_located((
                By.CSS_SELECTOR,
                ".customize-link"
            )))

            driver.execute_script("arguments[0].scrollIntoView(true);", customize_tbl)
            customize_tbl.click()
            break

        except (TimeoutException, ElementNotInteractableException):
            # Best-effort dialog close. When Seller Hub errors ("Something went
            # wrong"), the link exists but isn't interactable and the page is
            # re-rendering underneath, so this probe can raise stale/not-found
            # instead of timing out. Swallow all of it — the refresh is the
            # actual recovery, and letting it escape kills the whole run.
            try:
                WebDriverWait(driver, 5).until(EC.element_to_be_clickable((
                    By.CSS_SELECTOR,
                    "#sh-page > div.card-old > div > div.overlays > div.sme-discount-layer > span > div > div.lightbox-dialog__window.lightbox-dialog__window--animate.keyboard-trap--active > div.lightbox-dialog__header > button"
                ))).click()
            except (TimeoutException, StaleElementReferenceException, NoSuchElementException,
                    ElementNotInteractableException, ElementClickInterceptedException):
                pass

            log.warning(f"Customize-table link not ready (attempt #{attempt}). Refreshing.")
            driver.refresh()

        except ElementClickInterceptedException:
            header = driver.find_element(By.CLASS_NAME, "dialog-title").text

            if header == "Add or review discounts":
                log.info(f"Closing the [cyan]{header}[/cyan] dialog.")
                driver.find_element(
                    By.CSS_SELECTOR,
                    "#sh-page > div.card-old > div > div.overlays > div.sme-discount-layer > span > div > div.lightbox-dialog__window.lightbox-dialog__window--animate.keyboard-trap--active > div.lightbox-dialog__header > button"
                ).click()
                time.sleep(2)

            else:
                driver.refresh()
                time.sleep(5)

    else:
        # Previously an unbounded while-loop: a persistent Seller Hub outage hung
        # the job forever instead of failing, so no crash alert ever fired.
        raise RuntimeError("Customize-table link never became clickable after 5 attempts.")

    log.info("Restoring table to default columns.")
    WebDriverWait(driver, 10).until(EC.element_to_be_clickable((
        By.ID,
        "customize-restoreDefaults"
    ))).click()
    time.sleep(3)

    log.info("Selecting required columns.")

    # Every column is set to an absolute state rather than blind-toggled. The old
    # code assumed Restore Defaults left Views and Watchers ON and only clicked to
    # turn them OFF; eBay's redesigned dialog defaults to SKU and price alone, so
    # those assumptions silently produced a table with no Views, Watchers or Sold
    # data (2026-08-06). Never infer the default set — read each box and set it.
    requested = [
        name for name, wanted in (
            ("customize-availableQuantity", True),
            ("customize-soldQuantity", sold),
            ("customize-visitCount", views),
            ("customize-watchCount", watchers),
            ("customize-scheduledStartDate", start_date),
        ) if wanted
    ]

    _set_column(driver, "customize-availableQuantity", True)
    _set_column(driver, "customize-soldQuantity", sold)
    _set_column(driver, "customize-visitCount", views)
    _set_column(driver, "customize-watchCount", watchers)
    _set_column(driver, "customize-scheduledStartDate", start_date)

    # Not read by any report — kept off so the grid stays narrow.
    for unwanted in (
        "customize-itemSpecifics",
        "customize-listingId",
        "customize-format",
        "customize-promoteListing",
        "customize-unansweredQuestionCount",
        "customize-bidCount",
        "customize-promotions",
    ):
        _set_column(driver, unwanted, False)

    # State as the dialog is submitted, not as each box was clicked. A React
    # re-render can discard a checked box after the per-click check passed, which
    # looks identical to a Save that never applied — this line tells them apart.
    still_set = [name.replace("customize-", "") for name in requested if _is_set(driver, name)]
    log.info(f"Columns selected at save time: [cyan]{', '.join(still_set) or 'none'}[/cyan].")

    # eBay's own view of the selection. The checkboxes are just DOM; this list is
    # rendered from the state Save actually serializes, so when the two disagree
    # the clicks never reached React and the save will silently do nothing.
    ordering = _ordering_values(driver)
    if ordering is not None:
        log.info(f"Dialog's column list: [cyan]{', '.join(ordering) or 'empty'}[/cyan].")
        missed = [name.replace("customize-", "") for name in requested
                  if name.replace("customize-", "") not in ordering]
        if missed:
            log.warning(f"Selected but absent from the dialog's own list: [cyan]{missed}[/cyan].")

    log.info("Saving table configuration.")
    driver.find_element(By.ID, "customize-save").click()
    time.sleep(5)

    # eBay reports its own save failures in the dialog's alert strip and leaves the
    # dialog open. Surfacing that verbatim beats letting the run fail later with
    # "the columns never appeared", which reads like a selector bug and is not one.
    alert = _dialog_alert(driver)
    if alert:
        raise RuntimeError(f"eBay refused to save the table configuration: {alert}")

    log.info("Waiting for the page to reload.")
    wanted = {name.replace("customize-", "") for name in requested}
    applied = _wait_for_columns(driver, wanted)

    # The grid can keep serving the pre-save render well past the save itself, so
    # give it one reload before calling it a failure.
    if applied and not wanted.issubset(applied):
        log.warning("Grid still shows the old columns. Reloading once.")
        driver.refresh()
        time.sleep(5)
        applied = _wait_for_columns(driver, wanted)

    log.info(f"Table columns now: [cyan]{', '.join(applied) or 'none found'}[/cyan].")

    # Fail here, at the cause, rather than letting the caller scrape a table whose
    # columns are missing and discover it as blank data several steps later. Only
    # judged when a row was actually read back — an empty category proves nothing.
    if applied:
        lost = sorted(wanted - set(applied))
        if lost:
            raise RuntimeError(
                f"Saved the table but {lost} never appeared (got: {applied}). "
                "eBay's Customize dialog accepted the selection and discarded it."
            )