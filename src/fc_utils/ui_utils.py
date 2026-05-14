import ctypes
import logging

log = logging.getLogger(__name__)
# Windows MessageBox button and icon constants
_MB_YESNO = 0x04
_MB_ICONQUESTION = 0x20
_IDYES = 6

def ask_user(message: str, title: str = "Script") -> bool:
    """Display a Windows Yes/No dialog and return the user's choice.

    Uses the native Windows MessageBoxW API to show a blocking dialog.
    The script pauses until the user clicks Yes or No.

    Args:
        message (str): The message text displayed in the dialog body.
        title (str): The dialog window title. Defaults to "Script".

    Returns:
        bool: True if the user clicked Yes, False if they clicked No.
    """
    log.warning(f"Waiting for user confirmation: [cyan]{title}[/cyan].")

    result = ctypes.windll.user32.MessageBoxW(
        0,
        message,
        title,
        _MB_YESNO | _MB_ICONQUESTION
    )

    return result == _IDYES