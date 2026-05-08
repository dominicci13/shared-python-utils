from __future__ import annotations

import io
import os
import subprocess
import tempfile
import time
import pyodbc
import win32clipboard
from PIL import Image
from rich import print
from datetime import datetime, timedelta
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

###############################################################################################################################################
def download_finished(browser: str, path: str) -> bool:
    """Check whether a browser download in the given folder has completed.

    Looks for temporary download extensions (.crdownload for Chrome, .part for Firefox).
    Returns True only when none are present.

    Args:
        browser (str): Browser name — "chrome" or "firefox" (case-insensitive).
        path (str): Folder path where the download is happening (e.g., "C:/Users/Username/Downloads").

    Returns:
        bool: True if no in-progress download files are found, False otherwise.

    Raises:
        ValueError: If an unsupported browser name is provided.
    """
    if browser.lower() == "chrome":
        return not any(f.endswith(".crdownload") for f in os.listdir(path))
    elif browser.lower() == "firefox":
        return not any(f.endswith(".part") for f in os.listdir(path))
    else:
        raise ValueError(f"Unsupported browser: {browser}")


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

    Exactly one of css, Class, or xpath must be True to specify how the shadow
    host element is located.

    Args:
        driver (object): Active SeleniumBase WebDriver instance.
        host_selector (str): Selector string for the shadow host element.
        element_selector (str): Selector string for the target element inside the shadow root.
        wait (int): Max seconds to wait for the host element. Defaults to 10.
        css (bool): Use CSS selector for the host. Defaults to True.
        Class (bool): Use class name selector for the host. Defaults to False.
        xpath (bool): Use XPath selector for the host. Defaults to False.
        click (bool): Click the element if True, return its text if False. Defaults to True.

    Returns:
        str | None: The element's text if click=False, otherwise None.

    Raises:
        ValueError: If none of css, Class, or xpath is True.
    """
    if Class or xpath:
        css = False

    if not css and not Class and not xpath:
        raise ValueError("Provide exactly one selector type: css, Class, or xpath.")

    by_map = {
        css: By.CSS_SELECTOR,
        Class: By.CLASS_NAME,
        xpath: By.XPATH,
    }
    by = by_map[True]

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
def tomorrow() -> str:
    """Return the name of tomorrow's weekday (e.g., 'Monday')."""
    return (datetime.now() + timedelta(days=1)).strftime("%A")


def yesterday() -> datetime:
    """Return a datetime object representing yesterday at the current time."""
    return datetime.now() - timedelta(days=1)


###############################################################################################################################################
def first_empty_row(sheet: object, column: str, cell: str) -> int:
    """Find the first empty row in a column, starting from a given cell.

    Args:
        sheet (object): xlwings Sheet object to search.
        column (str): Column letter to check for empty values (e.g., "A").
        cell (str): Starting cell that anchors the table range (e.g., "A1").

    Returns:
        int: Row number of the first empty cell, or the row after the last table row if none found.
    """
    table = sheet.range(cell).expand("table")
    start_row = int("".join(c for c in cell if c.isnumeric()))

    for row in range(start_row, table.rows.count + start_row):
        value = sheet.range(f"{column}{row}").value
        if value is None or str(value).strip() == "":
            return row

    return table.rows.count + start_row


###############################################################################################################################################
def files_info(path: str) -> list[dict]:
    """Return metadata for every file found under the given directory tree.

    Args:
        path (str): Root directory to walk (e.g., "C:/Users/Your_User").

    Returns:
        list[dict]: One dict per file with keys: Name, Size, Date Created,
            Date Modified, Extension, Path.
    """
    results = []
    for root, _, files in os.walk(path):
        for file_name in files:
            full_path = os.path.join(root, file_name)
            results.append({
                "Name": file_name,
                "Size": os.path.getsize(full_path),
                "Date Created": os.path.getctime(full_path),
                "Date Modified": datetime.fromtimestamp(os.path.getmtime(full_path)),
                "Extension": os.path.splitext(file_name)[1].lower(),
                "Path": full_path,
            })
    return results


###############################################################################################################################################
def find_file(filepath: str, filename: str) -> bool:
    """Poll a directory until a file with the given name prefix is found.

    Args:
        filepath (str): Directory to search (e.g., "C:/Users/Administrator/Documents").
        filename (str): Filename prefix to match (e.g., "report" matches "report_2024.csv").

    Returns:
        bool: True once the file is found.
    """
    while True:
        for file in files_info(filepath):
            if file["Name"].startswith(filename):
                print(f"[cyan][INFO][/cyan] File found: [cyan]{file['Name']}[/cyan]")
                return True
        print("[yellow][WARNING][/yellow] File not found. Trying again in 5 seconds.")
        time.sleep(5)


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
