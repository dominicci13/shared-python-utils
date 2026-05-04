import os
import time
import win32com.client

###############################################################################################################################################
#Return the Inbox for the user provided selected account
def get_account(account: str, folder: str):

    """
    Finds the inbox object in the provided account and folder.

    Args:
        account (str): The email account to look on Outlook, in case you have multiple accounts configured on your Outlook Application (e.g., "username@server.com").
        folder (str): The folder to look on Outlook (e.g., "Inbox", "Junk", "Sent Items"). For nested folders, it has to be seppared with the slash forward character (e.g., "Folder/Subfolder").
    Returns:
        list: A list of all emails with all details.
    """
    #Get the Outlook Application object
    outlook = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")    

    #Loop through all email accounts in Outlook to find the one provided
    for acc in outlook.Folders:
        if acc.Name.lower() == account.lower():

            for subfolder in folder.split("/"):
                try:
                    acc = acc.Folders(subfolder)
                except:
                    raise ValueError(f"Folder '{subfolder}' not found in account '{account}'.")
                
            return acc.Items
        
    raise ValueError(f"Account '{account}' not found.")

###############################################################################################################################################
#Function to send an email to the provided account
def send_email(account: str, subject: str, body: str, to: list[str], cc: list[str] = None, bcc: list[str] = None, attachments: list[str] = None, show: bool = True, send: bool = True) -> None:
    """
    Sends an email to the provided account.

    Args:
        account (str): The email account to send the email from, in case you have multiple accounts configured on your Outlook Application (e.g., username@example.com).
        subject (str): The subject of the email.
        body (str): The body of the email as HTML.
        to (list[str]): The recipient's email address.
        cc (list[str], optional): The carbon copy recipient's email address. Defaults to None.
        bcc (list[str], optional): The blind carbon copy recipient's email address. Defaults to None.
        attachments (list[str], optional): A list of file paths to attach to the email. Defaults to None.
        show (bool, optional): If True, display the email before sending it. Defaults to True.
        send (bool, optional): If True, send the email. Defaults to True.

    Returns:
        None
    """
    try:
        #Initialize the Outlook Application object
        outlook = win32com.client.Dispatch("Outlook.Application")
        namespace = outlook.GetNamespace("MAPI")

        #Loop through all email accounts in Outlook to find the one provided
        selected_account = None
        for acc in namespace.Accounts:
            if acc.DisplayName.strip().lower() == account.strip().lower():
                selected_account = acc
                break
        
        #If the account was not found, raise an error
        if not selected_account:
            raise ValueError(f"Account '{account}' not found.")
        
        #Create a new email object
        email = outlook.CreateItem(0)
        email._oleobj_.Invoke(*(64209, 0, 8, 0, selected_account))

        #Set the email details
        email.Subject = subject
        email.HTMLBody = body
        email.To = "; ".join(to)

        #Add the carbon copy recipient if provided
        if cc:
            email.CC = "; ".join(cc)
        
        #Add the blind carbon copy recipient if provided
        if bcc:
            email.BCC = "; ".join(bcc)

        #Add the attachments if provided
        if attachments:
            for attachment in attachments:
                if os.path.exists(attachment):
                    email.Attachments.Add(attachment)

                else:
                    print(f"Warning: Attachment {attachment} not found.")

        if show:
            #Display the email before sending it to the user
            email.Display()
            time.sleep(5)

        if send:
            #Send the email
            email.Send()

    except Exception as e:
        print(f"An error occurred while sending the email: {str(e)}")
        raise e