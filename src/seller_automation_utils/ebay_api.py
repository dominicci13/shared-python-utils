"""eBay Trading API client for seller listing data.

Server-side replacement for the Seller Hub scrape: there is no browser, so eBay's
bot check, its React grid and its Customize-table dialog are all out of the
picture. Built after that dialog's Save broke on 2026-08-06 and stayed broken.

Credentials come from the environment — one app keyset covering every account
(``EBAY_APP_ID`` / ``EBAY_DEV_ID`` / ``EBAY_CERT_ID``) plus a per-account user
token named by :func:`token_env_var`.

Build and parse are pure functions kept apart from the HTTP call, so both the
request shape and the response handling are unit-testable without a network.
"""
from __future__ import annotations

import base64
import html
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)

TRADING_ENDPOINT = "https://api.ebay.com/ws/api.dll"
TRADING_COMPAT_LEVEL = "1193"

# eBay caps a Trading page at 200 and silently clamps anything larger.
MAX_ENTRIES_PER_PAGE = 200

# Seller Hub rendered every timestamp in eBay's own US Pacific clock, and the
# report's StartDate column has always meant that. The API returns UTC, so the
# conversion happens here rather than in each caller.
SELLER_TIMEZONE = "America/Los_Angeles"

# GetSellerList selects by end time, not by status. Every listing in this fleet
# is GTC FixedPriceItem and so ends within ~31 days; a 90-day window was measured
# on 2026-08-10 to return the whole active inventory, and widening it to 120 days
# returned zero extra listings. The one-day look-back covers a listing caught
# mid-relist with a momentarily stale end time — those come back as Completed and
# are dropped by the status filter anyway.
WINDOW_FORWARD_DAYS = 90
WINDOW_LOOKBACK_DAYS = 1

# A runaway page loop would burn the app's daily call budget for every other eBay
# automation sharing this keyset. No account is near this.
_MAX_PAGES = 400

# eBay returns these as Ack=Failure over HTTP 200, and its own LongMessage says
# "Please try again later". ebay-items-categories met 10007 three times
# (GetMyeBaySelling 2026-09-05, GetSellerList 2026-09-09 and 2026-09-21) and each
# cleared on a rerun minutes later. Every other code is treated as permanent: a
# bad token, a malformed request or a spent call limit fails identically on
# every retry, so retrying only burns quota.
TRANSIENT_TRADING_ERROR_CODES = frozenset({"10007"})

# One pause per retry, so len + 1 attempts per call (per page for the sweep).
# Short on purpose: long enough to ride out an eBay-side blip, short enough that
# a persistent fault still fails the run within seconds instead of minutes.
TRADING_RETRY_DELAYS: tuple[float, ...] = (5.0, 15.0)


OAUTH_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
TRAFFIC_REPORT_URL = "https://api.ebay.com/sell/analytics/v1/traffic_report"
ANALYTICS_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly"

FIND_ELIGIBLE_ITEMS_URL = "https://api.ebay.com/sell/negotiation/v1/find_eligible_items"
# findEligibleItems is authorized by the inventory read scope, not by
# `sell.negotiation` — that scope is not grantable to this keyset at all, and
# eBay confirmed the inventory scopes on ticket 260817-000055 (2026-08-21).
# Verified live 2026-08-25: the same refresh token returns 200 under this
# scope and 403 under sell.analytics.readonly.
INVENTORY_READONLY_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.inventory.readonly"

# eBay rejects a longer list outright (errorId 50028), it does not silently trim.
MAX_LISTING_IDS_PER_CALL = 200

# find_eligible_items defaults to 10 a page and caps at 200.
MAX_ELIGIBLE_ITEMS_PER_PAGE = 200

VIEWS_METRIC = "LISTING_VIEWS_TOTAL"

# Access tokens last two hours and a sweep takes ~15 minutes per account, so one
# mint per process is plenty. Keyed by (account, scope); 60s of slack absorbs a
# token that would otherwise expire mid-call.
_access_tokens: dict[tuple[str, str], tuple[str, datetime]] = {}


def token_env_var(account: str) -> str:
    """Environment variable holding a seller account's Trading API user token.

    Non-alphanumerics are stripped and the rest upper-cased, matching the
    convention already used by ``ebay-best-offers`` so one ``.env`` serves both.

    Args:
        account: eBay account display name, e.g. ``"AccountB"``.

    Returns:
        The variable name, e.g. ``"EBAY_AUTH_TOKEN_ACCOUNTB"``.
    """
    return "EBAY_AUTH_TOKEN_" + re.sub(r"[^A-Za-z0-9]", "", account).upper()


def account_token(account: str) -> str:
    """Read a seller account's Trading API user token from the environment.

    Args:
        account: eBay account display name.

    Returns:
        The token.

    Raises:
        RuntimeError: The variable is missing or empty. The message names the
            variable so the fix is obvious without exposing any token value.
    """
    name = token_env_var(account)
    token = os.getenv(name, "").strip()
    if not token:
        raise RuntimeError(f"No eBay Trading API token for {account!r} — set {name} in .env.")
    return token


