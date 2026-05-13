from __future__ import annotations

import io
import os
import subprocess
import tempfile
import pyodbc
import win32clipboard
from PIL import Image
from rich import print
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

###############################################################################################################################################
def shadow_element(
    driver: object,
    host_selector: str,
    element_selector: str,
    wait: int = 10,
    css: bool = True,
    Class: bool = False,
    xpath: bool = False,
    click: bool = True,
) -> str | None:
    """Click or retrieve text from an element inside a shadow DOM.

    Exactly one of css, Class, or xpath must be True to specify how both the
    shadow host and inner element are located. If Class or xpath is True, css
    is treated as False automatically.

    Args:
        driver (object): Active SeleniumBase WebDriver instance.
        host_selector (str): Selector string for the shadow host element.
        element_selector (str): Selector string for the target element inside the shadow root.
        wait (int): Max seconds to wait for the host element. Defaults to 10.
        css (bool): Use CSS selector. Defaults to True.
        Class (bool): Use class name selector. Defaults to False.
        xpath (bool): Use XPath selector. Defaults to False.
        click (bool): Click the element if True, return its text if False. Defaults to True.

    Returns:
        str | None: The element's text if click=False, otherwise None.

    Raises:
        ValueError: If none of css, Class, or xpath is True.
    """
    if Class or xpath:
        css = False

    if Class:
        by = By.CLASS_NAME
    elif xpath:
        by = By.XPATH
    elif css:
        by = By.CSS_SELECTOR
    else:
        raise ValueError("Provide one selector type: css, Class, or xpath.")

    shadow_host = WebDriverWait(driver, wait).until(
        EC.presence_of_element_located((by, host_selector))
    )
    shadow_root = driver.execute_script("return arguments[0].shadowRoot", shadow_host)
    element = shadow_root.find_element(by, element_selector)

    if click:
        element.click()
    else:
        return element.text


###############################################################################################################################################
def first_empty_row(sheet: object, column: str, cell: str) -> int:
    """Find the first empty row in a column, starting from a given cell.

    Reads the target column's value range in a single COM call and scans
    for the first empty cell in pure Python. This is dramatically faster
    than the previous one-cell-per-iteration approach for tables with many
    rows (one COM round-trip total instead of N).

    Args:
        sheet (object): xlwings Sheet object to search.
        column (str): Column letter to check for empty values (e.g., "A").
        cell (str): Starting cell that anchors the table range (e.g., "A1").

    Returns:
        int: Row number of the first empty cell, or the row after the last
            table row if every cell is non-empty.
    """
    table = sheet.range(cell).expand("table")
    start_row = int("".join(c for c in cell if c.isnumeric()))
    end_row = start_row + table.rows.count - 1

    values = sheet.range(f"{column}{start_row}:{column}{end_row}").value
    if not isinstance(values, list):
        values = [values]

    for offset, value in enumerate(values):
        if value is None or (isinstance(value, str) and not value.strip()):
            return start_row + offset

    return end_row + 1


###############################################################################################################################################
def send_to_clipboard(clip_type: int, data: bytes) -> None:
    """Write data to the Windows clipboard.

    Args:
        clip_type (int): Clipboard format constant (e.g., win32clipboard.CF_DIB).
        data (bytes): Raw data to place on the clipboard.
    """
    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardData(clip_type, data)
    win32clipboard.CloseClipboard()


###############################################################################################################################################
def paste_image_from_clipboard(sheet: object, cell: str) -> None:
    """Read a DIB image from the clipboard and paste it into an xlwings sheet.

    Converts the clipboard bitmap to PNG, saves it to a temp file, inserts it into
    the sheet anchored at the top-left corner of the target cell, then removes the temp file.

    Args:
        sheet (object): xlwings Sheet object to paste the image into.
        cell (str): Cell address used to position the image (e.g., "B5").
    """
    image_path = os.path.join(tempfile.gettempdir(), "fc_clipboard_image.png")

    win32clipboard.OpenClipboard()
    try:
        if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_DIB):
            print("[yellow][WARNING][/yellow] No image data found in clipboard.")
            return

        data = win32clipboard.GetClipboardData(win32clipboard.CF_DIB)
        img = Image.open(io.BytesIO(data))
        img.save(image_path, "PNG")
    finally:
        win32clipboard.CloseClipboard()

    try:
        sheet.pictures.add(image_path, left=sheet.range(cell).left, top=sheet.range(cell).top)
    except Exception as e:
        print(f"[bold red][ERROR][/bold red] Failed to paste image: {e}")
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)


##################################################################################################################################################
def kill_app(app_name: str) -> None:
    """Force-kill all running instances of the specified application.

    Args:
        app_name (str): Executable name without extension (e.g., "chrome", "excel", "chromedriver").
    """
    subprocess.run(
        ["taskkill", "/f", "/im", f"{app_name}.exe"],
        capture_output=True,
    )


##################################################################################################################################################
def sql_connection(database: str) -> object:
    """Open a pyodbc connection to the local SQL Server Express instance.

    Args:
        database (str): Name of the database to connect to.

    Returns:
        object: An open pyodbc connection object.

    Raises:
        pyodbc.Error: If the connection cannot be established.
    """
    print(f"[cyan][INFO][/cyan] Connecting to SQL database: [cyan]{database}[/cyan].")
    conn = pyodbc.connect(
        "DRIVER={SQL Server};"
        r"SERVER=localhost\SQLEXPRESS;"
        f"DATABASE={database};"
    )
    print(f"[green][SUCCESS][/green] Connected to [cyan]{database}[/cyan] successfully.")
    return conn
