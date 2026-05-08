# fc-utils

Shared Python utilities for Amazon and eBay seller automation.

## Installation

```bash
pip install git+https://github.com/***REDACTED***/shared-python-utils.git
```

Or in editable mode for local development:

```bash
pip install -e .
```

## Requirements

- Python 3.10+
- Windows (uses win32com, pyodbc SQL Server Express, and Windows clipboard APIs)

## Configuration

Some modules load runtime config from `config/accounts.json` in the working directory. Copy the example and fill in your values:

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
from fc_utils import AMAZON_ACCOUNT_NAMES, EBAY_PROFILES, amazon_login
```

### `alert_utils`
Send error alert emails and attach crash tracebacks via SMTP.

```python
from fc_utils import send_error_email, handle_crash

send_error_email(subject="Job failed", body="<p>Details</p>")
```

### `chrome`
Start a Chrome browser with SeleniumBase, with retry on failure.

```python
from fc_utils import start_browser

driver = start_browser(user_data_dir="C:/chrome-profiles", chrome_profile="Default", retry_count=3)
```

### `config_utils`
Load JSON config files and read environment variables from `.env`.

```python
from fc_utils import load_config, load_config_safe, get_env

config = load_config_safe("config/settings.json")   # returns {} if file missing
db_name = get_env("DB_NAME", required=True)
```

### `custom_functions`
General-purpose helpers: clipboard, shadow DOM, file scanning, SQL connection, date utilities.

```python
from fc_utils import sql_connection, kill_app, tomorrow, yesterday

conn = sql_connection("MyDatabase")
kill_app("chrome")
```

### `database_utils`
Parameterized DataFrame inserts and upserts for SQL Server via pyodbc.

```python
from fc_utils import insert_dataframe, upsert_dataframe

insert_dataframe(cursor, "dbo.Orders", df, columns=["OrderId", "Status"])
upsert_dataframe(cursor, "dbo.Inventory", df, columns=["Sku", "Qty"], key_columns=["Sku"])
```

### `ebay`
Customize the eBay Active Listings table columns in the seller dashboard.

```python
from fc_utils import CustomizeOffersTable

CustomizeOffersTable(driver, sold=True, watchers=True)
```

### `excel_utils`
Open Excel workbooks, run macros, refresh Power Query, and insert images.

```python
from fc_utils import refresh_workbook, run_macro, paste_image_to_sheet

refresh_workbook("C:/reports/dashboard.xlsm", wait=30)
run_macro("C:/reports/report.xlsm", "Module1.FormatSheet")
```

### `file_utils`
Directory creation, download polling, and directory cleanup.

```python
from fc_utils import create_dir_structure, wait_for_download, clear_directory

create_dir_structure("C:/automation", ["logs", "output/reports"])
path = wait_for_download("C:/Downloads", extension=".csv", timeout_sec=120)
clear_directory("C:/Downloads", extension=".csv")
```

### `logging_utils`
Set up a Rich-formatted logger with optional file output.

```python
from fc_utils import setup_logger

logger = setup_logger("my_script", log_file="logs/my_script.log")
logger.info("Starting job")
```

### `outlook`
Send emails from a configured Outlook account and poll for OTP/verification codes.

```python
from fc_utils import send_email, get_verification_code

send_email("sender@example.com", subject="Report", body="<p>Done</p>", to=["boss@example.com"])
code = get_verification_code("me@example.com", sender_contains="amazon", subject_contains="OTP")
```

### `schedule_utils`
Run a function on a recurring cron schedule using APScheduler.

```python
from fc_utils import run_on_schedule

run_on_schedule(my_job, hour=8, minute=30, day_of_week="mon-fri")
```

### `screenshot_utils`
Crop screenshots to Selenium elements or pixel boxes, and paste into Excel.

```python
from fc_utils import crop_to_element, crop_to_box, paste_to_excel

path = crop_to_element(element)
paste_to_excel("C:/reports/report.xlsm", sheet="Dashboard", cell="B5", image_path=path)
```

### `ui_utils`
Show a native Windows Yes/No dialog and return the user's choice.

```python
from fc_utils import ask_user

if ask_user("Continue with upload?", title="Confirm"):
    upload()
```

---

## License

MIT — see [LICENSE](LICENSE).
