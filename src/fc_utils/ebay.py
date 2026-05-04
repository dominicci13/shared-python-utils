import time
from rich import print
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, ElementClickInterceptedException, ElementNotInteractableException

###############################################################################################################################################
def CustomizeOffersTable(driver, sold=False, watchers=False, views=False) -> None:
    """
    Customizes the offers table in the eBay Active Listings dashboard.

    Args:
        driver (WedDriver): The WebDriver instance to execute.
        sold (bool, optional): Whether to include 'Sold' column in the table. Defaults to False.
        watchers (bool, optional): Whether to include 'Watchers' column in the table. Defaults to False.
    """
    #Click on 'Customize table' section
    print("'[INFO]' Customizing table.")
    customizing_table = True
    while customizing_table:
        print(1)
        try:
            customize_tbl = WebDriverWait(driver, 15).until(EC.presence_of_element_located((
                By.CSS_SELECTOR,
                ".customize-link"
            )))
            print(2)

            driver.execute_script("arguments[0].scrollIntoView(true);", customize_tbl)
            print(3)
            customize_tbl.click()
            print(4)
            customizing_table = False

        except (TimeoutException, ElementNotInteractableException):
            print(5)
            try:
                #Close the banner if it shows up
                WebDriverWait(driver, 5).until(EC.element_to_be_clickable((
                    By.CSS_SELECTOR,
                    "#sh-page > div.card-old > div > div.overlays > div.sme-discount-layer > span > div > div.lightbox-dialog__window.lightbox-dialog__window--animate.keyboard-trap--active > div.lightbox-dialog__header > button"
                ))).click()
                print(6)

            except TimeoutException:
                print(7)
                pass

            #Refresh the page
            driver.refresh()

        except ElementClickInterceptedException:
            print(8)
            header = driver.find_element(By.CLASS_NAME, "dialog-title").text
            print(9)

            if header == "Add or review discounts":
                print(f"'[INFO]' Closing the '{header}' dialog.")
                driver.find_element(
                    By.CSS_SELECTOR,
                    "#sh-page > div.card-old > div > div.overlays > div.sme-discount-layer > span > div > div.lightbox-dialog__window.lightbox-dialog__window--animate.keyboard-trap--active > div.lightbox-dialog__header > button"
                ).click()
                time.sleep(2)

            else:
                driver.refresh()
                time.sleep(5)

    #Wait for the new window to appear and restore table defaults
    print("'[INFO]' Restoring table values to default.")
    WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.ID, "customize-restoreDefaults"))).click()
    time.sleep(3)

    #Now select the values we actually need
    print("'[INFO]' Selecting the columns that we explicitly need.")

    #If "Item Specifics" check-box doesn't exists, continue
    try:
        driver.find_element(By.ID, "customize-itemSpecifics").click()
    except NoSuchElementException:
        pass

    driver.find_element(By.ID, "customize-listingId").click() #Item Number
    driver.find_element(By.ID, "customize-format").click() #Format
    driver.find_element(By.ID, "customize-availableQuantity").click() #Available Quantity

    if sold:
        driver.find_element(By.ID, "customize-soldQuantity").click() #Sold Quantity

    if not views:
        driver.find_element(By.ID, "customize-visitCount").click() #Views (30 days)

    driver.find_element(By.ID, "customize-promoteListing").click() #Promoted Listings

    if not watchers:
        driver.find_element(By.ID, "customize-watchCount").click() #Watchers

    driver.find_element(By.ID, "customize-unansweredQuestionCount").click() #Questions
    driver.find_element(By.ID, "customize-bidCount").click() #Bids

    #If 'Discounts' check-box doesn't exists, continue
    try:
        driver.find_element(By.ID, "customize-promotions").click()
    except NoSuchElementException:
        pass

    #Click on 'Save' button
    print("'[INFO]' Saving results.")
    driver.find_element(By.ID, "customize-save").click()

    #Wait 10 seconds to make sure all elements are restored to default
    print("'[INFO]' Waiting for the page to fully load again.")
    time.sleep(10)