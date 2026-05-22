"""SellerCloud Custom Export helpers.

Provides reusable browser automations against a SellerCloud tenant URL
of the form ``https://your-tenant.delta.sellercloud.com`` (the actual
tenant URL is loaded at runtime from ``config/paths.json``, which is
gitignored — see ``config/paths.json.example`` for the schema):

* :func:`request_custom_export` — navigates to the Manage Catalog grid
  with an inline SKU or product-group filter, walks the Export wizard
  with a named Custom Template, and returns the notify-download URL.
* :func:`download_report` — visits a notify-download URL produced by
  :func:`request_custom_export`, clicks the download button when the
  report is ready, waits for the file to land in the local downloads
  directory, and moves it to a caller-supplied destination.

Both functions read DOM selectors from ``config/selectors.json`` and
URLs from ``config/paths.json`` (both resolved relative to the entry
script, mirroring :mod:`seller_automation_utils.accounts`). The consumer
repo owns those JSON files and they are typically gitignored — see
``shared-python-utils/config/selectors.json.example`` and
``paths.json.example`` for the required schema.

Both functions log via the module logger
(``logging.getLogger("seller_automation_utils.sellercloud")``); configure
handlers at the application entry point with
:func:`seller_automation_utils.logging_utils.setup_logging`.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import time
from pathlib import Path

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    ElementNotInteractableException,
    StaleElementReferenceException,
    TimeoutException,
)

__all__ = ["request_custom_export", "download_report"]


log = logging.getLogger(__name__)


# Maximum SKUs SellerCloud allows in a single ``SKU=...`` filter; above
# this, the URL is rejected with a server-side validation error.
_MAX_SKUS_PER_REQUEST = 100

# Cached config payloads. Populated on first call to ``_selectors()`` or
# ``_paths()``; reset by clearing these module attributes (e.g., in tests).
_SELECTORS: dict[str, str] | None = None
_PATHS: dict[str, str] | None = None


def _config_path(name: str) -> Path:
    """Locate ``config/<name>`` relative to the entry script.

    Resolves against ``sys.argv[0]`` first so the lookup is correct even
    when the script is launched from a different working directory (e.g.,
    Windows Task Scheduler, a cron wrapper, or a parent shell). Falls
    back to ``Path.cwd() / "config" / name`` if ``sys.argv[0]`` is empty
    or its sibling ``config/`` folder doesn't exist.

    Args:
        name (str): Basename of the JSON file to locate (e.g.
            ``"selectors.json"``).

    Returns:
        Path: Resolved path to the file. Caller validates existence.
    """
    if sys.argv and sys.argv[0]:
        candidate = Path(sys.argv[0]).resolve().parent / "config" / name
        if candidate.exists():
            return candidate
    return Path.cwd() / "config" / name


def _selectors() -> dict[str, str]:
    """Return the cached SellerCloud DOM selector map.

    Loaded lazily so consumers who don't use this module never pay the
    file-read cost and don't crash on import if ``selectors.json`` is
    missing.

    Returns:
        dict[str, str]: Selector keys mapped to CSS/XPath strings.

    Raises:
        FileNotFoundError: If ``config/selectors.json`` doesn't exist.
        json.JSONDecodeError: If the file isn't valid JSON.
    """
    global _SELECTORS
    if _SELECTORS is None:
        path = _config_path("selectors.json")
        _SELECTORS = json.loads(path.read_text(encoding="utf-8"))
    return _SELECTORS


def _paths() -> dict[str, str]:
    """Return the cached SellerCloud URL map.

    Returns:
        dict[str, str]: URL keys mapped to fully-qualified URL strings.

    Raises:
        FileNotFoundError: If ``config/paths.json`` doesn't exist.
        json.JSONDecodeError: If the file isn't valid JSON.
    """
    global _PATHS
    if _PATHS is None:
        path = _config_path("paths.json")
        _PATHS = json.loads(path.read_text(encoding="utf-8"))
    return _PATHS


def _build_catalog_url(sku_list: list[str] | None, product_group: int | None) -> str:
    """Return the SellerCloud catalog URL pre-filtered by SKU or product group.

    Args:
        sku_list (list[str] | None): SKUs to join into a CSV filter, or
            ``None`` to use the product-group filter.
        product_group (int | None): SellerCloud product-group id, or
            ``None`` to use the SKU filter.

    Returns:
        str: Fully-qualified catalog URL with the chosen filter appended.
    """
    base = _paths()["sellercloud_catalog_url"]
    if product_group is not None:
        return f"{base}ProductGroupFilterType=1&ProductGroup={product_group}"
    return f"{base}SKU={','.join(sku_list or [])}"


def _wait_for_overlay_clear(driver: object, timeout_sec: int = 600) -> None:
    """Block until the SellerCloud loading-overlay spinner disappears.

    The grid wraps its loading state in an element whose id starts with
    ``overlay`` (e.g. ``overlayGridContainer``). The spinner is a deeply
    nested child div; once it loses visibility the page is interactable.

    Args:
        driver (object): Active SeleniumBase WebDriver instance.
        timeout_sec (int): Maximum seconds to wait for the spinner to
            clear. Defaults to ``600``.
    """
    while True:
        parent = driver.find_element(By.ID, "wrapper")
        children = parent.find_elements(By.CSS_SELECTOR, "[id]")
        try:
            overlay = next(
                child.get_attribute("id")
                for child in children
                if child.get_attribute("id").startswith("overlay")
            )
        except StopIteration:
            overlay = "overlay"
        except StaleElementReferenceException:
            log.info("Waiting for the overlay to be visible.")
            time.sleep(3)
            continue

        WebDriverWait(driver, timeout_sec).until(
            EC.invisibility_of_element_located(
                (By.CSS_SELECTOR, f"#{overlay} > div > div > div")
            )
        )
        return


def _click_select_all_checkbox(driver: object) -> None:
    """Tick the "select all rows" checkbox at the top of the catalog grid.

    Retries past three known interruptions: the Pendo onboarding tooltip,
    a stale DOM after the overlay clears, and a momentarily-disabled
    checkbox. Falls back to clicking the ``#check-all`` element if the
    primary XPath is unreachable.

    Args:
        driver (object): Active SeleniumBase WebDriver instance.

    Raises:
        RuntimeError: If the checkbox can't be clicked after every fallback.
    """
    sel = _selectors()
    for _ in range(5):
        _wait_for_overlay_clear(driver)
        log.info("Scrolling to the top of the page.")
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(5)

        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.XPATH, sel["select_all_checkbox"]))
            ).click()
            return
        except (
            ElementClickInterceptedException,
            StaleElementReferenceException,
            ElementNotInteractableException,
            TimeoutException,
        ):
            log.info("Waiting for the checkbox to be clickable.")

        # Dismiss the Pendo onboarding overlay if it's covering the checkbox.
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, sel["pendo_close_guide"]))
            ).click()
        except TimeoutException:
            pass

        # Fallback: try the simpler #check-all selector that the grid also exposes.
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.ID, "check-all"))
            ).click()
            return
        except (
            ElementClickInterceptedException,
            StaleElementReferenceException,
            ElementNotInteractableException,
            TimeoutException,
        ):
            log.info("Still couldn't click on the checkbox. Retrying.")
            time.sleep(5)

    raise RuntimeError("Could not click the select-all checkbox after 5 attempts.")


def _open_export_menu(driver: object) -> None:
    """Click the floating export button at the top of the catalog grid.

    The button lives inside one of several sibling ``div[i]`` containers
    (i = 0..10); the exact slot depends on which optional toolbar widgets
    the user's account has enabled. We try each in order and stop at the
    first one that becomes clickable within 5 s.

    Args:
        driver (object): Active SeleniumBase WebDriver instance.

    Raises:
        RuntimeError: If no slot is clickable within the search window.
    """
    sel = _selectors()
    log.info("Opening the export menu.")
    for i in range(11):
        try:
            WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.XPATH, sel["export_float_btn"].format(i=i)))
            ).click()
            return
        except TimeoutException:
            time.sleep(2)
    raise RuntimeError("Export menu button not clickable in any slot (i=0..10).")


def _click_export_products(driver: object, template_label: str) -> None:
    """Within the open export menu, click the "Export Products (Catalog)…" item.

    The menu layout is variable: the desired item lives somewhere in a
    nested ``div[div]/...../li[row]`` tree, and a "RESTRICT" warning popup
    sometimes intercepts the click and forces us to retry one column over.
    Walks the (div, row) grid until either the export popup appears or
    we exhaust every slot.

    Args:
        driver (object): Active SeleniumBase WebDriver instance.
        template_label (str): Label used only in log lines for context
            when screenshots get saved.

    Raises:
        RuntimeError: If no "Export Products (Catalog)…" entry is found
            within the searchable slots.
    """
    sel = _selectors()
    row = 2
    div = 0
    while True:
        try:
            export_btn = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located(
                    (By.XPATH, sel["export_products_item"].format(div=div, row=row))
                )
            )
        except TimeoutException:
            div += 1
            if div == 10:
                raise RuntimeError(
                    f"Could not find the export button for {template_label!r} "
                    "after exhausting div slots 0..9."
                )
            continue

        export_text = export_btn.text.split("\n")[-1]
        if export_text == "Export Products (Catalog)...":
            export_btn.click()
        elif export_text.startswith("EXPORT"):
            pass
        else:
            row += 1
            continue

        # Confirm we got the "EXPORT…" popup vs. a "RESTRICT…" warning.
        try:
            popup_title = WebDriverWait(driver, 120).until(
                EC.presence_of_element_located((By.XPATH, sel["popup_title"]))
            ).text
        except TimeoutException:
            raise RuntimeError(
                f"No popup appeared after clicking export for {template_label!r}."
            )

        if popup_title.startswith("RESTRICT"):
            driver.find_element(By.XPATH, sel["restrict_close_btn"]).click()
            div += 1
            row = 2
            continue
        if popup_title.startswith("EXPORT"):
            return


def _select_template(driver: object, custom_template: str) -> None:
    """Pick a custom export template by visible name from the wizard dropdown.

    Args:
        driver (object): Active SeleniumBase WebDriver instance.
        custom_template (str): Exact label of the template as it appears
            in the SellerCloud wizard's ``<select>`` element.

    Raises:
        ValueError: If no option in the dropdown matches ``custom_template``.
    """
    sel = _selectors()
    log.info(f"Selecting the [cyan]{custom_template}[/cyan] template.")
    templates = WebDriverWait(driver, 600).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, sel["templates_select"]))
    )
    templates.click()

    options = templates.text.split("\n")
    selection = next(
        (i for i, label in enumerate(options) if label == custom_template), None
    )
    if selection is None:
        raise ValueError(
            f"Custom template {custom_template!r} not found. "
            f"Available templates: {options}"
        )
    Select(templates).select_by_index(selection)


def request_custom_export(
    driver: object,
    custom_template: str,
    sku_list: list[str] | None = None,
    product_group: int | None = None,
) -> str:
    """Request a SellerCloud Custom Export and return its notify-download URL.

    Filters the SellerCloud catalog grid via the URL (either by a SKU CSV
    or by product-group id, never both), selects every visible row, walks
    the Export Products wizard, picks the named Custom Template, and
    returns the URL of the resulting notify-download link.

    Args:
        driver (object): Active SeleniumBase WebDriver instance. Must
            already be logged in to the SellerCloud tenant configured in
            ``config/paths.json``.
        custom_template (str): Name of the saved Custom Export template,
            exactly as it appears in the wizard's template dropdown.
        sku_list (list[str] | None): SKUs to filter by (max
            ``_MAX_SKUS_PER_REQUEST`` = 100). Joined into a CSV in the
            query string. Mutually exclusive with ``product_group``.
        product_group (int | None): SellerCloud product-group id to filter
            by. Mutually exclusive with ``sku_list``.

    Returns:
        str: The ``href`` of the notify-download link rendered after the
            wizard completes. Pass this to :func:`download_report` to
            retrieve the file once SellerCloud finishes generating it.

    Raises:
        ValueError: If both or neither of ``sku_list`` / ``product_group``
            are provided, if ``sku_list`` is empty, if more than 100 SKUs
            are supplied, or if ``custom_template`` isn't in the
            template dropdown.
        RuntimeError: If a required UI step fails after exhausting its
            internal retries (e.g. the export menu never becomes
            clickable, the wizard popup doesn't appear, etc.).
    """
    if sku_list is None and product_group is None:
        msg = "Provide either sku_list or product_group."
        log.error(msg)
        raise ValueError(msg)
    if sku_list is not None and product_group is not None:
        msg = "Provide either sku_list or product_group, not both."
        log.error(msg)
        raise ValueError(msg)
    if sku_list is not None:
        if not sku_list:
            msg = "sku_list cannot be empty."
            log.error(msg)
            raise ValueError(msg)
        if len(sku_list) > _MAX_SKUS_PER_REQUEST:
            msg = (
                f"sku_list has {len(sku_list)} SKUs; "
                f"max is {_MAX_SKUS_PER_REQUEST}."
            )
            log.error(msg)
            raise ValueError(msg)

    paths = _paths()
    sel = _selectors()

    url = _build_catalog_url(sku_list, product_group)
    log.info(f"Requesting Custom Export for template [cyan]{custom_template}[/cyan].")
    driver.get(url)
    time.sleep(3)

    _click_select_all_checkbox(driver)

    # The "Select All Pages" button only appears when results span >1 page;
    # absent on small filters. Best-effort click; ignore if not present.
    try:
        WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, sel["select_all_btn"]))
        ).click()
    except TimeoutException:
        pass

    # Dismiss the Pendo tour popup if it shows up over the menu.
    try:
        WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.CLASS_NAME, sel["pendo_close_class"]))
        ).click()
    except TimeoutException:
        pass

    _open_export_menu(driver)

    time.sleep(5)
    if driver.current_url == paths["sellercloud_add_product_url"]:
        raise RuntimeError(
            "SellerCloud redirected to the add-product URL after opening the "
            "export menu — selection was likely empty."
        )

    _click_export_products(driver, template_label=custom_template)

    log.info("Choosing the [cyan]Custom[/cyan] export option.")
    custom_btn = WebDriverWait(driver, 600).until(
        EC.presence_of_element_located((By.XPATH, sel["custom_export_radio"]))
    )
    time.sleep(2)
    custom_btn.click()

    WebDriverWait(driver, 5).until(
        EC.element_to_be_clickable((By.XPATH, sel["export_next_btn"]))
    ).click()

    _select_template(driver, custom_template)

    WebDriverWait(driver, 10).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, sel["export_wizard_next_btn"]))
    ).click()

    href = WebDriverWait(driver, 600).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, sel["notify_download_link"]))
    ).get_attribute("href")

    log.success(
        f"[cyan]{custom_template}[/cyan] export has been requested successfully."
    )
    return href


def download_report(
    driver: object,
    download_url: str,
    download_path: str | Path,
    output_path: str | Path,
    poll_interval_sec: int = 60,
    file_timeout_sec: int = 600,
) -> Path:
    """Click through a SellerCloud notify-download URL and move the file.

    Navigates to ``download_url``, clicks the report's download button
    (retrying every ``poll_interval_sec`` while SellerCloud is still
    generating the file), waits for the resulting ``.xlsx`` to land in
    ``download_path``, then moves it to ``output_path``.

    The downloaded filename is derived from the URL: SellerCloud names
    its files after the trailing query-string value (e.g. ``?id=12345``
    → ``12345.csv`` / ``12345.xlsx`` / ``12345.tsv``). The expected
    extension is taken from ``output_path``'s suffix, so pass
    ``output_path=".../report.csv"`` for CSV templates,
    ``".../report.xlsx"`` for Excel templates, or ``".../report.tsv"``
    for tab-separated templates. If ``output_path`` has no suffix,
    defaults to ``.xlsx``.

    Args:
        driver (object): Active SeleniumBase WebDriver instance.
        download_url (str): The notify-download URL returned by
            :func:`request_custom_export`.
        download_path (str | Path): Directory Chrome saves downloads to.
            Usually the ``user_data_dir`` profile's Downloads folder, or
            an automation-specific override.
        output_path (str | Path): Final destination for the moved file
            (including the filename and the extension). Parent
            directories are created on demand. The file's suffix is
            also used to recognize the downloaded file in
            ``download_path``.
        poll_interval_sec (int): Seconds to wait between download-button
            polls when SellerCloud is still preparing the report.
            Defaults to ``60``.
        file_timeout_sec (int): Maximum seconds to wait for the actual
            file bytes to land in ``download_path`` once the click
            succeeds. Defaults to ``600`` (10 minutes).

    Returns:
        Path: Absolute path to the moved file (``output_path`` resolved).

    Raises:
        TimeoutError: If the file doesn't appear in ``download_path``
            within ``file_timeout_sec`` seconds after the download click.
    """
    sel = _selectors()
    log.info("Opening the report's notify-download page.")
    driver.get(download_url)

    while True:
        try:
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, sel["download_btn"]))
            ).click()
            break
        except TimeoutException:
            log.info(
                f"Report is not ready to be downloaded yet. "
                f"Waiting {poll_interval_sec}s before retrying."
            )
            time.sleep(poll_interval_sec)

    output = Path(output_path).resolve()
    extension = output.suffix or ".xlsx"
    job_id = download_url.rsplit("=", 1)[-1]
    downloaded_file = Path(download_path) / f"{job_id}{extension}"

    log.info(f"Waiting for [cyan]{downloaded_file.name}[/cyan] to finish downloading.")
    deadline = time.monotonic() + file_timeout_sec
    while time.monotonic() < deadline:
        if downloaded_file.exists():
            break
        time.sleep(5)
    else:
        raise TimeoutError(
            f"File {downloaded_file} did not appear within {file_timeout_sec}s."
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(downloaded_file), str(output))
    log.success(f"Report saved to [cyan]{output}[/cyan].")
    return output
