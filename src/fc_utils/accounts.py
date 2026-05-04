import time
import ctypes
from rich import print
from dotenv import load_dotenv
from fc_utils import outlook
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support import expected_conditions as EC

##################################################################################################################################################
#Get the credentials from the environment
load_dotenv()
username = "user@example.com"
password = "***REDACTED***"

##################################################################################################################################################
def SellerCloud(driver: object, site: str = "Delta") -> None:
    """
    Logs in to SellerCloud.

    :param driver: Selenium WebDriver object
    :param site: SellerCloud version. Whether "Delta" or "Alpha" (Defaults to Delta)
    """
    print("'[INFO]' Logging into 'SellerCloud'.")

    if site == "Delta":
        driver.get("https://your-tenant.delta.sellercloud.com/account/login.aspx?ReturnUrl=%2f")

        try:
            UserBox = WebDriverWait(driver, 5).until(EC.presence_of_element_located((
                By.ID,
                "NewFormBody_deltaUsername"
            )))
            UserBox.send_keys(username)

            PasswordBox = driver.find_element(
                By.ID,
                "NewFormBody_deltaPass"
            )
            PasswordBox.send_keys(password)

            driver.find_element(
                By.CLASS_NAME,
                "wizard-btn-container"
            ).click()
        except TimeoutException:
            pass

    elif site == "Alpha":
        driver.get("https://fci.cwa.sellercloud.com/login.aspx?ReturnUrl=%2f")

        try:
            UserBox = WebDriverWait(driver, 5).until(EC.presence_of_element_located((
                By.CSS_SELECTOR,
                "#ContentPlaceHolder1_txtEmail"
            )))
            UserBox.send_keys(username)

            PasswordBox = driver.find_element(
                By.CSS_SELECTOR,
                "#ContentPlaceHolder1_txtPwd"
            )
            PasswordBox.send_keys(password)
            PasswordBox.send_keys(Keys.ENTER)
        except TimeoutException:
            pass

##################################################################################################################################################
def Amazon() -> dict[str]:
    """
    Returns a dictionary containing all Amazon accounts links.

    :Keys: "FocusCam", "LifeS", "XtraB", "KnoxGear", "Apple", "FocusHome"
    """

    # Create Account links
    FocusCam = "https://sellercentral.amazon.com/home?mons_sel_dir_mcid=amzn1.merchant.d.ACXK7NJLF7H4U3SN7G5QOGL7OYUQ&mons_sel_mkid=ATVPDKIKX0DER&mons_sel_dir_paid=amzn1.pa.d.ABTNG5ABZWSV5BWXEKLQ4FHXCNAA&ignore_selection_changed=true"
    LifeS = "https://sellercentral.amazon.com/home?mons_sel_dir_mcid=amzn1.merchant.d.AB2G2XGMLBWWPCLPQRWNNULHPSDA&mons_sel_mkid=ATVPDKIKX0DER&mons_sel_dir_paid=amzn1.pa.d.AAJGHULLDX4ACMZ7BDPDD5QR2MTQ&ignore_selection_changed=true"
    XtraB = "https://sellercentral.amazon.com/home?mons_sel_dir_mcid=amzn1.merchant.d.ABW4NFEWE7DTZP7LUKFFDNZ37F7Q&mons_sel_mkid=ATVPDKIKX0DER&mons_sel_dir_paid=amzn1.pa.d.ACXEZPUUIQJOFFOQOOVCY4LYZZGQ&ignore_selection_changed=true"
    KnoxGear = "https://sellercentral.amazon.com/home?mons_sel_dir_mcid=amzn1.merchant.d.ABH2KVDO7HLJ2URGLVHAAACCYGPA&mons_sel_mkid=ATVPDKIKX0DER&mons_sel_dir_paid=amzn1.pa.d.AAC5PASBYVBCT6GQT2MP2RP2Z7RA&ignore_selection_changed=true"
    Apple = "https://sellercentral.amazon.com/home?mons_sel_dir_mcid=amzn1.merchant.d.AAHQR73AEHC4HL6ZEWTSWDDSL4ZQ&mons_sel_mkid=ATVPDKIKX0DER&mons_sel_dir_paid=amzn1.pa.d.AC33RYCGPY565Q6LTAAXQIV3P25A&ignore_selection_changed=true"
    FocusHome = "https://sellercentral.amazon.com/home?mons_sel_dir_mcid=amzn1.merchant.d.ADOHJ5JJGVYBQI62WRJJL24RI5WA&mons_sel_mkid=ATVPDKIKX0DER&mons_sel_dir_paid=amzn1.pa.d.AC6H2XVZDB54VODYKBSN2D6VB6HQ&ignore_selection_changed=true"

    # Dictionary to map names to URLs
    accounts = {
        "FocusCam": FocusCam,
        "LifeS": LifeS,
        "XtraB": XtraB,
        "KnoxGear": KnoxGear,
        "Apple": Apple,
        "FocusHome": FocusHome
    }

    return accounts