def oauth_refresh_env_var(account: str) -> str:
    """Environment variable holding a seller account's OAuth refresh token.

    Separate from :func:`token_env_var`: the Trading API uses a legacy Auth'n'Auth
    token, while the REST APIs need an OAuth refresh token consented per account.
    The two are not interchangeable and both are needed.

    Args:
        account: eBay account display name.

    Returns:
        The variable name, e.g. ``"EBAY_OAUTH_REFRESH_TOKEN_ACCOUNTA"``.
    """
    return "EBAY_OAUTH_REFRESH_TOKEN_" + re.sub(r"[^A-Za-z0-9]", "", account).upper()


def oauth_access_token(account: str, scope: str = ANALYTICS_SCOPE) -> str:
    """Mint (or reuse) a two-hour OAuth access token for one account.

    Args:
        account: eBay account display name.
        scope: Space-delimited scopes to request. Must be a subset of what the
            account consented to, or eBay rejects the grant.

    Returns:
        The access token.

    Raises:
        RuntimeError: No refresh token is configured, or eBay refused the grant.
    """
    cached = _access_tokens.get((account, scope))
    if cached and cached[1] > datetime.now(timezone.utc):
        return cached[0]

    name = oauth_refresh_env_var(account)
    refresh = os.getenv(name, "").strip()
    if not refresh:
        raise RuntimeError(
            f"No eBay OAuth refresh token for {account!r} — set {name} in .env. "
            "It is granted per account through eBay's consent flow; the Trading "
            "API token does not cover the REST APIs."
        )

    basic = base64.b64encode(
        f"{os.environ['EBAY_APP_ID']}:{os.environ['EBAY_CERT_ID']}".encode()
    ).decode()
    response = requests.post(
        OAUTH_TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Authorization": f"Basic {basic}"},
        data={"grant_type": "refresh_token", "refresh_token": refresh, "scope": scope},
        timeout=60,
    )
    if response.status_code != 200:
        raise RuntimeError(f"eBay refused the refresh grant for {account}: "
                           f"{response.status_code} {response.text[:300]}")

    payload = response.json()
    token = payload["access_token"]
    expires = datetime.now(timezone.utc) + timedelta(seconds=int(payload["expires_in"]) - 60)
    _access_tokens[(account, scope)] = (token, expires)
    return token


def parse_traffic_report(payload: dict, metric: str = VIEWS_METRIC) -> dict[str, int]:
    """Pull one metric out of a getTrafficReport response, keyed by listing id.

    Pure (no HTTP). The metric is located by its position in ``header.metrics``
    rather than assumed — the order follows the request, so reading
    ``metricValues[0]`` blindly would silently swap impressions for views.

    Listings with no traffic are **absent from the response**, not returned as
    zero, so the caller must supply the zero rather than expect a key.

    Args:
        payload: Decoded getTrafficReport JSON.
        metric: Metric key to extract.

    Returns:
        ``{listing_id: value}`` for every listing the response carried.

    Raises:
        RuntimeError: The requested metric is not present in the response header.
    """
    metrics = [m.get("key") for m in payload.get("header", {}).get("metrics", [])]
    if metric not in metrics:
        raise RuntimeError(f"getTrafficReport returned metrics {metrics}, expected {metric}.")
    index = metrics.index(metric)

    views: dict[str, int] = {}
    for record in payload.get("records", []):
        dimensions = record.get("dimensionValues", [])
        values = record.get("metricValues", [])
        if not dimensions or len(values) <= index:
            continue
        listing_id = dimensions[0].get("value")
        value = values[index].get("value")
        if listing_id is not None:
            views[str(listing_id)] = int(value or 0)
    return views


