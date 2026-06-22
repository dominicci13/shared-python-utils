from __future__ import annotations

import os
import threading
import time
import traceback
from typing import Callable

import pythoncom
import win32com.client
import logging

log = logging.getLogger(__name__)

_com_ready = threading.local()


def _ensure_com() -> None:
    """Initialize COM on the current thread if it isn't already.

    ``win32com.Dispatch`` requires ``CoInitialize`` on the calling thread.
    Under APScheduler the job runs on a worker thread where COM is never set
    up — unlike xlwings jobs, which initialize COM as a side effect of opening
    Excel. Initialize once per thread and leave it up for the thread's life
    (the returned Outlook objects must outlive this call, so we never
    ``CoUninitialize``).
    """
    if getattr(_com_ready, "ready", False):
        return
    pythoncom.CoInitialize()
    _com_ready.ready = True


def get_account(account: str, folder: str) -> object:
    """Return the Outlook Items collection for a given account and folder.

    Args:
        account (str): Email address of the Outlook account to access (e.g., "username@server.com").
        folder (str): Folder path to navigate to. Use "/" for nested folders (e.g., "Inbox" or "Folder/Subfolder").

    Returns:
        object: An Outlook Items collection for the specified folder.

    Raises:
        ValueError: If the account or any folder in the path is not found.
    """
    _ensure_com()
    outlook = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")

    for acc in outlook.Folders:
        if acc.Name.lower() == account.lower():
            for subfolder in folder.split("/"):
                try:
                    acc = acc.Folders(subfolder)
                except Exception:
                    raise ValueError(f"Folder '{subfolder}' not found in account '{account}'.")
            return acc.Items

    raise ValueError(f"Account '{account}' not found.")


def send_email(
    account: str,
    subject: str,
    body: str,
    to: list[str],
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    attachments: list[str] | None = None,
    show: bool = True,
    send: bool = True,
) -> None:
    """Send an email from a configured Outlook account.

    Args:
        account (str): Display name or email address of the sending Outlook account.
        subject (str): Email subject line.
        body (str): Email body as HTML.
        to (list[str]): Recipient email addresses.
        cc (list[str] | None): CC recipient email addresses. Defaults to None.
        bcc (list[str] | None): BCC recipient email addresses. Defaults to None.
        attachments (list[str] | None): Absolute file paths to attach. Defaults to None.
        show (bool): Display the email in Outlook before sending. Defaults to True.
        send (bool): Send the email after display. Defaults to True.

    Raises:
        ValueError: If the specified account is not found in Outlook.
        Exception: Re-raises any COM or send failure after logging.
    """
    try:
        _ensure_com()
        outlook = win32com.client.Dispatch("Outlook.Application")
        namespace = outlook.GetNamespace("MAPI")

        selected_account = None
        for acc in namespace.Accounts:
            if acc.DisplayName.strip().lower() == account.strip().lower():
                selected_account = acc
                break

        if not selected_account:
            raise ValueError(f"Account '{account}' not found.")

        email = outlook.CreateItem(0)
        email._oleobj_.Invoke(*(64209, 0, 8, 0, selected_account))

        email.Subject = subject
        email.HTMLBody = body
        email.To = "; ".join(to)

        if cc:
            email.CC = "; ".join(cc)

        if bcc:
            email.BCC = "; ".join(bcc)

        if attachments:
            for attachment in attachments:
                if os.path.exists(attachment):
                    email.Attachments.Add(attachment)
                else:
                    log.warning(f"Attachment not found: [cyan]{attachment}[/cyan]")

        if show:
            email.Display()
            time.sleep(5)

        if send:
            email.Send()

    except Exception:
        log.error("An error occurred while sending the email.")
        traceback.print_exc()
        raise


def get_verification_code(
    account: str,
    sender_contains: str,
    subject_contains: str,
    timeout_sec: int = 60,
    body_extractor: Callable[[str], str] = lambda body: body.split()[0],
    consume: bool = False,
) -> str | None:
    """Poll an Outlook inbox for an OTP/verification-code email and extract the code.

    Checks the inbox every 10 seconds until the timeout is reached. When a matching
    email is found, passes its plain-text body to `body_extractor` to pull out the code.

    Args:
        account (str): Email address of the Outlook account to check (e.g., "username@server.com").
        sender_contains (str): Substring to match against the sender address (case-insensitive).
            Pass "" to match any sender.
        subject_contains (str): Substring to match against the email subject (case-insensitive).
        timeout_sec (int): Maximum seconds to wait before giving up. Defaults to 60.
        body_extractor (Callable[[str], str]): Function that receives the email body and
            returns the verification code. Defaults to returning the first whitespace-delimited token.
        consume (bool): If True, mark the matched message as read and delete it after the
            code is extracted, so subsequent calls don't see the same OTP. Defaults to False.

    Returns:
        str | None: The extracted verification code, or None if no matching email was found
            before the timeout.
    """
    inbox = get_account(account, "Inbox")

    for _ in range(timeout_sec // 10):
        inbox.Sort("[ReceivedTime]", True)
        for message in inbox:
            try:
                sender = (message.SenderEmailAddress or "").lower()
                subject = (message.Subject or "").lower()
                if sender_contains.lower() in sender and subject_contains.lower() in subject:
                    code = body_extractor(message.Body)
                    if consume:
                        message.Unread = False
                        message.Delete()
                    return code
            except Exception:
                continue
        time.sleep(10)

    return None
