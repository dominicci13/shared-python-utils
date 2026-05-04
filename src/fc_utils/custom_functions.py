import os
import io
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
def DownloadFinished(browser: str, path: str) -> bool:
    """
    Check if a download is finished by checking if the file with the temporary extension is gone (e.g. ".crdownload" if Chrome, or ".part" if Firefox).

    :param browser: The browser used to download the file (e.g. "Chrome" or "Firefox").
    :param path: The folder path where the download is happening (e.g. "C:\\User\\YourUsername\\Downloads").

    :return bool: True if the download is finished, False otherwise.
    """
    if browser.lower() == "chrome":
        for file in os.listdir(path):
            if file.endswith(".crdownload"):
                return False
            
        return True
    
    elif browser.lower() == "firefox":
        for file in os.listdir(path):
            if file.endswith(".part"):
                return False
        
        return True
            
    else:
        raise ValueError(f"Unsupported browser: {browser}")

###############################################################################################################################################
def shadow_element(driver: object, host_selector: str, element_selector: str, wait: int = 10, css: bool = True, Class: bool = False, xpath: bool = False, click=True) -> str:
    """
    Clicks an element located within a shadow DOM using the provided CSS selectors.

    :param driver: The WebDriver instance to execute.
    :param host_selector: The CSS selector for the host element within the shadow DOM.
    :param element_selector: The CSS selector for the element within the shadow DOM.
    :param (int, optional) wait: The maximum time to wait for the element to be present. Defaults to 10.
    :param (bool, optional) css: If True, uses CSS selector for the host element. Defaults to True.
    :param (bool, optional) Class: If True, uses class selector for the host element. Defaults to False.
    :param (bool, optional) xpath: If True, uses XPath selector for the host element. Defaults to False.
    :param (bool, optional) click: Whether to click the element after finding it. If set to False, it retrieves the text. Defaults to True.

    :return: None if the element is found and clicked, otherwise raises an exception.
    """
    #Check if at least one of the selectors is provided
    if Class or xpath:
        css = False

    #Check if at least one of the selectors is provided
    if not css and not Class and not xpath:
        raise ValueError("Please provide either a CSS selector, a Class selector or a XPATH selector.")


    if css:
        #Wait for the host element to be present and accessible within the shadow DOM
        shadow_host = WebDriverWait(driver, wait).until(EC.presence_of_element_located((
            By.CSS_SELECTOR,
            host_selector
        )))
        shadow_root = driver.execute_script('return arguments[0].shadowRoot', shadow_host)

        #Use JavaScript to access the shadow root and then find the element within it
        element = shadow_root.find_element(By.CSS_SELECTOR, element_selector)

    elif Class:
        #Wait for the host element to be present and accessible within the shadow DOM
        shadow_host = WebDriverWait(driver, wait).until(EC.presence_of_element_located((
            By.CLASS_NAME,
            host_selector
        )))
        shadow_root = driver.execute_script('return arguments[0].shadowRoot', shadow_host)

        #Use JavaScript to access the shadow root and then find the element within it
        element = shadow_root.find_element(By.CLASS_NAME, element_selector)

    elif xpath:
        #Wait for the host element to be present and accessible within the shadow DOM
        shadow_host = WebDriverWait(driver, wait).until(EC.presence_of_element_located((
            By.XPATH,
            host_selector
        )))
        shadow_root = driver.execute_script('return arguments[0].shadowRoot', shadow_host)

        #Use JavaScript to access the shadow root and then find the element within it
        element = shadow_root.find_element(By.XPATH, element_selector)

    if click:
        element.click()
    else:
        return element.text

###############################################################################################################################################
#Get tomorrow day
def tomorrow() -> str:
    tomorrow: datetime = datetime.now() + timedelta(days=1)
    return tomorrow.strftime("%A")

#Get yesterday day
def yesterday() -> str:
    yesterday: datetime = datetime.now() - timedelta(days=1)
    return yesterday

###############################################################################################################################################
def first_empty_row(sheet: object, column: str, cell: str) -> int:

    """
    Finds the first empty row in the specified column of a sheet starting from a given row.

    :param sheet: The sheet object (could be from libraries like xlwings or openpyxl).
    :param column: The column letter to check for the empty row (e.g., "A").
    :param cell: The starting cell that defines the table's start range in the sheet (e.g., "A1").

    :return: int: The row number of the first empty row or the next available row after the table.
    """
    #Get the table range starting at the provided cell
    table = sheet.range(cell).expand("table")
    initial_range = int("".join([char for char in cell if char.isnumeric()]))

    #Iterate over rows in the table range
    for row in range(initial_range, table.rows.count + initial_range):
        cell_value = sheet.range(f"{column}{row}").value

        #Check if the first cell in the row is empty
        if cell_value is None or str(cell_value).strip() == "":
            return row
        
    #If no empty row is found, return the row after the last row in the table
    return table.rows.count + initial_range

