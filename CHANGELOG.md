# Changelog

## 1.6.0 — 2026-08-11

### Added
- **`ebay_api` gains the OAuth half — per-listing view counts.** The Trading API
  has no view metric, so Views comes from the Sell Analytics API, which is REST on
  OAuth 2.0 and needs a consent grant per seller account. That credential is
  separate from the Trading token and the two are not interchangeable.
  - `oauth_refresh_env_var(account)` / `oauth_access_token(account, scope)` —
    refresh-token grant, cached per (account, scope) with 60s of expiry slack, so
    a 15-minute sweep mints one token rather than one per batch.
  - `get_listing_views(account, listing_ids, days=30)` — batches by 200 and
    returns every id passed in.
  - `parse_traffic_report(payload, metric)` — pure, so the response handling is
    testable without a network.

### Findings that shaped it (measured live 2026-08-11)
- **`getTrafficReport` is capped at 200 listing ids per call** — eBay rejects a
  longer list outright (`errorId 50028`), it does not trim. An unfiltered call
  returns at most 200 records, so batching by id is the only complete approach.
- **Listings with no traffic are omitted from the response, not returned as zero.**
  200 ids came back as 182 records. `get_listing_views` fills the gap, because
  "nobody looked" and "we failed to ask" must not become the same stored value.
- **Metric order in `metricValues` follows the request**, and the response echoes
  it in `header.metrics`. The metric is located by that header rather than
  positionally: reading `metricValues[0]` would have reported 92,089 impressions
  as views and looked entirely plausible in a report.
- **The daily quota is the real constraint.** `sell.analytics.traffic_report`
  allows **100 calls per 24h for the whole application**, shared across every
  automation on the keyset, while four seller accounts holding 23,822 active
  listings need 121 for a single pass. A 429 here is a daily budget, not a burst,
  so it raises a message saying so instead of looking like something a retry would
  fix. Increase requested from eBay (ticket 260811-000048).

### Tests
- New `tests/test_ebay_api_traffic.py` (17 cases): env-var naming and its
  distinctness from the Trading token variable, credential errors, metric
  extraction by header in both orders, absent/null metrics, zero-filling,
  batching at the 200 boundary, no-op on an empty id list, the daily-quota
  message, and token minting/reuse. Full suite: **198 passing** (was 181).

---

## 1.5.0 — 2026-08-10

Held back from 1.4.2 so that release could ship on its own, then versioned
immediately once it had. Leaving it unversioned any longer was a mistake worth
recording: for a short window on 2026-08-10 two different code states both called
themselves `1.4.2` — `ebay-returns` had `ebay_api`, `ebay-items-categories` did
not — which makes a fleet version audit lie, and stops `pip install -U` from
reaching any repo already on 1.4.2.

### Added
- **`ebay_api` — a Trading API client, the way off the Seller Hub scrape.** eBay's
  Customize-table Save has been rejecting every request since 2026-08-06 (still
  failing on 08-10), and no client-side fix reaches it. This module reads the same
  listing data server-side: no browser, so no bot check, no React grid and no
  Customize dialog. Credentials reuse what `ebay-best-offers` already has in
  production — one app keyset for all accounts plus per-account user tokens named
  by `token_env_var`.
  - `get_active_listings(token)` sweeps `GetSellerList` and returns every active
    listing with the eight fields the Items-Categories report needs: item number,
    title, SKU, current price, sold quantity, watchers, start time and category.
  - `count_active_listings(token)` is a one-call second opinion on how many active
    listings an account has, so a sweep can prove it missed nothing.
  - `l1_category`, `to_seller_local`, `account_token`, `token_env_var` are the pure
    helpers callers need; build and parse are kept apart from HTTP so both are
    unit-testable without a network.

### Findings that shaped it (measured live against AccountA, 2026-08-10)
- **`GetMyeBaySelling` cannot feed this report.** Its ActiveList items carry no
  `PrimaryCategory` and no `QuantitySold`. `GetSellerList` carries both.
- **`GetSellerList` returns ended listings too, and page 1 is the worst case.**
  Results are ordered by end time ascending, so page 1 was 22–25 listings that had
  ended earlier the same day, out of 200. Pages 31 and 62 were 100% active.
  `get_active_listings` filters on `ListingStatus == 'Active'`; extrapolating page
  1's ratio instead would have suggested ~1,300 phantom missing listings.
  Arithmetic that confirms the filter: 12,278 entries − 25 ended = 12,253, which is
  exactly what `GetMyeBaySelling` independently reported.
