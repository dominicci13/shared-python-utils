import os
import time
from datetime import datetime
from selenium import webdriver
from webdriver_manager.firefox import GeckoDriverManager
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions

#Get current time name and directory
now = datetime.now().strftime("%H:%M:%S")
directory = os.getcwd()

##################################################################################################################################################
#Set Firefox browser properties
def BrowserService():
    """
    Returns a FirefoxService object for the GeckoDriver.
    """
    #Caching the GeckoDriver to avoid re-downloading or checking every time
    print(f"{now} - Installing GeckoDriver...")
    service = FirefoxService(executable_path=GeckoDriverManager().install())

    return service

def BrowserOptions(account: str, headless=False):
    """
    Returns a FirefoxOptions object for the specified account.

    :param account: The name of the account or profile created for this FirefoxDriver session.
    :param headless: Set to True to launch Firefox in headless mode (False by default).
    :return: A FirefoxOptions object for the specified account.
    """
    now = datetime.now().strftime("%H:%M:%S")
    print(f"{now} - Getting Firefox options for {account} profile...")

    options = FirefoxOptions()
    #options.set_preference("network.proxy.allow_hijacking_localhost", True)
    #options.set_preference("security.sandbox.content.level", 5)
    #options.add_argument('--ignore-certificate-errors')
    #options.add_argument('--ignore-ssl-errors')
    #options.add_argument('--allow-running-insecure-content')

    if headless:
        options.add_argument('--headless')
        print(f"{now} - Configuring Firefox in headless mode...")
        
    #options.binary_location = FirefoxBinPath
    options.profile = fr"{directory}\\Mozilla Firefox\\Profiles\\{account}" #Path to select Firefox profile

    return options

#Open browser
def LaunchFirefox(service, options):
    """
    Launches a Firefox browser with the specified service and options.
    
    :param service: A FirefoxService object for the GeckoDriver.
    :param options: A FirefoxOptions object for the specified account.
    :return: A WebDriver object for the launched Firefox browser.
    """
    now = datetime.now().strftime("%H:%M:%S")
    print(f"{now} - Starting Firefox...")

    driver = webdriver.Firefox(service=service, options=options)
    driver.maximize_window()

    now = datetime.now().strftime("%H:%M:%S")
    print(f"{now} - Firefox is now open...")

    return driver