##################################################################################################################################################
def Amazon_login(driver: object, username: str, password: str) -> str:
    """
    Logs in to Amazon Seller Central using the provided credentials.
    """
    def OTP() -> str:
        emails = outlook.get_account("user@example.com", "Inbox")
        emails.Sort("[ReceivedTime]", True)

        for email in emails:
            subject = email.Subject

            if subject == "Amazon OTP":
                email.Unread = False
                code = email.Body.split(" ")[0]
                email.Delete()

                return code

        return None

    ##################################################################################################################################################
    def login(username: str, password: str) -> str:
        """
        Log in to Amazon Seller Central with the provided credentials.
        """
        try:
            EmailPass = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, "#ap_email")))
            EmailPass.send_keys(username)
            EmailPass.send_keys(Keys.ENTER)
        except TimeoutException:
            pass

        try:
            InputPass = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, "#ap_password")))
            print("'[INFO]' Seller Central logged out. Trying to login.")
            InputPass.send_keys(password)
            InputPass.send_keys(Keys.ENTER)
        except TimeoutException:
            pass

        InputCode = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.CSS_SELECTOR, "#auth-mfa-otpcode")))

        code = None
        retries = 0
        while not code:
            code: str = OTP()

            if not code:
                print("'[INFO]' Waiting for the verification code to arrive.")
                time.sleep(10)
                retries += 1

            if retries > 5:
                return code

        InputCode.send_keys(code)
        InputCode.send_keys(Keys.ENTER)

        print("'[INFO]' Logged in successfully!")
        return code

    #Call function to login
    login(username, password)

##################################################################################################################################################
def Walmart(account: str, driver: object) -> None:
    """
    Logs in to Walmart accounts and looks for the validation code in your inbox.

    :param account: Name of the Walmart account
    :param driver: Selenium WebDriver object
    """

    # Look for the verification code in the most recent email from Walmart
    def getVerificationCode():
        # Establish a connection to Outlook
        messages = outlook.get_account("user@example.com", "Inbox")
        messages.Sort("[ReceivedTime]", True)

        # Loop through the emails in the inbox
        for message in messages:
            # Extract the sender's email address and subject line
            sender = message.SenderEmailAddress
            subject = str(message.Subject)

            # Extract the body of the email
            if sender.endswith("@walmart.com") and subject == "Your verification code":
                message.Unread = False
                body = message.Body
                body = body.split("\n")
                body = [item.replace("\r", "") for item in body if item != "\r"]
                
                # Extract the verification code from the email body
                print("'[INFO]' Extracting the verification code.")
                for item in body:
                    if item.startswith("W-"):
                        code = item.replace("W-", "")

                # Delete email
                message.Delete()
                return code

        return None

    if account == "SellerOrg":
        WalmartUserName = "user@example.com"
        WalmartPassword = "***REDACTED***"
    elif account == "SellerOrgTwo":
        WalmartUserName = "user@example.com"
        WalmartPassword = "***REDACTED***"

    login = False
    while not login:
        try:
            # Enter Walmart login credentials
            LoginBox = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".sso-login > form:nth-child(2) > div:nth-child(1) > span:nth-child(1) > span:nth-child(2) > span:nth-child(1) > input:nth-child(1)")))
            PasswordBox = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".custom-input-field-password-container > span:nth-child(1) > span:nth-child(2) > span:nth-child(1) > input:nth-child(1)")))

            print(f"'[INFO]' Logging in to account {account}.")
            LoginBox.send_keys(Keys.CONTROL + "a")
            LoginBox.send_keys(Keys.DELETE)
            LoginBox.send_keys(WalmartUserName)

            PasswordBox.send_keys(Keys.CONTROL + "a")
            PasswordBox.send_keys(Keys.DELETE)
            PasswordBox.send_keys(WalmartPassword)
            time.sleep(1)

            # Submit Walmart login credentials
            PasswordBox.send_keys(Keys.ENTER)
            time.sleep(2)

            # Get 2FA code from email inbox
            print("'[INFO]' Looping through all emails in the inbox.")
            VerificationCode = None
            while not VerificationCode:
                VerificationCode = getVerificationCode()

                if not VerificationCode:
                    print("'[INFO]' Waiting for the verification code to arrive.")
                    time.sleep(15)

            time.sleep(2)
            
            # Enter 2FA code
            CodeInput = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, "body > div.js-content > div > div.main-container > div.content-container > div > div > form > div.custom-input-field-container > span > span > span > input")))
            CodeInput.send_keys(VerificationCode)
            CodeInput.send_keys(Keys.ENTER)
            time.sleep(5)

            login = True
                    
        except TimeoutException:

            try:
                WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, ".top-nav-bar-module_height36__FLXIi")))
                login = True

            except TimeoutException:
                print(f"'[ERROR]' Failed to log in to account {account}. Waiting for user action.")
                BtnPressed = ctypes.windll.user32.MessageBoxW(0, "Is it necessary to log in?", "User Confirmation", 4 | 0x40)

                if BtnPressed == 6:
                    # Try logging in again
                    login = False

                elif BtnPressed == 7:
                    login = True

def eBay(password: str, driver: object) -> None:
    """
    Logs in to eBay accounts.

    :param password: eBay account password.
    :param driver: Selenium WebDriver object.
    """
    PasswordBox = WebDriverWait(driver, 15).until(EC.presence_of_element_located((
        By.ID, 
        "pass"
    )))
    print("'[INFO]' Logging into 'eBay'.")

    # Clear and enter password
    PasswordBox.send_keys(Keys.CONTROL + "a")
    PasswordBox.send_keys(Keys.DELETE)
    PasswordBox.send_keys(password)
    PasswordBox.send_keys(Keys.ENTER)

    print("'[INFO]' Logged in successfully!")