def get_listing_views(account: str, listing_ids: list[str], days: int = 30) -> dict[str, int]:
    """Fetch per-listing view counts for one account over a trailing window.

    Batched because eBay caps a request at :data:`MAX_LISTING_IDS_PER_CALL` ids
    and rejects anything longer outright.

    Ids that eBay omits from the report are filled with 0, because eBay drops
    zero-traffic listings rather than returning them as 0. The fill promises
    only that every requested id has a key — **not** that the 0 was measured:
    an id that does not exist, or that belongs to another seller account, fills
    identically. Two guards keep that from passing as data: ids are validated as
    digit strings before any request, and a batch that parses to no records at
    all is logged as a warning.

    Args:
        account: eBay account display name.
        listing_ids: Item numbers to look up, as strings or ints. Each must be
            all digits once surrounding whitespace is stripped. Duplicates are
            requested once, in first-seen order.
        days: Length of the trailing window, ending yesterday. Must be >= 1.

    Returns:
        ``{listing_id: views}`` with one entry per *unique* id passed in, 0 where
        eBay returned no record for it. **Keys are the stripped string form**, so
        an id passed as ``123`` or ``" 123 "`` comes back under ``"123"`` —
        map results back by that form, not by the value you passed in.

    Raises:
        ValueError: An id is not all digits once stripped, or `days` is below 1.
            Raised before the first request, so a bad input costs no quota.
        RuntimeError: eBay refused the grant or returned an error response.
    """
    if days < 1:
        raise ValueError(f"get_listing_views needs days >= 1, got {days!r}.")
    if not listing_ids:
        return {}

    malformed: list[str] = []
    unique: list[str] = []
    seen: set[str] = set()
    for listing_id in listing_ids:
        candidate = str(listing_id).strip()
        # Digits-only, and `[0-9]` not `\d` — `\d` accepts Arabic-Indic and
        # fullwidth digits, and `|` / `}` would close the filter string early.
        if not re.fullmatch(r"[0-9]+", candidate):
            malformed.append(candidate)
        elif candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    if malformed:
        raise ValueError(
            f"get_listing_views got {len(malformed):,} listing id(s) for {account} that are "
            f"not digit strings: {malformed[:5]}. eBay matches nothing against them and the "
            "zero-fill would hide that, so the run stops before spending any quota. Ids "
            "shaped like '123456789012.0' usually come through a float64 column."
        )
    duplicates = len(listing_ids) - len(unique)
    if duplicates:
        log.debug(f"Dropped {duplicates:,} duplicate listing id(s) for {account}; "
                  f"requesting {len(unique):,} unique ids.")
    listing_ids = unique

    token = oauth_access_token(account)
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    window = f"{start:%Y%m%d}..{end:%Y%m%d}"

    views: dict[str, int] = {}
    returned = 0
    batches = range(0, len(listing_ids), MAX_LISTING_IDS_PER_CALL)
    for number, offset in enumerate(batches, start=1):
        batch = listing_ids[offset:offset + MAX_LISTING_IDS_PER_CALL]
        response = requests.get(
            TRAFFIC_REPORT_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={
                "dimension": "LISTING",
                "filter": f"marketplace_ids:{{EBAY_US}},date_range:[{window}],"
                          f"listing_ids:{{{'|'.join(batch)}}}",
                "metric": VIEWS_METRIC,
            },
            timeout=120,
        )
        # 429 here is a *daily* quota, not a burst — retrying or backing off does
        # not help, so say what actually happened. The
        # sell.analytics.traffic_report limit is 500 calls per 24h for the whole
        # application (raised from 100 via eBay's Application Growth Check,
        # 2026-09-09), and every caller on the keyset draws from the same pool.
        if response.status_code == 429:
            raise RuntimeError(
                f"getTrafficReport hit eBay's daily call limit for {account} on batch "
                f"{number} of {len(range(0, len(listing_ids), MAX_LISTING_IDS_PER_CALL))}. "
                "The sell.analytics.traffic_report quota is per application per day and is "
                "shared across every automation on this keyset. It resets on eBay's Pacific "
                "midnight, which is 07:00 or 08:00 UTC depending on US DST. "
                "Check the remaining budget with the Developer Analytics rate_limit resource."
            )
        if response.status_code != 200:
            raise RuntimeError(f"getTrafficReport failed for {account} "
                               f"(batch {number}): {response.status_code} {response.text[:300]}")
        parsed = parse_traffic_report(response.json())
        if not parsed:
            # Zero records is legitimate for an account with no traffic in the
            # window, but it looks identical to having asked wrong: an id that
            # passed through a float64 pandas column ("123456789012.0"), or one
            # from another seller account, matches nothing at eBay. The
            # zero-fill cannot tell those apart, so it says so out loud.
            log.warning(
                f"getTrafficReport returned no records for {account} batch {number} "
                f"({len(batch):,} ids). All {len(batch):,} will store as 0 views. That is "
                "correct only if none of them had any traffic in the window."
            )
        views.update(parsed)
        returned += len(parsed)
        log.info(f"eBay returned views for {returned:,}/{len(listing_ids):,} listings "
                 f"after batch {number}.")

    return {listing_id: views.get(listing_id, 0) for listing_id in listing_ids}


def parse_eligible_items(payload: dict) -> list[str]:
    """Pull the listing ids out of a find_eligible_items response.

    Pure (no HTTP). eBay returns one object per eligible listing; only the id is
    of interest, since every other field for that listing already comes from the
    Trading sweep.

    Args:
        payload: Decoded find_eligible_items JSON.

    Returns:
        Listing ids in the order eBay returned them, ids absent skipped.
    """
    return [str(item["listingId"]) for item in payload.get("eligibleItems", [])
            if item.get("listingId") is not None]


def get_offer_eligible_items(account: str, marketplace: str = "EBAY_US") -> set[str]:
    """Fetch the listings eBay will let this account send offers on.

    This is the API replacement for Seller Hub's ``offers=sendNewOffers`` filter.
    Eligibility is eBay's own judgement — it is not derivable from listing data,
    and watcher count is not a usable proxy for it (measured 2026-08-13: watchers
    select ~16x too many listings and still miss eligible ones).

    Needs ``sell.inventory.readonly``. That is not a typo for a Negotiation
    scope: ``sell.negotiation`` cannot be granted to this keyset and does not
    authorize this call. The scope must be in the account's consent alongside
    every other scope it uses, because a refresh token carries only what it was
    consented for — re-consenting for one scope alone silently drops the rest.

    Args:
        account: eBay account display name.
        marketplace: eBay marketplace id the listings belong to.

    Returns:
        Eligible listing ids. Empty if the account currently has none.

    Raises:
        RuntimeError: eBay refused the grant, withheld the scope, or returned an
            error response.
    """
    token = oauth_access_token(account, INVENTORY_READONLY_SCOPE)

    eligible: set[str] = set()
    offset = 0
    page = 1
    while True:
        if page > _MAX_PAGES:
            raise RuntimeError(f"find_eligible_items exceeded {_MAX_PAGES} pages for {account} "
                               "— refusing to keep paging.")

        response = requests.get(
            FIND_ELIGIBLE_ITEMS_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                     "X-EBAY-C-MARKETPLACE-ID": marketplace},
            params={"limit": MAX_ELIGIBLE_ITEMS_PER_PAGE, "offset": offset},
            timeout=120,
        )
        # The scope rides on the token, so a 403 here means this account's consent
        # lacks it rather than anything about this run.
        if response.status_code == 403:
            raise RuntimeError(
                f"find_eligible_items was refused for {account}: the keyset does not carry "
                f"{INVENTORY_READONLY_SCOPE}. Re-consent the account for that scope "
                "alongside the ones it already uses."
            )
        if response.status_code != 200:
            raise RuntimeError(f"find_eligible_items failed for {account} "
                               f"(page {page}): {response.status_code} {response.text[:300]}")

        payload = response.json()
        listing_ids = parse_eligible_items(payload)
        eligible.update(listing_ids)

        # Advance by what actually came back, not by the requested page size, so a
        # short page cannot skip listings.
        offset += len(listing_ids)
        total = payload.get("total")
        if total is None:
            # eBay has always sent it. Without it, keep going while pages come back
            # full rather than treating the first page as the whole account.
            done = len(listing_ids) < MAX_ELIGIBLE_ITEMS_PER_PAGE
        else:
            done = offset >= int(total)

        if not listing_ids or done:
            break
        page += 1

    log.info(f"{account}: {len(eligible):,} listing(s) eligible for seller-initiated offers.")
    return eligible