- **No Taxonomy API is needed.** `PrimaryCategory/CategoryName` is the full path
  (`Cameras & Photo:Video Production & Editing:Video Monitors`), so the report's
  top-level bucket is `split(":")[0]`. The `/` → `-` substitution reproduces the
  scraper's own output for `Computers/Tablets & Networking`.
- **The end-time window is not a constraint.** Every listing is GTC
  `FixedPriceItem` ending within ~31 days; widening the window from 90 to 120 days
  returned zero additional listings.
- **Timestamps shift.** Seller Hub rendered Pacific time and the API returns UTC,
  so `to_seller_local` converts before the value reaches SQL. Adopting the raw UTC
  value would move every StartDate by 7–8 hours.

### Tests
- New `tests/test_ebay_api.py` (42 cases): token-name normalization, credential
  errors that never echo a token, category rollup including the `/` case, both
  Pacific offsets and naive output, request building (paging, window, escaping,
  page-size clamp), response parsing (full mapping, absent numerics, absent SKU as
  `None` rather than `""`, unparseable and millisecond-less timestamps, missing
  category, failure acks, missing pagination), and the sweep itself (ended-listing
  filter, multi-page walk, injected window, failure ack, runaway-page guard,
  `Warning` treated as success). Full suite: **181 passing** (was 139).

---

## 1.4.2 — 2026-08-06

### Fixed
- **`ebay.customize_offers_table` selected the wrong columns, silently.** After 1.4.1 stopped the crash, the 12:07 run scraped a table containing only the default columns and died on the extraction gate with `empty ['StartDate']`. Root cause: the function *toggled* rather than *set*. `if not views:` and `if not watchers:` only make sense if Restore Defaults leaves those columns ON — eBay's redesigned dialog defaults to **Custom label (SKU) and Current price alone**, confirmed by both the crash DOM and the debug screenshot, so `views=True` meant "never click it" and the column simply never appeared. Views, Watchers and Sold were all silently empty; only StartDate was gated, which is the sole reason this surfaced at all.
  - Columns are now driven to an **absolute state** via `_set_column(driver, id, desired)`, which reads `checked` first and clicks only when it differs. No assumption about eBay's defaults survives anywhere in the function.
  - Each click is **verified**, with fallbacks: native click → `label[for=...]` click → JS click plus a bubbling `change` event. eBay wraps each input in `span.checkbox` with a sibling label, so a native click can land without toggling. A column that refuses all three raises rather than saving a wrong table.
  - After saving, the applied column set is logged, so a bad selection is visible at the point it happens instead of surfacing later as blank data.
  - Columns no report reads (`promoteListing`, `unansweredQuestionCount`, `bidCount`, `promotions`, `itemSpecifics`) are now explicitly set OFF instead of blind-toggled.
  - **The clicks were not reaching eBay's React state.** Instrumentation on the 14:40 run showed all five requested boxes still checked at the moment Save was clicked, and the saved table still came back as `lineActions, title, listingSKU, price, timeRemaining` — exactly the Restore-Defaults set. Comparing the dialog's two controls in the crash DOM shows why: the checkboxes read `listingSKU, price` while the dialog's own "Arrange the order of the columns" list still held the *previously saved* set. The two are out of sync, and what got saved was neither — it was the state React held. Clicking the `<input>` sets its `checked` property without React's handler firing, so the box looks right and Save serializes something else. **The label is now the primary click target** (the browser dispatches the click on the input itself, which React does handle), with the input and JS as fallbacks. The dialog's column list is logged before Save, and a mismatch against it is warned about, since that list — not `checked` — is the honest preview of what Save will apply.
  - **The remaining blocker is eBay's, not ours.** With React state finally correct at save time, the save is still rejected: the dialog stays open and shows *"We ran into a problem and couldn't complete your action. Please try again."* Reproduced with **no changes made at all** (open the dialog, click Save), in a **visible** browser as well as headless, and on a **second account** — so it is not our clicking, not the column choice, and not bot detection. Both accounts' stored views still list the retired `listingId`, and Restore Defaults re-adds the retired `format`, so the payload eBay sends itself contains columns its own backend no longer accepts, and no UI path exists to remove them. `customize_offers_table` now reads the dialog's alert strip after saving and raises with eBay's message verbatim, so this fails in one obvious line instead of masquerading as a selector bug.
  - **Not yet confirmed against eBay** (superseded by the finding above): The 14:09 run proved every checkbox verified as flipped — no fallback warnings, no unreadable-state warnings, no refusal — and the saved table *still* came back as `lineActions, title, listingSKU, price, timeRemaining`. So the clicks land and Save is accepted (the dialog unmounts), yet the selection does not survive. Two candidates remain: eBay discards the selection server-side, or a React re-render clears the boxes between the per-click check and the Save click. Added for the next run: the checkbox states are re-read and logged **at save time**, which separates those two, and a post-save check raises at the cause when a requested column never appears instead of letting the caller scrape a table that is missing it.
