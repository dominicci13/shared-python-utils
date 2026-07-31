# Changelog

## 1.3.0 — 2026-07-30

### Changed
- **`custom_functions.sql_connection`: driver `{SQL Server}` (legacy) → `{ODBC Driver 17 for SQL Server}`, plus `Trusted_Connection=yes`.** `fast_executemany` only accelerates on the Driver 11/17/18 family, so this is a prerequisite for the insert change below. **This affects reads as well as writes** — date, decimal and unicode binding differ between the legacy driver and Driver 17, for every query in every repo that calls `sql_connection`.
- **`database_utils.insert_dataframe`: per-row `execute` loop → a single `fast_executemany` `executemany`.** Measured on real AllItems data: ~600 rows/s → **14,117 rows/s (~23×)**.
  - New `_input_sizes()` pins each column's bind width from the live table schema (`cursor.columns()`). **Required, not an optimization**: `fast_executemany` otherwise sizes string parameters from the *first* row, so a longer later value raises `String data, right truncation`. It also pins `date`/`datetime` columns as WVARCHAR, because callers bind those as strings and the driver otherwise raises `Invalid character value for cast specification`.
  - The bulk insert runs on a **dedicated cursor on the caller's connection**, so `fast_executemany`/`setinputsizes` never leak onto the caller's cursor, while a preceding uncommitted `DELETE` stays in the same transaction — atomic delete-then-insert is preserved.
  - On driver error: rollback, then replay row-by-row on a clean cursor. A genuinely bad row is named in the `RuntimeError` for the crash email and re-raised; otherwise the row-by-row inserts are committed, since the fast path merely could not bulk-bind those types.

### Tests
- Added `tests/test_database_input_sizes.py` (28 cases) covering every `_input_sizes` branch against a fake cursor: all six string type codes, the 4000-char boundary, `>4000`/`0`/`None`/negative widths collapsing to `(n)varchar(max)`, decimal precision and scale, decimal defaults, all six date/time type names case-insensitively, unknown columns, non-pinned types, positional alignment to the requested column order, and the table actually introspected. A wrong width here corrupts data silently, which is why it is unit-tested rather than left to per-repo checks.

### Upgrade notes
- Requires **ODBC Driver 17 for SQL Server** on the machine running the automation. Check with `python -c "import pyodbc; print(pyodbc.drivers())"`.
- Rollback: `pip install seller-automation-utils==1.2.1` in the affected repo.
- Repos calling `insert_dataframe` or `sql_connection` (9): `amzn-catalog-health`, `amzn-ca-fba-inventory`, `amzn-feedback-manager`, `amzn-prime-orders`, `amzn-top-sales`, `ebay-avg-sold-price`, `ebay-best-offers`, `ebay-items-categories`, `sellercloud-sync`.
- `pricing-monitor` keeps its own copy of `sql_connection` (`src/catalog.py`) and is unaffected, but now diverges from the fleet on the driver.

---

## 1.2.1 — 2026-07-30

### Fixed
- `__init__.__version__` is now read from installed package metadata (`importlib.metadata.version`) instead of being hardcoded. The constant had silently lagged the real version twice — stuck at `1.0.1` through the `1.0.3` release, then at `1.1.1` through both `1.1.2` and `1.2.0`. Anything auditing the fleet by importing the package and reading `__version__` got a wrong answer: during the 1.2.0 rollout it reported every repo as failing to upgrade when all 19 had in fact installed correctly. Falls back to `0.0.0+unknown` when imported from a source tree that was never installed.

### Packaging
- Bumped version to `1.2.1`

---

## 1.2.0 — 2026-07-30

### Added
- `alert_utils.handle_crash`: crash emails now carry the live DOM as a `<automation>_crash_dom.txt` attachment alongside the screenshot, deleted after send like the screenshot is. Prompted by the `amzn-account-health` outage the same day: Amazon moved the Program Eligibilities metric cards into iframes, and the screenshot proved only *that* the page had changed, not *what the new selectors were* — diagnosing it cost a login and several rounds of live DOM inspection.
- `alert_utils._collect_dom`: walks the frame tree rather than calling `driver.page_source` once. This is the whole point — `page_source` returns only the top-level document, and on that exact page it captures 157k chars of shell and **none** of the three widget iframes holding the real content, reproducing the very blind spot the attachment exists to remove. Measured on the live page: `page_source` 156,959 chars with zero hits for "Premium Shipping" / "Seller Fulfilled" / "program-card--PSO"; the frame walk 559,113 chars with all of them.

