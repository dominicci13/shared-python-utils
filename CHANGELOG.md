# Changelog

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
