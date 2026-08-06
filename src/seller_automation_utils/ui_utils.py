import ctypes
import logging
import os

log = logging.getLogger(__name__)
# Windows MessageBox button and icon constants
_MB_YESNO = 0x04
_MB_ICONQUESTION = 0x20
_IDYES = 6

NO_PROMPT_ENV = "FC_NO_PROMPT"


def ask_user(message: str, title: str = "Script") -> bool:
    """Display a Windows Yes/No dialog and return the user's choice.

    Uses the native Windows MessageBoxW API to show a blocking dialog.
    The script pauses until the user clicks Yes or No.

    When ``FC_NO_PROMPT`` is set the dialog is skipped entirely and this
    returns False. Every entry point in the fleet is shaped as::

        if ask_user("Run now?", "..."):
            main()
        run_on_schedule(main, ...)

    so False means "skip the immediate run, go straight to the scheduler" —
    the right behaviour for an unattended start. Without this an automation
    launched by the ``fleet-control`` supervisor would block forever on a
    dialog box nobody is looking at, while appearing to be running.

    Args:
        message (str): The message text displayed in the dialog body.
        title (str): The dialog window title. Defaults to "Script".

    Returns:
        bool: True if the user clicked Yes, False if they clicked No or if
            ``FC_NO_PROMPT`` is set.
    """
    if os.environ.get(NO_PROMPT_ENV):
        log.info(f"[cyan]{title}[/cyan]: {NO_PROMPT_ENV} set, skipping the run-now prompt.")
        return False

    log.warning(f"Waiting for user confirmation: [cyan]{title}[/cyan].")

    result = ctypes.windll.user32.MessageBoxW(
        0,
        message,
        title,
        _MB_YESNO | _MB_ICONQUESTION
    )

    return result == _IDYES