### Notes
- Capture never breaks the alert. A dead session, an unreadable frame, or a frame that cannot be entered is noted inline and skipped; if not one document is readable the attachment is dropped entirely rather than shipping a file of error markers next to the traceback already in the email body.
- The DOM is captured **before** the open-tab sweep, since that loop leaves the driver on the last window handle rather than the one that crashed.
- Bounded by `MAX_DOM_CHARS` (8M) and `MAX_FRAME_DEPTH` (3), so a runaway page cannot produce an unsendable attachment. Truncation is stated in the file.
- The driver is returned to the top-level document afterwards, leaving the existing tab sweep unaffected.

### Tests
- Added `tests/test_alert_utils_dom.py` (12 cases) driven by a fake WebDriver: frame descent, nesting, depth cap, size cap, unreadable frame, un-enterable frame, restoration to default content, file header, and a dead-session driver.

### Packaging
- Bumped version to `1.2.0`

---

## 1.1.2 — 2026-07-30

### Fixed
- `ebay.customize_offers_table`: a `StaleElementReferenceException` escaped the retry handler and killed the calling automation. When Seller Hub serves its own error state ("Something went wrong. Please try again."), `.customize-link` is present but not interactable, so the `ElementNotInteractableException` branch runs — but its best-effort probe for the discount dialog raised *stale* rather than timing out, because the page was re-rendering underneath it. The inner `except` caught only `TimeoutException`, so the stale error propagated out of the function and aborted the run (`ebay-items-categories`, 2026-07-30 12:23). The probe now swallows `StaleElementReferenceException`, `NoSuchElementException`, `ElementNotInteractableException` and `ElementClickInterceptedException` as well — the `driver.refresh()` is the real recovery path.

### Changed
- `ebay.customize_offers_table`: the retry is now bounded at 5 attempts and raises `RuntimeError` when exhausted. It was an unbounded `while`, so a sustained Seller Hub outage hung the job indefinitely instead of failing — meaning no crash alert ever fired and a scheduled run could sit wedged until killed by hand.

### Tests
- Added `tests/test_ebay_customize.py` (11 cases): healthy path, scroll-before-click, recovery after not-interactable, the bounded give-up, both intercepted-click dialog branches, and a parametrized case asserting no probe exception escapes.

### Packaging
- Bumped version to `1.1.2`

---

## 1.1.1 — 2026-06-22

### Fixed
- `outlook.send_email` / `outlook.get_account`: `win32com.client.Dispatch("Outlook.Application")` raised `com_error -2147221008 'CoInitialize has not been called'` when invoked from an APScheduler worker thread. COM must be initialized per-thread, and the scheduler's worker thread never was — jobs that talk to Outlook from a non-main thread without first making an xlwings (Excel COM) call would fail at the email step, taking down the crash-alert path with them. Both functions now call a thread-local-guarded `_ensure_com()` (`pythoncom.CoInitialize()` once per thread, no `CoUninitialize` since returned Outlook objects must outlive the call) before any `Dispatch`.

### Packaging
- Bumped version to `1.1.1`

---

## 1.1.0 — 2026-06-01

### New
- `ebay.customize_offers_table()` — added `start_date: bool = False` parameter that toggles the eBay 'Start Date' column (`#customize-scheduledStartDate`) in the Active Listings table. Backward-compatible — existing callers default to the prior behavior. First consumer is `ebay-items-categories`, which now captures listing start timestamps into SQL `DATETIME2`.

### Packaging
- Bumped version to `1.1.0`

---

## 1.0.3 — 2026-05-26

### Fixed
- `schedule_utils.run_on_schedule`: scheduled jobs were inheriting APScheduler's default `misfire_grace_time` of 1 second. Normal scheduler jitter on Windows (~1–2 s) routinely exceeded that window, so APScheduler flagged the run as "missed" and silently skipped it — the job never executed and only a `Run time of job … was missed by …` warning was logged. Jobs are now added with `misfire_grace_time=30` and explicit `coalesce=True`, so runs fire reliably while a badly delayed fire still defers to its next clean slot.

### Packaging
- Bumped version to `1.0.3`; re-synced `__version__` in `__init__.py` (was lagging at `1.0.1`).

---

## 1.0.2 — 2026-05-25

