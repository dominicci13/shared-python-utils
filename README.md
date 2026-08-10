# seller-automation-utils

Shared Python utilities for Amazon and eBay seller automation.

## Installation

```bash
pip install git+https://github.com/dominicci13/shared-python-utils.git
```

Or in editable mode for local development:

```bash
pip install -e .
```

## Requirements

- Python 3.10+
- Windows (uses win32com, pyodbc SQL Server Express, and Windows clipboard APIs)

## Configuration

Some modules load runtime config from `config/accounts.json` next to the entry script (falling back to the working directory). Copy the example and fill in your values:

```bash
cp config/accounts.json.example config/accounts.json
```

Sensitive values (email addresses, credentials) are loaded from a `.env` file via `get_env()`. Copy `.env.example` and fill in your values:

```bash
cp .env.example .env
```

---

## Module Index

### `accounts`
Account name maps and eBay Chrome profiles loaded from `config/accounts.json`. Amazon login via Outlook OTP.

```python
from seller_automation_utils import AMAZON_ACCOUNT_NAMES, EBAY_PROFILES, amazon_login
```

`accounts.ebay(password, driver, username=None)` handles both of eBay's sign-in layouts: password-only when the profile still knows the user, and the two-step username-then-password form when it does not. Set `eBay_user` in `.env` so the two-step path can complete unattended. A captcha splash raises a `RuntimeError` rather than a timeout — sign in by hand in that Chrome profile when it fires, since retrying automatically makes it worse.

### `alert_utils`
Capture browser screenshots, the live DOM (main document plus every iframe), and tab URLs on crash, archive all of it to disk, send a crash report via Outlook, and clean up automation processes. The archive is written *before* the email is attempted, so a broken Outlook no longer loses the traceback.

```python
import traceback
from seller_automation_utils import handle_crash

try:
    run_automation()
except Exception:
    handle_crash(driver, traceback.format_exc(), automation_name="My Job")
```

### `chrome`
Start a Chrome browser with SeleniumBase, with retry on failure.

```python
from seller_automation_utils import start_browser

driver = start_browser(user_data_dir="C:/chrome-profiles", chrome_profile="Default", retry_count=3)
```

### `config_utils`
Load JSON config files and read environment variables from `.env`.

```python
from seller_automation_utils import load_config, load_config_safe, get_env

config = load_config_safe("config/settings.json")   # returns {} if file missing
db_name = get_env("DB_NAME", required=True)
```

### `custom_functions`
General-purpose helpers: clipboard, shadow DOM, file scanning, SQL connection, process control.

```python
from seller_automation_utils import sql_connection, kill_app

conn = sql_connection("MyDatabase")
kill_app("chrome")
```

### `database_utils`
Bulk DataFrame inserts for SQL Server via pyodbc `fast_executemany` (~23× the old
per-row loop). Bind widths are pinned from the live table schema, so long strings
do not truncate; a driver error rolls back and replays row-by-row to name the
offending row. Requires the **ODBC Driver 17** connection from `sql_connection`.

```python
from seller_automation_utils import insert_dataframe

insert_dataframe(cursor, "dbo.Orders", df, columns=["OrderId", "Status"])
```

### `ebay`
Customize the eBay Active Listings table columns in the seller dashboard.

```python
from seller_automation_utils import customize_offers_table

customize_offers_table(driver, sold=True, watchers=True)
```

Each column is driven to an absolute state — the checkbox is read first and clicked only when it differs, then the click is verified (native → label → JS with a bubbling `change` event). Nothing assumes what eBay's "Restore Defaults" leaves selected, because that set changes: as of Aug 2026 it is Custom label (SKU) and Current price alone.

Columns listed in `OPTIONAL_COLUMNS` are skipped with a warning when eBay retires them from the Customize dialog (Item number and Format both went in Aug 2026); every other column is required, and a missing or unresponsive checkbox raises, so a real DOM change fails loudly instead of inserting blank rows.

### `ebay_api`
Read seller listing data through the eBay Trading API instead of the browser.

```python
from seller_automation_utils import account_token, get_active_listings, to_seller_local

listings = get_active_listings(account_token("AccountA"))
listings[0]["category"]                      # "Cameras & Photo"
to_seller_local(listings[0]["start_time"])   # naive Pacific, as SQL has always stored it
```

