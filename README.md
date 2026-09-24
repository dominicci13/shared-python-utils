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

`accounts.sellercloud(driver, username, password, site="Delta")` raises `RuntimeError` unless it ends positively signed in: on the tenant's own host, not on the login page, and with a logout link on the page. It allows 10s to find the login form or a signed-in page, and 30s after submitting (1.8.6+). Don't wrap it in a bare `except`: that turns a failed login into a run that scrapes login pages.

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
Bulk DataFrame inserts for SQL Server via pyodbc `fast_executemany` (~3-8× the old
per-row loop on 2,000-row batches; up to ~23× measured on one large load). Bind widths are pinned from the live table schema, so long strings
do not truncate. Requires the **ODBC Driver 17** connection from `sql_connection`.

It commits once, together with any uncommitted statement the caller ran first on
the same connection, so `DELETE ... WHERE ReportDate = ?` followed by
`insert_dataframe` replaces the day's rows atomically. On a driver error only the
bulk attempt is undone, back to a savepoint, so that DELETE survives; the rows are
then replayed one by one. All rows good: one commit, as the bulk path would have
made. A bad row: everything rolls back, the caller's DELETE included, and the
error names the row. If the savepoint cannot be restored, everything rolls back
and it raises without replaying, so a failure never leaves duplicates.

Column names are bracketed in the INSERT text (since 1.8.3), so hyphens, spaces,
symbols and reserved words (`your-price`, `P&L (30 days)`, `rank`) need no
caller-side quoting. Already-bracketed names (`[your-price]`) are accepted and never
double-bracketed. Each name is also the DataFrame key exactly as passed. The table
name is used verbatim in the INSERT, and `Orders`, `dbo.Orders`, `[dbo].[Orders]`
and `Reports.dbo.Orders` all get their widths pinned: the schema is passed to the
metadata lookup on its own, `_`/`%` in it are escaped, and the result is filtered
to the exact table. A malformed or four-part name raises `ValueError` before
anything is written.

```python
from seller_automation_utils import insert_dataframe

insert_dataframe(cursor, "Orders", df, columns=["OrderId", "Status", "ship-date"])
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

Retries are narrow on purpose. `get_active_listings` retries each page on its own, and `count_active_listings` and `get_item` each retry their one call: up to 3 attempts, 5s then 15s (`TRADING_RETRY_DELAYS`), and only on eBay `10007` or a transport-transient failure (HTTP 5xx, connection, timeout, dropped body). Anything else, every 4xx included, raises on the first attempt, because it fails the same way again. Pass `account=` to any of the three so each retry's WARNING names the account (`get_item`'s also names the item id). The HTTP layer underneath makes a single attempt, so a failure costs 3 requests, never 3 x 3.

Build and parse are pure functions kept apart from the HTTP call, so both are testable without a network.

Views come from the **Sell Analytics** API rather than Trading, which has no view metric:

```python
from seller_automation_utils import get_listing_views

views = get_listing_views("AccountA", ["123456789012", ...])   # {item_number: views}
```

That path needs an OAuth refresh token per account (`EBAY_OAUTH_REFRESH_TOKEN_<ACCOUNT>`), which is a different credential from the Trading token and not interchangeable with it. Requests are batched at eBay's cap of **200 listing ids**, and listings with no traffic — which eBay omits from the response rather than returning as zero — are filled in as 0.

Two things that fill matters for. Ids must be **all digits** (given as strings or ints, whitespace stripped); anything else raises `ValueError` before a single request, because an id eBay cannot match would otherwise be zero-filled and read as "nobody looked". And the returned dict is keyed by the **stripped string form**, so map results back by that, not by the value you passed in — a whitespace-padded or int id passed straight back as a key is a guaranteed miss. Duplicates are requested once and returned once.

Be aware of the quota: `sell.analytics.traffic_report` allows **500 calls per 24h for the whole application** (raised from the default 100 through eBay's Application Growth Check), shared across every automation on the keyset. It resets on eBay's own daily boundary — read `resetTime` off the Developer Analytics `rate_limit` resource rather than assuming a fixed hour, because eBay resets on Pacific midnight, which moves between 07:00 and 08:00 UTC with US DST. At the 200-id cap that is 500 x 200 = 100,000 listings a day across every caller combined, so a large seller plus a same-day rerun can still exhaust it. A 429 is a daily budget, not a burst, so no amount of backoff helps; check the remaining budget with the Developer Analytics `rate_limit` resource.

Which listings an account may send offers on comes from the **Negotiation** API, the replacement for Seller Hub's `offers=sendNewOffers` filter:

```python
from seller_automation_utils import get_offer_eligible_items