def l1_category(category_path: str | None) -> str:
    """Roll an eBay category path up to its top-level name.

    ``PrimaryCategory/CategoryName`` comes back as a full path, e.g.
    ``"Cameras & Photo:Video Production & Editing:Video Monitors"``. The report
    groups by the top level only. The ``/`` → ``-`` substitution reproduces what
    the scraper wrote for names like ``Computers/Tablets & Networking``.

    Args:
        category_path: Full colon-delimited path, or None.

    Returns:
        The normalized top-level name, or ``""`` when the path is missing.
    """
    if not category_path:
        return ""
    return category_path.split(":")[0].strip().replace("/", "-")


def to_seller_local(moment: datetime | None, tz: str = SELLER_TIMEZONE) -> datetime | None:
    """Convert an aware UTC timestamp to a naive timestamp in eBay's seller clock.

    Naive on purpose: the value lands in a SQL ``DATETIME2`` column that has
    always held wall-clock Pacific time, and a tz-aware value would shift it.

    Args:
        moment: Timezone-aware datetime, or None.
        tz: IANA timezone name.

    Returns:
        The naive local datetime, or None when given None.
    """
    if moment is None:
        return None
    return moment.astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def _text(item: ET.Element, path: str) -> str | None:
    """Read a descendant's text, or None when the element is absent."""
    value = item.findtext(path)
    return value if value not in (None, "") else None


def _as_float(value: str | None) -> float:
    """Coerce eBay's numeric text to float, treating junk and absence as 0.0."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: str | None) -> int:
    """Coerce eBay's numeric text to int, treating junk and absence as 0."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_utc(value: str | None) -> datetime | None:
    """Parse eBay's ``2025-06-10T03:07:39.000Z`` into an aware UTC datetime."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _error_fields(root: ET.Element) -> tuple[list[str], list[str]]:
    """Read a Trading response's ``Errors`` elements for display and for retry.

    Pure. Tags must already be stripped of their namespace.

    Args:
        root: The parsed response root.

    Returns:
        ``(errors, error_codes)``. ``errors`` carries every ``Errors`` element
        as ``"code: message"``; ``error_codes`` carries only the codes whose
        ``SeverityCode`` is not ``Warning`` (absent counts as an error), since a
        warning riding along on a failure is not what failed it.
    """
    elements = root.findall(".//Errors")
    errors = [f"{e.findtext('ErrorCode')}: {e.findtext('LongMessage')}" for e in elements]
    error_codes = [
        (e.findtext("ErrorCode") or "").strip()
        for e in elements
        if (e.findtext("SeverityCode") or "").strip() != "Warning"
    ]
    return errors, error_codes


def _iso_z(moment: datetime) -> str:
    """Format an aware datetime the way the Trading API expects it."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def trading_headers(call_name: str) -> dict[str, str]:
    """Build the Trading API HTTP headers for one call.

    Args:
        call_name: Trading call, e.g. ``"GetSellerList"``.

    Returns:
        The header mapping, including the app keyset read from the environment.

    Raises:
        KeyError: A keyset variable is missing.
    """
    return {
        "X-EBAY-API-CALL-NAME": call_name,
        "X-EBAY-API-SITEID": "0",
        "X-EBAY-API-COMPATIBILITY-LEVEL": TRADING_COMPAT_LEVEL,
        "X-EBAY-API-APP-NAME": os.environ["EBAY_APP_ID"],
        "X-EBAY-API-DEV-NAME": os.environ["EBAY_DEV_ID"],
        "X-EBAY-API-CERT-NAME": os.environ["EBAY_CERT_ID"],
        "Content-Type": "text/xml",
    }


