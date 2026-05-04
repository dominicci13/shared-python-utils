from rich import print
from seleniumbase import Driver

##################################################################################################################################################
def start_browser(user_data_dir: str, ChromeProfile: str, headless=True) -> object:
    """Creates a new browser instance with Selenium, using your own profile settings.

    Args:
        user_data_dir (str): Full path for the folder where Google Chrome store its profiles. \n
                            On Windows: "C:/Users/YourUsername/AppData/Local/Google/Chrome/User Data" \n
                            On MacOS: "/Users/YourUsername/Library/Application Support/Google/Chrome"

        ChromeProfile (str): The name of your profile folder inside the user_data_dir path. \n
                            Default folder names: "Default", "Profile 1", "Profile 2".

        headless (bool, optional): Set as False if you want to see browser activity. Defaults to True.

    Returns:
        object: Returns the WebDriver object that will be used to start the browser.
    """
    driver = Driver(uc=True, 
                        user_data_dir=user_data_dir, 
                        chromium_arg=f"--profile-directory={ChromeProfile}",
                        headless=headless)

    if headless:
        print("'[INFO]' Starting Google Chrome in headless mode.")
    else:
        print("'[INFO]' Starting Google Chrome.")
        driver.maximize_window()

    return driver