eligible = get_offer_eligible_items("AccountA")   # {"123456789012", ...}
```

Eligibility is eBay's own judgement and cannot be derived from listing data — measured against a scraped baseline, `Watchers > 0` selects ~16x too many listings and still misses eligible ones. This needs the `sell.inventory.readonly` scope. That is not a typo for a Negotiation scope: `sell.negotiation` does not authorize this call and is not grantable to every keyset. It must be present in each account's consent alongside every other scope it uses, because a refresh token only carries what it was consented for, so re-consenting for one scope alone silently drops the others.

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

### `instance_guard`
At most one running copy of each automation per host. An orphaned scheduler (its terminal closed, so Ctrl+C cannot reach it) otherwise keeps firing beside a fresh copy, and both drive Chrome on the same profile.

You do not call it: `ask_user` takes the lock as its first action (before the dialog, and whether or not `FC_NO_PROMPT` is set), and `run_on_schedule` takes it again as a backstop. It is idempotent within a process. Call `ensure_single_instance()` yourself only in an entry point that uses neither.

- **Lock:** a Windows named mutex, `Global\seller_automation_utils.single_instance.<name>`. The kernel releases it when the process dies, however it dies, so there is no stale lock to clean up. `Global\` so a copy orphaned in another logon session is still caught. The handle is not inheritable, so a Chrome or driver outliving its Python parent does not hold it.
- **Name:** `run_demo_report.py` locks as `demo_report` (casefolded). A process not started from a `run_*.py` script, and given no name, is not locked at all (one WARNING), so two unrelated scripts can never block each other.
- **Blocked copy:** logs one ERROR naming the automation and the holder's PID, then exits with status 0. No crash mail, no heartbeat, nothing written. The PID comes from `%LOCALAPPDATA%\fc-fleet\locks\<name>.json`, which is information only; the holder deletes it on a clean exit. The PID is named only while a live process with that PID and the recorded creation time exists, so a record left by a killed holder never points at an unrelated process that reused the PID; otherwise the message says the holder PID is unknown.
- **Opt-out:** set `FC_ALLOW_MULTIPLE_INSTANCES=1` (or `true` / `yes`) to run a deliberate second copy. Any other value, including `0` and `false`, leaves the guard on. It logs a WARNING so the guard is never off silently.
- **Upgrade gotcha:** the guard only sees copies that also run this version or later. A copy started before the upgrade holds no mutex, so a new copy starts beside it unblocked. Before restarting after the upgrade, confirm no old copy is still running (`Get-CimInstance Win32_Process`).

```python
from seller_automation_utils import ensure_single_instance

ensure_single_instance()                 # name from sys.argv[0]
ensure_single_instance("my_automation")  # or explicit
```

### `outlook`
Send emails from a configured Outlook account and poll for OTP/verification codes.

```python
from seller_automation_utils import send_email, get_verification_code

send_email("sender@example.com", subject="Report", body="<p>Done</p>", to=["boss@example.com"])
code = get_verification_code("me@example.com", sender_contains="amazon", subject_contains="OTP")
```

### `schedule_utils`
Run a function on a recurring cron schedule using APScheduler, emitting a `fleet_state` heartbeat on every tick. It takes the single-instance lock before anything else (a no-op if `ask_user` already has).

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

`ask_user` first takes the single-instance lock (see `instance_guard`), so a second copy of a running automation exits here, before the dialog and before any work.

---

## Author

Built by **Brian Ramirez** ([@dominicci13](https://github.com/dominicci13)) — automation & AI workflow specialist. More on my [GitHub profile](https://github.com/dominicci13) and [LinkedIn](https://linkedin.com/in/bdramirez).

## License

MIT — see [LICENSE](LICENSE).