def build_get_seller_list_xml(
    token: str,
    page: int,
    end_from: datetime,
    end_to: datetime,
    per_page: int = MAX_ENTRIES_PER_PAGE,
) -> str:
    """Build one page's GetSellerList request body.

    Pure (no HTTP). The token is XML-escaped defensively.

    ``GranularityLevel`` and ``DetailLevel`` are both sent because this exact
    combination was measured returning all eight fields the report needs;
    dropping either was never tested, and a missing field here shows up as
    silently blank data downstream.

    Args:
        token: The seller account's Trading API user token.
        page: 1-based page number.
        end_from: Start of the listing-end-time window.
        end_to: End of that window.
        per_page: Entries per page; clamped to :data:`MAX_ENTRIES_PER_PAGE`.

    Returns:
        The XML request body.
    """
    per_page = min(per_page, MAX_ENTRIES_PER_PAGE)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<GetSellerListRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<RequesterCredentials><eBayAuthToken>{html.escape(token)}</eBayAuthToken></RequesterCredentials>"
        "<GranularityLevel>Fine</GranularityLevel>"
        f"<EndTimeFrom>{_iso_z(end_from)}</EndTimeFrom>"
        f"<EndTimeTo>{_iso_z(end_to)}</EndTimeTo>"
        "<IncludeWatchCount>true</IncludeWatchCount>"
        f"<Pagination><EntriesPerPage>{int(per_page)}</EntriesPerPage>"
        f"<PageNumber>{int(page)}</PageNumber></Pagination>"
        "<DetailLevel>ReturnAll</DetailLevel>"
        "</GetSellerListRequest>"
    )


def parse_seller_list(xml: bytes | str) -> dict:
    """Parse a GetSellerList response into acks, paging info and typed listings.

    Pure (no HTTP). Namespaces are stripped so elements are reachable by local
    name. Every listing on the page is returned regardless of status — the
    caller decides what to keep, so the same parse serves both the active sweep
    and any later audit of what ended.

    Args:
        xml: The raw response body.

    Returns:
        ``{"ack": str, "errors": list[str], "error_codes": list[str],
        "total_entries": int, "total_pages": int, "items": list[dict]}``.
        ``errors`` carries every ``Errors`` element as ``"code: message"``;
        ``error_codes`` carries only the codes whose ``SeverityCode`` is not
        ``Warning`` (absent counts as an error), since a warning riding along on
        a failure is not what failed it. Each item carries ``item_number``,
        ``title``, ``sku``, ``current_price``, ``sold_quantity``, ``watchers``,
        ``start_time`` (aware UTC), ``category_path``, ``category`` (rolled up)
        and ``listing_status``.
    """
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    root = ET.fromstring(xml)
    for el in root.iter():
        el.tag = el.tag.split("}")[-1]

    errors, error_codes = _error_fields(root)

    pagination = root.find(".//PaginationResult")
    items: list[dict] = []
    for it in root.findall(".//Item"):
        category_path = _text(it, "PrimaryCategory/CategoryName")
        items.append({
            "item_number": _text(it, "ItemID"),
            "title": _text(it, "Title"),
            "sku": _text(it, "SKU"),
            "current_price": _as_float(_text(it, "SellingStatus/CurrentPrice")),
            "sold_quantity": _as_int(_text(it, "SellingStatus/QuantitySold")),
            "watchers": _as_int(_text(it, "WatchCount")),
            "start_time": _as_utc(_text(it, "ListingDetails/StartTime")),
            "category_path": category_path,
            "category": l1_category(category_path),
            "listing_status": _text(it, "SellingStatus/ListingStatus"),
        })

    return {
        "ack": root.findtext("Ack") or "",
        "errors": errors,
        "error_codes": error_codes,
        "total_entries": _as_int(pagination.findtext("TotalNumberOfEntries") if pagination is not None else None),
        "total_pages": _as_int(pagination.findtext("TotalNumberOfPages") if pagination is not None else None),
        "items": items,
    }


def build_get_item_xml(token: str, item_id: str) -> str:
    """Build a GetItem request for one listing.

    Pure (no HTTP). Used to enrich a short list of known item numbers — cheaper
    than sweeping a whole account when only a handful of listings matter.

    Args:
        token: The seller account's Trading API user token.
        item_id: The listing's eBay item number.

    Returns:
        The XML request body.
    """
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<RequesterCredentials><eBayAuthToken>{html.escape(token)}</eBayAuthToken></RequesterCredentials>"
        f"<ItemID>{html.escape(str(item_id))}</ItemID>"
        "<DetailLevel>ReturnAll</DetailLevel>"
        "</GetItemRequest>"
    )


def parse_item(xml: bytes | str) -> dict:
    """Parse a GetItem response into the fields a report needs.

    Pure (no HTTP). ``quantity_available`` is derived as ``Quantity`` minus
    ``QuantitySold`` — eBay does not return an "available" figure directly, and
    a multi-quantity listing that has sold out still reports its original
    ``Quantity``.

    Args:
        xml: The raw response body.

    Returns:
        ``{"ack", "errors", "error_codes", "item"}`` with ``errors`` and
        ``error_codes`` as :func:`parse_seller_list` describes them. ``item``
        carries ``item_number``, ``title``, ``sku``, ``current_price``,
        ``quantity``, ``quantity_sold``, ``quantity_available`` and
        ``listing_status``, or is None when absent.
    """
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    root = ET.fromstring(xml)
    for el in root.iter():
        el.tag = el.tag.split("}")[-1]

    errors, error_codes = _error_fields(root)
    element = root.find(".//Item")
    item = None
    if element is not None:
        quantity = _as_int(_text(element, "Quantity"))
        sold = _as_int(_text(element, "SellingStatus/QuantitySold"))
        item = {
            "item_number": _text(element, "ItemID"),
            "title": _text(element, "Title"),
            "sku": _text(element, "SKU"),
            "current_price": _as_float(_text(element, "SellingStatus/CurrentPrice")),
            "quantity": quantity,
            "quantity_sold": sold,
            "quantity_available": max(quantity - sold, 0),
            "listing_status": _text(element, "SellingStatus/ListingStatus"),
        }

    return {"ack": root.findtext("Ack") or "", "errors": errors, "error_codes": error_codes,
            "item": item}


