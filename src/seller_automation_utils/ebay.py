from __future__ import annotations

import time
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, ElementClickInterceptedException, ElementNotInteractableException
import logging

log = logging.getLogger(__name__)

###############################################################################################################################################
def customize_offers_table(driver: object, sold: bool = False, watchers: bool = False, views: bool = False, start_date: bool = False) -> None:
    """Customize the Active Listings table columns in the eBay seller dashboard.

    Clicks the 'Customize table' button, resets to defaults, then selects the
    specific columns needed for automation. Handles banners and intercepted clicks
    by closing dialogs or refreshing the page.

    Args:
        driver (object): Active SeleniumBase WebDriver instance.
        sold (bool, optional): Include the 'Sold Quantity' column. Defaults to False.
        watchers (bool, optional): Include the 'Watchers' column. Defaults to False.
        views (bool, optional): Include the 'Views (30 days)' column. Defaults to False.
        start_date (bool, optional): Include the 'Start Date' column. Defaults to False.
    """
    log.info("Customizing the offers table.")
    customizing_table = True
    while customizing_table:
        try:
            customize_tbl = WebDriverWait(driver, 15).until(EC.presence_of_element_located((
                By.CSS_SELECTOR,
                ".customize-link"
            )))

            driver.execute_script("arguments[0].scrollIntoView(true);", customize_tbl)
            customize_tbl.click()
            customizing_table = False

        except (TimeoutException, ElementNotInteractableException):
            try:
                WebDriverWait(driver, 5).until(EC.element_to_be_clickable((
                    By.CSS_SELECTOR,
                    "#sh-page > div.card-old > div > div.overlays > div.sme-discount-layer > span > div > div.lightbox-dialog__window.lightbox-dialog__window--animate.keyboard-trap--active > div.lightbox-dialog__header > button"
                ))).click()
            except TimeoutException:
                pass

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

    log.info("Restoring table to default columns.")
    WebDriverWait(driver, 10).until(EC.element_to_be_clickable((
        By.ID,
        "customize-restoreDefaults"
    ))).click()
    time.sleep(3)

    log.info("Selecting required columns.")

    # "Item Specifics" checkbox is not always present
    try:
        driver.find_element(By.ID, "customize-itemSpecifics").click()
    except NoSuchElementException:
        pass

    driver.find_element(By.ID, "customize-listingId").click()           # Item Number
    driver.find_element(By.ID, "customize-format").click()              # Format
    driver.find_element(By.ID, "customize-availableQuantity").click()   # Available Quantity

    if sold:
        driver.find_element(By.ID, "customize-soldQuantity").click()    # Sold Quantity

    if not views:
        driver.find_element(By.ID, "customize-visitCount").click()      # Views (30 days)

    driver.find_element(By.ID, "customize-promoteListing").click()      # Promoted Listings

    if not watchers:
        driver.find_element(By.ID, "customize-watchCount").click()      # Watchers

    if start_date:
        driver.find_element(By.ID, "customize-scheduledStartDate").click()      # Start Date

    driver.find_element(By.ID, "customize-unansweredQuestionCount").click() # Questions
    driver.find_element(By.ID, "customize-bidCount").click()            # Bids

    # "Discounts" checkbox is not always present
    try:
        driver.find_element(By.ID, "customize-promotions").click()
    except NoSuchElementException:
        pass

    log.info("Saving table configuration.")
    driver.find_element(By.ID, "customize-save").click()

    log.info("Waiting for the page to reload.")
    time.sleep(10)