### Fixed
- `sellercloud.download_report`: refresh the notify-download page after each wait interval in the report-ready poll loop. The loop previously re-checked the same stale page, so the download button never appeared once SellerCloud finished generating the report; the page is now reloaded so the button is detected on the next poll.

### Packaging
- Bumped version to `1.0.2`

---

## 1.0.1 — 2026-05-24

### Quality
- Added a `tests/` suite with unit coverage for the pure-function modules: `greeting.greeting_for`, `config_utils.get_env`, and `file_utils.create_dir_structure` / `file_utils.latest_modified_date`. Tests use `pytest` fixtures (`monkeypatch`, `tmp_path`) and run in <1 s.
- Added a GitHub Actions workflow (`.github/workflows/install.yml`) that runs on every push and PR to `main`. Installs the package in editable mode with the new `[dev]` extra, smoke-imports it, and runs the test suite against Python 3.10 and 3.12 on `windows-latest`.
- Added a `[project.optional-dependencies]` table to `pyproject.toml` with `dev = ["pytest>=8.0"]` so consumers can install testing tooling via `pip install -e ".[dev]"`.

### Packaging
- Bumped version to `1.0.1`

---

## 1.0.0 — 2026-05-22

### Renamed
- **Package renamed from `fc-utils` to `seller-automation-utils`** (and the Python import name from `fc_utils` to `seller_automation_utils`). The new name describes the package's purpose without any project-specific shorthand. Consumers must update their `requirements.txt` (`fc-utils @ git+...` → `seller-automation-utils @ git+...`) and any `from fc_utils.X import Y` statements to `from seller_automation_utils.X import Y`. No public API or behavior changes.

### Added
- `config/selectors.json.example` and `config/paths.json.example` — placeholder schemas for the SellerCloud DOM selectors and per-tenant URLs that `sellercloud.request_custom_export` / `download_report` read at runtime. Both real files remain gitignored.
- `.env.example` — placeholder for `ALERT_EMAIL`, `SELLERCLOUD_DELTA_URL`, and `SELLERCLOUD_ALPHA_URL`, matching the `get_env(...)` reads in `alert_utils` and `accounts`.

### Packaging
- Bumped version to `1.0.0`

---

## 0.7.3 — 2026-05-21

### Fixes
- `database_utils.insert_dataframe` now opts out of pandas 3.x's StringDtype default via `pd.set_option("future.infer_string", False)`. Without this, `df.iterrows()` rebuilds each row as a Series that converts `None` back to `NaN`, which pyodbc cannot bind to nullable SQL columns — causing every insert against a DataFrame with `None` in string columns to fail. Discovered during live integration testing.

### Packaging
- Bumped version to `0.7.3`

---

## 0.7.2 — 2026-05-20

### Behavior changes
- `excel_utils.refresh_workbook` default `macro_name` changed from `"Module1.Refresh"` to `"modUtilities.refresh"`. Reflects the standardized workbook convention adopted across consumers — one Standard Module per workbook named `modUtilities` with a synchronous `refresh()` sub. Callers that still drive a legacy `Module1.Refresh` macro must now pass it explicitly.

### Packaging
- Bumped version to `0.7.2`

---

## 0.7.1 — 2026-05-20

### Improvements
- `database_utils.insert_dataframe` now embeds the failing row's column → value mapping into its `RuntimeError` message. The traceback that `alert_utils.handle_crash` packages into the crash email body therefore carries full row context — previously only the row index was reported, and the row data lived only in the local rotating log file. Eliminates the back-and-forth of SSHing to the box after a per-row insert failure.

### Packaging
- Bumped version to `0.7.1`

---

## 0.7.0 — 2026-05-19

### New
- `greeting.greeting_for(hour=None)` — returns the time-of-day greeting (`"Good morning"`, `"Good afternoon"`, `"Good evening"`) for the supplied or current local hour. Centralizes the four sibling repos that hand-rolled the same `if 5 <= hour <= 11 …` block.

### Note
- Dead-code audit performed across all 13 sibling repos for `upsert_dataframe`, `find_file`, `download_finished`, `files_info`, `tomorrow`, `yesterday`, `load_env`, `setup_logger`, `send_error_email` — confirmed zero callers. The functions themselves had already been removed in `0.3.0`; this audit closes the loop.

### Packaging
- Bumped version to `0.7.0`
- Exported `greeting_for` from the package root: `from fc_utils import greeting_for`

