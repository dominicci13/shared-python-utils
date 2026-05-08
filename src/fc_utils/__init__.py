"""fc_utils — shared utilities for Amazon and eBay seller automation."""

from __future__ import annotations

__version__ = "0.2.0"

from fc_utils.accounts import (
    AMAZON_ACCOUNT_NAMES,
    AMAZON_URLS,
    EBAY_PROFILES,
    amazon_login,
)
from fc_utils.alert_utils import handle_crash, send_error_email
from fc_utils.chrome import start_browser
from fc_utils.file_utils import clear_directory, create_dir_structure, wait_for_download
from fc_utils.config_utils import get_env, load_config, load_config_safe
from fc_utils.custom_functions import (
    download_finished,
    files_info,
    find_file,
    first_empty_row,
    kill_app,
    paste_image_from_clipboard,
    send_to_clipboard,
    shadow_element,
    sql_connection,
    tomorrow,
    yesterday,
)
from fc_utils.database_utils import insert_dataframe, safe_execute, upsert_dataframe
from fc_utils.ebay import CustomizeOffersTable
from fc_utils.excel_utils import paste_image_to_sheet, refresh_workbook, run_macro
from fc_utils.logging_utils import setup_logger
from fc_utils.outlook import get_account, get_verification_code, send_email
from fc_utils.schedule_utils import run_on_schedule
from fc_utils.screenshot_utils import crop_to_box, crop_to_element, paste_to_excel
from fc_utils.ui_utils import ask_user

__all__ = [
    # accounts
    "AMAZON_ACCOUNT_NAMES",
    "AMAZON_URLS",
    "EBAY_PROFILES",
    "amazon_login",
    # alert_utils
    "handle_crash",
    "send_error_email",
    # chrome
    "start_browser",
    # file_utils
    "clear_directory",
    "create_dir_structure",
    "wait_for_download",
    # config_utils
    "get_env",
    "load_config",
    "load_config_safe",
    # custom_functions
    "download_finished",
    "files_info",
    "find_file",
    "first_empty_row",
    "kill_app",
    "paste_image_from_clipboard",
    "send_to_clipboard",
    "shadow_element",
    "sql_connection",
    "tomorrow",
    "yesterday",
    # database_utils
    "insert_dataframe",
    "safe_execute",
    "upsert_dataframe",
    # ebay
    "CustomizeOffersTable",
    # excel_utils
    "paste_image_to_sheet",
    "refresh_workbook",
    "run_macro",
    # logging_utils
    "setup_logger",
    # outlook
    "get_account",
    "get_verification_code",
    "send_email",
    # schedule_utils
    "run_on_schedule",
    # screenshot_utils
    "crop_to_box",
    "crop_to_element",
    "paste_to_excel",
    # ui_utils
    "ask_user",
]
