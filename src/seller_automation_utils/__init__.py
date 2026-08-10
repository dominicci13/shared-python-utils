"""seller_automation_utils — shared utilities for Amazon and eBay seller automation."""

from __future__ import annotations

# Read from installed package metadata rather than hardcoded: this constant has
# silently lagged the real version twice (stuck at 1.0.1 through 1.0.3, then at
# 1.1.1 through 1.1.2 and 1.2.0), which makes fleet-wide version audits lie.
try:
    from importlib.metadata import PackageNotFoundError, version as _version

    __version__ = _version("seller-automation-utils")
except PackageNotFoundError:  # running from a source tree, never installed
    __version__ = "0.0.0+unknown"

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
from seller_automation_utils.ebay_api import (
    account_token,
    count_active_listings,
    get_active_listings,
    l1_category,
    to_seller_local,
    token_env_var,
)
from seller_automation_utils.excel_utils import paste_image_to_sheet, refresh_workbook, run_macro
from seller_automation_utils.fleet_state import (
    HeartbeatWriter,
    automation_name,
    read_heartbeat,
    record_crash,
    snapshot_jobs,
)
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
    # ebay_api
    "account_token",
    "count_active_listings",
    "get_active_listings",
    "l1_category",
    "to_seller_local",
    "token_env_var",
    # excel_utils
    "paste_image_to_sheet",
    "refresh_workbook",
    "run_macro",
    # fleet_state
    "HeartbeatWriter",
    "automation_name",
    "read_heartbeat",
    "record_crash",
    "snapshot_jobs",
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