---

## 0.6.1 — 2026-05-14

### Fixes
- `sellercloud.download_report` no longer hardcodes a `.xlsx` extension when looking for the downloaded file. It now derives the expected extension from `output_path.suffix`, so CSV, XLSX, and TSV exports all work. Falls back to `.xlsx` only if `output_path` has no suffix at all.

---

## 0.6.0 — 2026-05-14

### New module — `sellercloud`
- `request_custom_export(driver, custom_template, sku_list=None, product_group=None)` — drives the SellerCloud Manage Catalog grid through the Export Products wizard, picks a named Custom Export template, and returns the notify-download URL. Validates that exactly one of `sku_list` / `product_group` is supplied (raises `ValueError` otherwise) and enforces SellerCloud's 100-SKU-per-request cap.
- `download_report(driver, download_url, download_path, output_path, ...)` — polls the notify-download URL, clicks the download button once the report is ready, waits for the resulting `.xlsx` to land in `download_path`, and moves it to `output_path`.
- Both functions read DOM selectors from `config/selectors.json` and URLs from `config/paths.json` (resolved relative to the entry script, mirroring `accounts.py`). Consumers ship those JSON files in their own repo (gitignored); see the new `config/selectors.json.example` and `config/paths.json.example` for the required schema.
- Exported from the package root: `from fc_utils import request_custom_export, download_report`.

### Packaging
- Bumped version to `0.6.0`

---

## 0.5.0 — 2026-05-14

### Improvements
- Every fc_utils module that emitted progress output via `rich.print` now uses the standard `logging` API: each module declares `log = logging.getLogger(__name__)` at module scope, and all `print()` calls have been converted to the matching `log.info()` / `log.success()` / `log.warning()` / `log.error()` calls. This means:
  - Consumer scripts see a consistent stream of formatted log lines from fc_utils utilities (Chrome launches, scheduler ticks, Outlook polling, etc.) — same `[INFO]` / `[SUCCESS]` / `[WARNING]` / `[ERROR]` prefixes the formatter produces for application code.
  - Library code no longer assumes a writable stdout; when called from a context without a configured logger (tests, REPL), the output is silently dropped instead of attempting to render Rich markup.
  - The redundant inline `[cyan][INFO][/cyan]` / `[bold red][ERROR][/bold red]` markup was stripped from all 47 fc_utils log call sites (the formatter from v0.4.0 supplies the colored level tag).

### Removed
- `from rich import print` shadowing in every fc_utils module — the logger is now the only output sink.

### Packaging
- Bumped version to `0.5.0`

---

## 0.4.0 — 2026-05-13

### New
- `logging_utils.SUCCESS` — new log level (numeric value `25`, between `INFO` and `WARNING`) registered at import time; matching `Logger.success(msg, ...)` method bound to every `logging.Logger` instance. Lets automations write `log.success("done")` for milestone messages without hard-coding `[SUCCESS]` markup in every string.
- `logging_utils.RichLevelFormatter` — `logging.Formatter` subclass that prepends `[<color>][LEVELNAME][/<color>] ` to each message (cyan/INFO, green/SUCCESS, yellow/WARNING, red/ERROR, bold red/CRITICAL, dim/DEBUG).

### Improvements
- `logging_utils.setup_logging()` — `RichHandler` is now configured with `show_level=False` and `RichLevelFormatter` attached, so the console no longer prints its own level column on top of any inline `[LEVEL]` markup. Consumer scripts can drop the hard-coded `[cyan][INFO][/cyan]` / `[red][ERROR][/red]` prefixes from their messages; the level tag is now produced once by the formatter.

### Packaging
- Bumped version to `0.4.0`

### Migration notes for existing scripts
- Existing call sites that still embed `[cyan][INFO][/cyan]` etc. in the message will see a doubled prefix on the console (one from the formatter, one from the literal string). Recommended cleanup: remove the inline tag from every `log.info()/warning()/error()` call. The change is otherwise backward-compatible.

---

## 0.3.0 — 2026-05-12

### New
- `accounts.iter_amazon_accounts()` — yields `(account_key, display_name, url)` tuples; replaces the repeated `for account, url in AMAZON_URLS.items(): root = AMAZON_ACCOUNT_NAMES[account]` pattern across every Amazon script.
- `file_utils.latest_modified_date(path)` — returns the most-recent file modification datetime in a directory tree (or None).