- **`accounts.ebay` could not sign in at all.** It waited for `#pass` to be *present* and then typed into it. eBay now serves a **two-step** form — page 1 asks for the username, and `#pass` exists in the DOM but stays hidden until page 2 — so `presence_of_element_located` resolved instantly and `send_keys` raised `ElementNotInteractableException`. The wait is now `element_to_be_clickable`, and the username step is submitted first when it is on screen.
  - New optional `username` argument, falling back to the `eBay_user` environment variable. When eBay asks for a username and none is configured, it raises a `RuntimeError` saying so instead of dying on an interaction error.
  - Submits with Enter rather than a Continue button: eBay renames that button between flows, while the form has always submitted on Enter.
  - A captcha splash (`/splashui/captcha`) is now detected and raised as a clear `RuntimeError` both before and during sign-in, instead of surfacing as a mystery timeout.
  - **Fleet-wide severity.** This helper is the only re-authentication path for all six eBay automations. The breakage was invisible only because the Chrome profiles stayed signed in; any lapsed session would have left that automation stranded on a sign-in page with no way back.

### Known limitation
- **The step-1 username locators are unverified.** Every automated hit on eBay's sign-in page during development landed on a captcha, so the two-step form could not be captured live. `_USERNAME_LOCATORS` is a best-known list (`#userid`, `input[name=userid]`, `input[autocomplete=username]`) and the code falls through to an explicit error rather than guessing further. **Needs one live confirmation against the real form.** The password-only path, the captcha path, and the error paths are all fully covered.

### Tests
- New `tests/test_ebay_login.py` (8 cases): password-only form, two-step form, env-var username fallback, missing username, hidden username input ignored, captcha before and during sign-in, and password field never usable. Full suite: 139 passing.

---

## 1.4.1 — 2026-08-06

### Fixed
- **`ebay.customize_offers_table` no longer dies when eBay retires a column.** eBay removed the **Item number** (`customize-listingId`) and **Format** (`customize-format`) checkboxes from the "Customize active view" dialog, and both were clicked unconditionally — so `NoSuchElementException` killed the `ebay-items-categories` run at 2026-08-06 00:00 and again on the 00:05 manual retry. Confirmed against the crash DOM: the dialog was fully rendered (all four fieldsets plus Save/Restore/Cancel) and those two ids were simply absent, while every other id the function clicks was still present. This is the binary wrong-selector failure, not a render race — a longer wait would only have failed slower.
  - Checkbox clicks now go through `_toggle_column`, which skips ids in the new `OPTIONAL_COLUMNS` set (`itemSpecifics`, `listingId`, `format`, `promotions`) with a warning naming the id, and re-raises for anything else.
  - **Neither retired column carried data.** Both consumers read the item number off the row (`tr.grid-row[data-id]` → `r.dataset.id`), and neither extracts Format at all, so nothing is lost. Every column a report *does* read stays required, so a real DOM change still fails loudly instead of inserting blank rows.
  - `itemSpecifics` and `promotions` had the same tolerance already, expressed as two ad-hoc `try`/`except NoSuchElementException` blocks; they now share the one mechanism.

### Tests
- 12 new cases in `tests/test_ebay_customize.py`: each optional column missing individually, all four missing at once, six required columns each raising, and the `views=False` de-select path (which clicks to turn a column *off* and is therefore still required). Full suite: 122 passing.

### Upgrade notes
- No caller changes. `ebay-items-categories` and `ebay-best-offers` are the only consumers of `customize_offers_table` and both need `pip install -U seller-automation-utils` before their next run.

---

## 1.4.0 — 2026-07-31

### Added
- **`fleet_state.py` — durable on-disk heartbeat and crash archive.** Until now nothing outside an automation's own process could tell whether it was healthy. The only failure signal was the `[CRASH]` email from `handle_crash`, which requires the automation to catch its own exception *and* still be able to drive Outlook COM. A killed process, a logged-off Windows session, a scheduler thread that quietly stopped firing, or a broken Outlook all failed **silently**. Two automations were found down for weeks with no signal at all.
  - `HeartbeatWriter` writes `%LOCALAPPDATA%\fc-fleet\heartbeats\<name>.json` every 15s (atomic temp-file + `os.replace`, with a short retry because Windows fails the replace if a reader or AV has the target open).
  - Each beat carries every job's **live** `next_run_time`, re-read from `scheduler.get_jobs()` and never cached. If the scheduler's background thread dies inside a still-running process, the jobs stay listed but their next-run times stop advancing — the only externally visible symptom of that failure, and the reason caching would defeat the purpose.
  - `automation_name()` derives the key from `sys.argv[0]` (`run_<module>.py` → `<module>`), so **no repo needed a source edit**. It is deliberately not derived from the display name passed to `handle_crash`: those are human labels (`"Amazon CA FBA Inventory"` for `amzn_ca_fba_inventory`) and are sometimes built at runtime (`"eBay Best Offers (failed on X)"`).
  - Nothing in the module raises. A monitoring side-channel that can kill the automation it monitors is worse than no monitoring, so every function swallows its own errors and reports success as a bool.

