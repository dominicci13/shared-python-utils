from __future__ import annotations

import smtplib
import traceback
from email.message import EmailMessage


def send_error_email(
    smtp_server: str,
    smtp_port: int,
    sender_email: str,
    sender_password: str,
    recipient_email: str,
    automation_name: str,
    error: Exception,
) -> None:
    """
    Sends an error alert email when an automation fails.
    """

    message = EmailMessage()
    message["Subject"] = f"Automation Failed: {automation_name}"
    message["From"] = sender_email
    message["To"] = recipient_email

    error_details = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )

    message.set_content(
        f"""
The automation failed.

Automation:
{automation_name}

Error:
{repr(error)}

Traceback:
{error_details}
"""
    )

    with smtplib.SMTP_SSL(smtp_server, smtp_port) as smtp:
        smtp.login(sender_email, sender_password)
        smtp.send_message(message)