There is no browser here, so eBay's bot check, its React grid and the Customize dialog are all out of the picture — which is why this exists, after that dialog's Save started rejecting every request in Aug 2026.

Credentials come from the environment and are shared with `ebay-best-offers`: one app keyset (`EBAY_APP_ID` / `EBAY_DEV_ID` / `EBAY_CERT_ID`) plus a per-account user token named by `token_env_var` (`"AccountB"` → `EBAY_AUTH_TOKEN_ACCOUNTB`).

Each listing carries `item_number`, `title`, `sku`, `current_price`, `sold_quantity`, `watchers`, `start_time` (aware UTC), `category_path`, `category` (top level, `/` normalized to `-`) and `listing_status`.

Two behaviours worth knowing. `GetSellerList` selects by end time, not status, and orders results by end time ascending — so the first page is dense with listings that ended earlier the same day, and `get_active_listings` filters them out. And `GetMyeBaySelling` is deliberately not used for listing data: its items carry no category and no sold quantity. It appears only in `count_active_listings`, as an independent second opinion a sweep can check itself against.

Build and parse are pure functions kept apart from the HTTP call, so both are testable without a network.

### `excel_utils`
Open Excel workbooks, run macros, refresh Power Query, and insert images.

```python
from seller_automation_utils import refresh_workbook, run_macro, paste_image_to_sheet

refresh_workbook("C:/reports/dashboard.xlsm", wait=30)
run_macro("C:/reports/report.xlsm", "Module1.FormatSheet")
```

### `file_utils`
Directory creation, download polling, and directory cleanup.

```python
from seller_automation_utils import create_dir_structure, wait_for_download, clear_directory

create_dir_structure("C:/automation", ["logs", "output/reports"])
path = wait_for_download("C:/Downloads", extension=".csv", timeout_sec=120)
clear_directory("C:/Downloads", extension=".csv")
```

### `fleet_state`
Durable on-disk heartbeat and crash archive under `%LOCALAPPDATA%\fc-fleet`, read by the `fleet-control` dashboard. `run_on_schedule` wires this up automatically — you only touch it directly to read state back.

```python
from seller_automation_utils import read_heartbeat

beat = read_heartbeat("ebay_best_offers")
print(beat["jobs"], beat["last_result"])
```

Each beat carries every job's live `next_run_time`, so a scheduler thread that died inside a still-running process is externally visible — the one failure `handle_crash` can never report.

### `outlook`
Send emails from a configured Outlook account and poll for OTP/verification codes.

```python
from seller_automation_utils import send_email, get_verification_code

send_email("sender@example.com", subject="Report", body="<p>Done</p>", to=["boss@example.com"])
code = get_verification_code("me@example.com", sender_contains="amazon", subject_contains="OTP")
```

### `schedule_utils`
Run a function on a recurring cron schedule using APScheduler, emitting a `fleet_state` heartbeat on every tick.

```python
from seller_automation_utils import run_on_schedule

run_on_schedule(my_job, hour=8, minute=30, day_of_week="mon-fri")
```

Requires APScheduler 3.x — 4.x drops the scheduler API this is built on and is capped out in `pyproject.toml`.

### `screenshot_utils`
Crop screenshots to Selenium elements or pixel boxes, and paste into Excel.

```python
from seller_automation_utils import crop_to_element, crop_to_box, paste_to_excel

path = crop_to_element(element)
paste_to_excel("C:/reports/report.xlsm", sheet="Dashboard", cell="B5", image_path=path)
```

### `ui_utils`
Show a native Windows Yes/No dialog and return the user's choice.

```python
from seller_automation_utils import ask_user

if ask_user("Continue with upload?", title="Confirm"):
    upload()
```

Set `FC_NO_PROMPT=1` to skip the dialog and return False — required for unattended starts, which would otherwise block forever on a message box nobody is looking at.

---

## Author

Built by **Brian Ramirez** ([@dominicci13](https://github.com/dominicci13)) — automation & AI workflow specialist. More on my [GitHub profile](https://github.com/dominicci13) and [LinkedIn](https://linkedin.com/in/bdramirez).

## License

MIT — see [LICENSE](LICENSE).