### Changed
- **`schedule_utils.run_on_schedule` emits the heartbeat**, records each job outcome from its existing `EVENT_JOB_EXECUTED | EVENT_JOB_ERROR` listener, and clears the heartbeat file on clean shutdown — so a deliberate Ctrl+C is distinguishable from a kill, which leaves the file behind to age into staleness.
- **`alert_utils.handle_crash` archives the crash before attempting the email.** Everything after that point depends on Outlook COM, and a broken Outlook must not also erase the evidence of the crash. The screenshot and DOM capture are now **moved into the archive rather than deleted**, so they stop existing only as mail attachments; the email attaches them from their new location. A failing `send_email` is caught and logged instead of aborting the handler, so the Excel/Chrome/ChromeDriver cleanup still runs. `emailed` is stamped on the record, which distinguishes "crashed and you were told" from "crashed and the alert itself failed".
- **`ui_utils.ask_user` honors `FC_NO_PROMPT`.** Every entry point in the fleet blocks on this dialog before reaching its scheduler; an automation started unattended would otherwise hang forever on a message box nobody is looking at while appearing to run. Returns False when set, which for the fleet's `if ask_user(...): main()` shape means "skip the immediate run, go straight to the scheduler".

### Fixed
- **`apscheduler` capped to `>=3.10,<4`.** The previous `>=3.10` permitted APScheduler 4, which drops the 3.x scheduler API this package is built on (`get_jobs()`/`next_run_time`, the `add_listener` event constants). A routine `pip install -U` would have broken all 18 automations at once.

### Tests
- Added `tests/test_fleet_state.py` (24 cases) and `tests/test_ui_utils.py` (5 cases). Covers name derivation across the fleet's path shapes, heartbeat round-trip, corrupt/missing files, rate limiting, crash archive artifact moves, `emailed` stamping, retention pruning, and multi-job snapshots (`inventory-feed-report` runs two jobs in one process). Includes a regression test for a bug found during development: `record_result` forced a write with an empty job list *and* reset the rate limiter, blanking next-run times for a full interval — precisely the signature a monitor reads as a dead scheduler.
- Full suite: 110 passing.

### Upgrade notes
- Purely additive; no caller changes required. Repos pick it up with `pip install -U seller-automation-utils`.
- `inventory-feed-report` shadows the shared helper with its own two-job `run_on_schedule`, so it was wired up by hand in that repo.
- **Verified end-to-end**, not just unit-tested: a real scheduler process writes a correct heartbeat, and killing it leaves the file behind as intended.

---

## 1.3.1 — 2026-07-30

### Fixed
- **`_input_sizes` no longer breaks callers that pass real date objects.** 1.3.0 pinned every temporal column to `WVARCHAR(40)` purely from the table schema, on the assumption that callers stringify dates. Some do — `amzn-ca-fba-inventory` deliberately sends `date.isoformat()` because the legacy driver could not bind `datetime.date` — but others do not: `sellercloud-sync` builds `LastReceived` with `pd.to_datetime(...)` and hands over `pandas.Timestamp` objects. Pinning those WVARCHAR made the insert fail, a regression against the old per-row path where pyodbc bound them natively. The decision is now made from the data: a temporal column is pinned WVARCHAR only when its first non-null value is a `str`, and is otherwise left to pyodbc. Leading nulls are skipped when sniffing; an all-null column, or a call with no DataFrame, is left native.
- Caught by the per-repo verification gate before any scheduled run hit it. `sellercloud-sync` would have failed on its next run under 1.3.0.

### Tests
- Extended `tests/test_database_input_sizes.py` to 35 cases: strings still pinned, `datetime.date` / `datetime.datetime` / `pandas.Timestamp` left native, leading nulls skipped, all-null left native, no-DataFrame left native, and string/decimal pinning proven independent of the data.

### Packaging
- Bumped version to `1.3.1`. **Anyone on 1.3.0 should move to 1.3.1** — 1.3.0 is only safe for callers that stringify every date.

---

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