###############################################################################################################################################
def files_info(path: str) -> dict[str]:

    """
    Finds all files on the given path.

    :param path: The full root path to check (e.g., "C:\\Users\\Your_User").

    :return dict: A dictionary with details of all files that were found in the main path provided and all subfolders. \n
        The provided details are: Name, Size, Date Modified, Extension and full Path.
    """
        
    #Prints information about files in a given directory, including their names, sizes, date modified, and file extensions.
    files_info = []
    for root, dirs, files in os.walk(path):

        for file_name in files:

            file_info = {
                "Name": file_name,
                "Size": os.path.getsize(os.path.join(path, root, file_name)),
                "Date Created": os.path.getmtime(os.path.join(path, root, file_name)),
                "Date Modified": datetime.fromtimestamp(os.path.getmtime(os.path.join(path, root, file_name))),
                "Extension": os.path.splitext(file_name)[1].lower(),
                "Path": os.path.join(path, root, file_name)
            }
            files_info.append(file_info)

    return files_info

###############################################################################################################################################
def update_directory(workbook: object) -> None:

    """
    Writes the current working directory in a given Excel workbook, in the already existing DataVal sheet (If sheet does not exists, it will return an error).

    :param workbook: The workbook object (could be from libraries like xlwings or openpyxl).
    """

    directory: str = os.getcwd()
    sheet = workbook.sheets("DataVal")
    sheet.range("B1").value = directory

    print("'[INFO]' Directory updated successfully.")

###############################################################################################################################################
def find_file(filepath: str, filename: str) -> bool:
    """
    Loops through all files in the given directory and keeps looping until found.

    :param filepath: Path to the file (e.g., "C:\\Users\\Administrator\\Documents")
    :param filename: Name of the file to search for (e.g., "example.txt")

    :return: True if the file exists.
    """

    #Check if the specified file exists in the given directory
    files = []
    found = False

    while not found:
        files = files_info(filepath)
        
        for file in files:
            name: str = file['Name']

            if name.startswith(filename):
                print(f"'[INFO]' File found: {name}")
                found = True
                return found
        
        if not found:
            print("'[WARNING]' File not found. Trying again in 5 seconds...")
            time.sleep(5)

###############################################################################################################################################
#Create clipboard function to copy objects to clipboard
def send_to_clipboard(clip_type, data) -> None:
    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardData(clip_type, data)
    win32clipboard.CloseClipboard()

###############################################################################################################################################
#Function to paste image from clipboard to Excel using xlwings
def paste_image_from_clipboard(sheet, cell) -> None:
    image_path: str = os.path.abspath('temp_image.png')
    win32clipboard.OpenClipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_DIB):
            data: str = win32clipboard.GetClipboardData(win32clipboard.CF_DIB)
            bmp_data = io.BytesIO(data)
            bmp_data.seek(0)
            img = Image.open(bmp_data)

            # Convert BMP to PNG and save
            img.save(image_path, 'PNG')

            # Debugging: Check if the file is saved and accessible
            if os.path.exists(image_path):

                # Add image to Excel sheet
                try:
                    sheet.pictures.add(image_path, left=sheet.range(cell).left, top=sheet.range(cell).top)
                except Exception as e:
                    print(f"'[ERROR]' Failed to paste image: {e}")
            else:
                print(f"'[ERROR]' Failed to save image to {image_path}")
        else:
            print("'[WARNING]' No image data found in clipboard.")
    finally:
        win32clipboard.CloseClipboard()

##################################################################################################################################################
def kill_app(app_name: str) -> None:
    """
    Kills all instances of a specified application.

    Args:
        app_name (str): Name of the application to kill. \n
        Examples: "chrome", "firefox", "excel", etc.
    """
    #Using taskkill command to kill all instances of chrome.exe
    os.system(f"taskkill /f /im {app_name}.exe")

##################################################################################################################################################
def SQLConnection(database: str) -> object:
    """
    Establishes a connection to a specified SQL database using pyodbc.

    Args:
        database (str): Name of the database to connect to.

    Returns:
        conn (object): The connection object for the SQL connection to the database.
    """
    print(f"'[INFO]' Connecting with SQL Database: '{database}'.")
    conn = pyodbc.connect(
        "DRIVER={SQL Server};"
        "SERVER=localhost\SQLEXPRESS;"
        f"DATABASE={database};"
    )

    print(f"'[INFO]' Successfully connected to the '{database}' database!")
    return conn