def get_item(
    token: str,
    item_id: str,
    *,
    account: str | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict:
    """Fetch one listing's details.

    Retried exactly as :func:`count_active_listings` is: up to 3 attempts, 5s
    then 15s (:data:`TRADING_RETRY_DELAYS`), only on eBay 10007, HTTP 5xx or a
    connection, timeout or dropped-body error. Any other eBay error code and
    every 4xx raise on the first attempt. Each retry logs a WARNING naming the
    item, the account and the cause, and the earlier causes ride on the final
    exception as a note (Python 3.11+).

    Args:
        token: The seller account's Trading API user token.
        item_id: The listing's eBay item number.
        account: Account display name for the retry warnings. Optional so
            existing callers keep working; without it the warnings cannot say
            which account failed.
        sleep: Pause between attempts, defaulting to :func:`time.sleep`.
            Injectable so tests do not wait.

    Returns:
        The item dict described by :func:`parse_item`.

    Raises:
        RuntimeError: eBay returned a non-transient failure ack, or a transient
            one on every attempt (``"GetItem failed for <item_id>: [...]"``,
            unchanged from before retries existed), or a success ack with no
            item in the response, which is not retried.
        requests.RequestException: A non-transient transport failure (any 4xx)
            on the first attempt, or a transient one on every attempt.
    """
    result = _call_with_retry(
        "GetItem",
        build_get_item_xml(token, item_id),
        parse_item,
        account or "an unnamed account",
        # Resolved per call, not bound as a default, so a monkeypatched
        # time.sleep is honoured.
        sleep or time.sleep,
        item_id=item_id,
    )
    if result["item"] is None:
        raise RuntimeError(f"GetItem returned no item for {item_id}.")
    return result["item"]


def build_active_count_xml(token: str) -> str:
    """Build a GetMyeBaySelling request that asks only for the active-listing count.

    Pure (no HTTP). One entry is requested because only ``PaginationResult`` is
    wanted — this is the independent second opinion on how many active listings
    an account has, used to prove the GetSellerList sweep missed nothing.

    Args:
        token: The seller account's Trading API user token.

    Returns:
        The XML request body.
    """
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<RequesterCredentials><eBayAuthToken>{html.escape(token)}</eBayAuthToken></RequesterCredentials>"
        "<ActiveList><Include>true</Include>"
        "<Pagination><EntriesPerPage>1</EntriesPerPage><PageNumber>1</PageNumber></Pagination>"
        "</ActiveList>"
        "</GetMyeBaySellingRequest>"
    )


def parse_active_count(xml: bytes | str) -> dict:
    """Parse a GetMyeBaySelling response down to its active-listing total.

    Pure (no HTTP).

    Args:
        xml: The raw response body.

    Returns:
        ``{"ack": str, "errors": list[str], "error_codes": list[str],
        "total_entries": int}``, with ``errors`` and ``error_codes`` as
        :func:`parse_seller_list` describes them.
    """
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    root = ET.fromstring(xml)
    for el in root.iter():
        el.tag = el.tag.split("}")[-1]

    errors, error_codes = _error_fields(root)
    active = root.find(".//ActiveList")
    pagination = active.find("PaginationResult") if active is not None else None
    return {
        "ack": root.findtext("Ack") or "",
        "errors": errors,
        "error_codes": error_codes,
        "total_entries": _as_int(pagination.findtext("TotalNumberOfEntries") if pagination is not None else None),
    }