### Removed (dead code, never imported externally)
- `logging_utils` module (`setup_logger()`) — suite standardized on `rich.print`
- `alert_utils.send_error_email()` — SMTP path was unused; `handle_crash()` covers alerting via Outlook
- `config_utils.load_env()` — every script uses `python-dotenv` directly
- `custom_functions.download_finished()` — superseded by `file_utils.wait_for_download()`
- `custom_functions.find_file()` — infinite-loop poller with no timeout; superseded by `file_utils.wait_for_download()`
- `custom_functions.files_info()` — every caller replaced with `file_utils.latest_modified_date()`
- `custom_functions.tomorrow()`, `custom_functions.yesterday()` — trivial one-liners that no caller used
- `database_utils.upsert_dataframe()` — added in 0.2.0 but never adopted

### Improvements
- `accounts.py` — `config/accounts.json` is now resolved relative to the entry script (`sys.argv[0]`), falling back to CWD. Robust against scripts launched from a different working directory (e.g. Task Scheduler).
- `custom_functions.shadow_element()` — fixed selector dispatch bug (`by_map[True]` collision); reduced to a single `find_element` block via explicit `if/elif` cascade. External API (css/Class/xpath/click flags) preserved.
- `accounts.amazon_login()` — now delegates OTP polling to `outlook.get_verification_code(..., consume=True)` (the manual inbox loop is gone). New `retry_url` parameter pulls the retry-on-missing-OTP loop out of every caller; up to 5 attempts (down from the original unbounded loop).
- `outlook.get_verification_code()` — new `consume: bool` parameter that marks the matched message as read and deletes it after extraction; default False preserves prior behavior.
- `schedule_utils.run_on_schedule()` — Ctrl+C now stops the scheduler immediately on Windows by installing a SIGINT handler that calls `scheduler.shutdown(wait=False)` (the prior C-level `Event.wait` blocked SIGINT until the next fire).

### Packaging
- Bumped version to `0.3.0`
- `__init__.py` `__all__` reduced to actually-used symbols
- Removed `python-dotenv` from `pyproject.toml` dependencies — no fc_utils module imports it after `load_env()` was deleted; consumer scripts continue to depend on it via their own `requirements.txt`

---

## 0.2.0 — 2026-05-07

### New modules
- `file_utils` — `create_dir_structure()`, `wait_for_download()`, `clear_directory()`
- `screenshot_utils` — `crop_to_element()`, `crop_to_box()`, `paste_to_excel()`

### New functions
- `config_utils.load_config_safe()` — returns `{}` on missing file instead of raising
- `database_utils.upsert_dataframe()` — UPDATE-first, INSERT-fallback for SQL Server
- `excel_utils.run_macro()` — run a synchronous macro without a wait period
- `excel_utils.paste_image_to_sheet()` — insert an image anchored at a cell
- `outlook.get_verification_code()` — generic OTP poller with custom body extractor
- `schedule_utils.run_on_schedule()` — APScheduler cron wrapper (replaces sleep loops)

### Improvements
- `accounts` — hardcoded account names, URLs, and eBay profiles moved to `config/accounts.json`
- `alert_utils` — SMTP sender address now loaded from `ALERT_EMAIL` env var
- `chrome` — `safe_start_browser()` merged into `start_browser()` with `retry_count` param
- `custom_functions.kill_app()` — fixed command injection risk (`os.system` → `subprocess.run`)
- `custom_functions.paste_image_from_clipboard()` — temp file now written to system temp dir
- `custom_functions.files_info()` — fixed double path-join bug and wrong `Date Created` field
- `database_utils` — replaced `quit()` with `raise RuntimeError` in `insert_dataframe()`
- All modules — full type hints and `Args`/`Returns`/`Raises` docstrings added throughout

### Removed
- `firefox.py` — Firefox support dropped (Chrome-only)
- `schedule_utils.seconds_until_target()`, `should_run()`, `sleep_until()` — replaced by APScheduler

### Packaging
- Bumped version to `0.2.0`
- Added `apscheduler>=3.10` dependency
- Added `LICENSE` (MIT)
- Updated `.gitignore` — covers `config/*.json`, `*.egg-info/`, `dist/`, `build/`

---

## 0.1.0

- Initial shared utilities package.
- Added reusable modules for accounts, browser helpers, custom functions, eBay, Chrome, and Outlook.
