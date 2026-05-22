"""seller_automation_utils — shared utilities for Amazon and eBay seller automation."""

from __future__ import annotations

__version__ = "1.0.0"

from seller_automation_utils.accounts import (
    AMAZON_ACCOUNT_NAMES,
    AMAZON_URLS,
    EBAY_PROFILES,
    amazon_login,
    iter_amazon_accounts,
)
from seller_automation_utils.alert_utils import handle_crash
from seller_automation_utils.chrome import start_browser
from seller_automation_utils.file_utils import clear_directory, create_dir_structure, latest_modified_date, wait_for_download
from seller_automation_utils.config_utils import get_env, load_config, load_config_safe
from seller_automation_utils.custom_functions import (
    first_empty_row,
    kill_app,
    paste_image_from_clipboard,
    send_to_clipboard,
    shadow_element,
    sql_connection,
)
from seller_automation_utils.database_utils import insert_dataframe, safe_execute
from seller_automation_utils.ebay import customize_offers_table
from seller_automation_utils.excel_utils import paste_image_to_sheet, refresh_workbook, run_macro
from seller_automation_utils.greeting import greeting_for
from seller_automation_utils.logging_utils import setup_logging
from seller_automation_utils.outlook import get_account, get_verification_code, send_email
from seller_automation_utils.schedule_utils import run_on_schedule
from seller_automation_utils.screenshot_utils import crop_to_box, crop_to_element, paste_to_excel, save_debug_screenshot
from seller_automation_utils.sellercloud import download_report, request_custom_export
from seller_automation_utils.ui_utils import ask_user

__all__ = [
    # accounts
    "AMAZON_ACCOUNT_NAMES",
    "AMAZON_URLS",
    "EBAY_PROFILES",
    "amazon_login",
    "iter_amazon_accounts",
    # alert_utils
    "handle_crash",
    # chrome
    "start_browser",
    # file_utils
    "clear_directory",
    "create_dir_structure",
    "latest_modified_date",
    "wait_for_download",
    # config_utils
    "get_env",
    "load_config",
    "load_config_safe",
    # custom_functions
    "first_empty_row",
    "kill_app",
    "paste_image_from_clipboard",
    "send_to_clipboard",
    "shadow_element",
    "sql_connection",
    # database_utils
    "insert_dataframe",
    "safe_execute",
    # ebay
    "customize_offers_table",
    # excel_utils
    "paste_image_to_sheet",
    "refresh_workbook",
    "run_macro",
    # greeting
    "greeting_for",
    # logging_utils
    "setup_logging",
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
    "save_debug_screenshot",
    # sellercloud
    "download_report",
    "request_custom_export",
    # ui_utils
    "ask_user",
]