def _post(call_name: str, body: str, timeout: int = 180, attempts: int = 3) -> bytes:
    """POST one Trading call, retrying only transient transport failures.

    Retried, back to back: whatever :func:`is_transient_transport_error`
    accepts (connection, timeout, dropped body, HTTP 5xx). Everything else,
    every 4xx included, raises on the first attempt, because the same request
    fails the same way again. An eBay-level failure (a Failure ``Ack`` over
    HTTP 200) is not seen here at all — the caller reads the ack and decides.

    Every Trading call in this module goes through :func:`_call_with_retry`,
    which passes ``attempts=1`` so the two layers do not multiply. The default
    of 3 applies only to a direct call.

    Args:
        call_name: Trading call name for the header.
        body: The XML request body.
        timeout: Per-request timeout in seconds.
        attempts: Total transport attempts before giving up.

    Returns:
        The raw response body.

    Raises:
        requests.RequestException: A non-transient failure on any attempt, or a
            transient one on every attempt.
    """
    headers = trading_headers(call_name)
    payload = body.encode("utf-8")
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(TRADING_ENDPOINT, data=payload, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.content
        except requests.RequestException as exc:
            if attempt == attempts or not is_transient_transport_error(exc):
                raise
            log.warning(f"{call_name} transport error (attempt {attempt}/{attempts}): "
                        f"{type(exc).__name__}: {exc}. Retrying.")
    raise AssertionError("unreachable")


def is_transient_trading_failure(error_codes: list[str]) -> bool:
    """Decide whether a failed Trading response is worth sending again unchanged.

    Pure. Transient only when there is at least one error code and **every** one
    is in :data:`TRANSIENT_TRADING_ERROR_CODES`: a response carrying 10007
    alongside, say, an invalid-token error will fail the same way next time.

    Args:
        error_codes: Error-severity codes from the response, as
            :func:`parse_seller_list` returns them in ``error_codes``.

    Returns:
        True when a retry can plausibly succeed.
    """
    return bool(error_codes) and all(code in TRANSIENT_TRADING_ERROR_CODES for code in error_codes)


def is_transient_transport_error(exc: requests.RequestException) -> bool:
    """Decide whether a failed HTTP exchange is worth sending again unchanged.

    Pure. Transient: an HTTP 5xx, and a connection, timeout or dropped-body
    failure — each a fault in eBay or the network rather than in the request.
    Everything else is permanent, including **every 4xx**: 401/403 is a
    credential eBay will reject again, 400/404 is a malformed request, and 429
    on this keyset means a spent call budget that no short pause refills.

    Args:
        exc: The exception :func:`_post` raised.

    Returns:
        True when a retry can plausibly succeed.
    """
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code >= 500
    return isinstance(exc, (requests.ConnectionError, requests.Timeout,
                            requests.exceptions.ChunkedEncodingError))


def _call_with_retry(
    call_name: str,
    body: str,
    parse: Callable[[bytes], dict],
    account: str,
    sleep: Callable[[float], None],
    page: int | None = None,
    *,
    item_id: str | None = None,
) -> dict:
    """Send one Trading call, retrying that call alone on a transient failure.

    For the sweep this retries one page, not the account: one eBay blip on page
    22 of 27 used to throw away the 21 pages already fetched. This is the only
    retry layer for the calls routed through it — :func:`_post` is called with
    a single attempt so the two do not multiply.

    Up to ``len(TRADING_RETRY_DELAYS) + 1`` attempts, pausing per
    :data:`TRADING_RETRY_DELAYS`, and only when :func:`is_transient_trading_failure`
    or :func:`is_transient_transport_error` says so. Each retry logs a WARNING
    naming the call, page (if any), account, attempt and cause.

    Args:
        call_name: Trading call name, e.g. ``GetSellerList``.
        body: The XML request body, sent unchanged on every attempt.
        parse: Turns the raw response into a dict carrying ``ack``, ``errors``
            and ``error_codes``, as :func:`parse_seller_list`,
            :func:`parse_active_count` and :func:`parse_item` do.
        account: Label for the retry warnings; never the token.
        sleep: Pause between attempts, injectable so tests do not wait.
        page: 1-based page number for a paged call, named in every message;
            None for a single-shot call.
        item_id: Listing a per-item call is about, named in every message;
            None otherwise. Never combined with ``page``.

    Returns:
        The parsed response, with a ``Success`` or ``Warning`` ack.

    Raises:
        ValueError: Both ``page`` and ``item_id`` were given.
        RuntimeError: eBay returned a failure ack that is not transient, or a
            transient one on every attempt. The message is the one each caller
            raised before retries existed: ``"<call> failed on page N: [...]"``,
            ``"<call> failed for <item_id>: [...]"`` or ``"<call> failed: [...]"``.
        requests.RequestException: A transport failure that is not transient,
            or a transient one on every attempt.
    """
    if page is not None and item_id is not None:
        raise ValueError(f"{call_name}: pass page or item_id, not both.")
    if page is not None:
        subject, failed = f"{call_name} page {page}", f"{call_name} failed on page {page}"
    elif item_id is not None:
        subject, failed = f"{call_name} item {item_id}", f"{call_name} failed for {item_id}"
    else:
        subject, failed = call_name, f"{call_name} failed"
    attempts = len(TRADING_RETRY_DELAYS) + 1
    earlier: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            result = parse(_post(call_name, body, attempts=1))
        except requests.RequestException as exc:
            if attempt == attempts or not is_transient_transport_error(exc):
                _note_earlier_attempts(exc, earlier, subject, account)
                raise
            cause = f"{type(exc).__name__}: {exc}"
        else:
            if result["ack"] in ("Success", "Warning"):
                return result
            if attempt == attempts or not is_transient_trading_failure(result["error_codes"]):
                failure = RuntimeError(f"{failed}: {result['errors'] or result['ack']}")
                _note_earlier_attempts(failure, earlier, subject, account)
                raise failure
            cause = "; ".join(result["errors"])

        delay = TRADING_RETRY_DELAYS[attempt - 1]
        earlier.append(f"attempt {attempt}/{attempts}: {cause}")
        log.warning(f"{subject} for {account} failed transiently "
                    f"(attempt {attempt}/{attempts}): {cause}. Retrying in {delay:g}s.")
        sleep(delay)
    raise AssertionError("unreachable")


def _note_earlier_attempts(exc: BaseException, earlier: list[str], subject: str, account: str) -> None:
    """Attach the causes of a call's earlier attempts to the exception that ends it.

    The crash mail carries the traceback but not the log, so without this only
    the last attempt's cause reaches it. A note leaves the exception's type and
    message untouched, which is what callers match on. Notes need Python 3.11;
    on 3.10 the WARNING log lines are the only record.

    Args:
        exc: The exception about to be raised.
        earlier: One line per failed earlier attempt, oldest first.
        subject: The call, and page if any, e.g. ``GetSellerList page 3``.
        account: Account label, never the token.
    """
    if earlier and hasattr(exc, "add_note"):
        exc.add_note(f"{subject} for {account} failed on earlier attempts: " + " | ".join(earlier))


def count_active_listings(
    token: str,
    *,
    account: str | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    """Ask eBay how many active listings an account has.

    A single call, independent of the GetSellerList sweep, so the two can be
    compared as a completeness check. Retried exactly as one sweep page is (see
    :func:`get_active_listings`): up to 3 attempts, 5s then 15s, only on eBay
    10007, HTTP 5xx or a connection, timeout or dropped-body error, with a
    WARNING per retry and the earlier causes attached to the final exception as
    a note (Python 3.11+).

    Args:
        token: The seller account's Trading API user token.
        account: Account display name for the retry warnings. Optional so
            existing callers keep working; without it the warnings cannot say
            which account failed.
        sleep: Pause between attempts, defaulting to :func:`time.sleep`.
            Injectable so tests do not wait.

    Returns:
        The active-listing count.

    Raises:
        RuntimeError: eBay returned a non-transient failure ack, or a transient
            one on every attempt. Type and message are unchanged from before
            retries existed.
        requests.RequestException: A non-transient transport failure, or a
            transient one on every attempt.
    """
    result = _call_with_retry(
        "GetMyeBaySelling",
        build_active_count_xml(token),
        parse_active_count,
        account or "an unnamed account",
        # Resolved per call, not bound as a default, so a monkeypatched
        # time.sleep is honoured.
        sleep or time.sleep,
    )
    return result["total_entries"]


def get_active_listings(
    token: str,
    now: datetime | None = None,
    per_page: int = MAX_ENTRIES_PER_PAGE,
    *,
    account: str | None = None,
    sleep: Callable[[float], None] | None = None,
) -> list[dict]:
    """Fetch every active listing for one seller account.

    Walks GetSellerList over an end-time window wide enough to cover the whole
    active inventory, then keeps only listings eBay reports as ``Active``.
    That filter is not cosmetic: the response is ordered by end time ascending,
    so the first page is dense with listings that ended earlier the same day
    (22 of 200 when measured), and inserting them would quietly pad the report.

    Each page is retried on its own, up to ``len(TRADING_RETRY_DELAYS) + 1``
    attempts (3) with the pauses in :data:`TRADING_RETRY_DELAYS` (5s, then 15s), and
    only on a transient failure: eBay error 10007, HTTP 5xx, or a connection,
    timeout or dropped-body error. An auth error, any other eBay error code and
    any 4xx raise on the first attempt. Every retry logs a WARNING naming the
    account, page, attempt and cause, and when the page finally fails those
    causes are also attached to the exception as a note (Python 3.11+), so the
    crash mail's traceback carries every attempt, not only the last. Pages
    already fetched are kept, and the retried page lands in its own place, so
    order is unchanged.

    Args:
        token: The seller account's Trading API user token.
        now: Reference time, defaulting to the current UTC time. Injectable so
            the window can be pinned in tests.
        per_page: Entries per page; clamped to :data:`MAX_ENTRIES_PER_PAGE`.
        account: Account display name for the retry warnings. Optional so
            existing callers keep working; without it the warnings cannot say
            which account failed.
        sleep: Pause between attempts, defaulting to :func:`time.sleep`.
            Injectable so tests do not wait.

    Returns:
        One dict per active listing, in the order eBay returned them.

    Raises:
        RuntimeError: eBay returned a non-transient failure ack, or a transient
            one on every attempt for the same page, or the page count exceeded
            :data:`_MAX_PAGES`. The type and message are unchanged from before
            retries existed; earlier attempts ride along only as a note.
        requests.RequestException: A non-transient transport failure, or a
            transient one on every attempt for the same page.
    """
    now = now or datetime.now(timezone.utc)
    end_from = now - timedelta(days=WINDOW_LOOKBACK_DAYS)
    end_to = now + timedelta(days=WINDOW_FORWARD_DAYS)
    label = account or "an unnamed account"
    # Resolved per call, not bound as a default, so a monkeypatched time.sleep
    # is honoured.
    pause = sleep or time.sleep

    listings: list[dict] = []
    dropped = 0
    page = 1
    total_pages = 1
    while page <= total_pages:
        if page > _MAX_PAGES:
            raise RuntimeError(f"GetSellerList exceeded {_MAX_PAGES} pages — refusing to keep paging.")

        body = build_get_seller_list_xml(token, page, end_from, end_to, per_page)
        result = _call_with_retry("GetSellerList", body, parse_seller_list, label, pause, page=page)

        total_pages = result["total_pages"] or 1
        active = [i for i in result["items"] if i["listing_status"] == "Active"]
        dropped += len(result["items"]) - len(active)
        listings.extend(active)
        log.info(f"Fetched page {page}/{total_pages} ({len(listings):,} active so far).")
        page += 1

    if dropped:
        log.info(f"Skipped {dropped:,} listings that were no longer active.")
    return listings
