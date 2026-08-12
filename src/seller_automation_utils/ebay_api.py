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
import xml.etree.ElementTree as ET
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


OAUTH_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
TRAFFIC_REPORT_URL = "https://api.ebay.com/sell/analytics/v1/traffic_report"
ANALYTICS_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.analytics.readonly"

# eBay rejects a longer list outright (errorId 50028), it does not silently trim.
MAX_LISTING_IDS_PER_CALL = 200

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
    and rejects anything longer outright. Every requested id comes back in the
    result, defaulting to 0, so callers never have to distinguish "no traffic"
    from "not returned".

    Args:
        account: eBay account display name.
        listing_ids: Item numbers to look up.
        days: Length of the trailing window, ending yesterday.

    Returns:
        ``{listing_id: views}`` covering every id passed in.

    Raises:
        RuntimeError: eBay refused the grant or returned an error response.
    """
    if not listing_ids:
        return {}

    token = oauth_access_token(account)
    end = datetime.now(timezone.utc).date() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    window = f"{start:%Y%m%d}..{end:%Y%m%d}"

    views: dict[str, int] = {}
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
        # not help, so say what actually happened. Measured 2026-08-11: the
        # sell.analytics.traffic_report limit is 100 calls per 24h for the whole
        # application, while the four accounts need 121 at 200 ids per call.
        if response.status_code == 429:
            raise RuntimeError(
                f"getTrafficReport hit eBay's daily call limit for {account} on batch "
                f"{number} of {len(range(0, len(listing_ids), MAX_LISTING_IDS_PER_CALL))}. "
                "The sell.analytics.traffic_report quota is per application per day and is "
                "shared across every automation on this keyset; it resets at 07:00 UTC. "
                "Check the remaining budget with the Developer Analytics rate_limit resource."
            )
        if response.status_code != 200:
            raise RuntimeError(f"getTrafficReport failed for {account} "
                               f"(batch {number}): {response.status_code} {response.text[:300]}")
        views.update(parse_traffic_report(response.json()))
        log.info(f"Fetched views for {min(offset + len(batch), len(listing_ids)):,}"
                 f"/{len(listing_ids):,} listings.")

    return {listing_id: views.get(listing_id, 0) for listing_id in listing_ids}


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
        ``{"ack": str, "errors": list[str], "total_entries": int,
        "total_pages": int, "items": list[dict]}``, where each item carries
        ``item_number``, ``title``, ``sku``, ``current_price``,
        ``sold_quantity``, ``watchers``, ``start_time`` (aware UTC),
        ``category_path``, ``category`` (rolled up) and ``listing_status``.
    """
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    root = ET.fromstring(xml)
    for el in root.iter():
        el.tag = el.tag.split("}")[-1]

    errors = [
        f"{e.findtext('ErrorCode')}: {e.findtext('LongMessage')}"
        for e in root.findall(".//Errors")
    ]

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
        ``{"ack", "errors", "item"}`` where ``item`` carries ``item_number``,
        ``title``, ``sku``, ``current_price``, ``quantity``, ``quantity_sold``,
        ``quantity_available`` and ``listing_status``, or None when absent.
    """
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    root = ET.fromstring(xml)
    for el in root.iter():
        el.tag = el.tag.split("}")[-1]

    errors = [
        f"{e.findtext('ErrorCode')}: {e.findtext('LongMessage')}"
        for e in root.findall(".//Errors")
    ]
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

    return {"ack": root.findtext("Ack") or "", "errors": errors, "item": item}


def get_item(token: str, item_id: str) -> dict:
    """Fetch one listing's details.

    Args:
        token: The seller account's Trading API user token.
        item_id: The listing's eBay item number.

    Returns:
        The item dict described by :func:`parse_item`.

    Raises:
        RuntimeError: eBay returned a failure ack, or no item in the response.
    """
    result = parse_item(_post("GetItem", build_get_item_xml(token, item_id)))
    if result["ack"] not in ("Success", "Warning"):
        raise RuntimeError(f"GetItem failed for {item_id}: {result['errors'] or result['ack']}")
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
        ``{"ack": str, "errors": list[str], "total_entries": int}``.
    """
    if isinstance(xml, str):
        xml = xml.encode("utf-8")
    root = ET.fromstring(xml)
    for el in root.iter():
        el.tag = el.tag.split("}")[-1]

    active = root.find(".//ActiveList")
    pagination = active.find("PaginationResult") if active is not None else None
    return {
        "ack": root.findtext("Ack") or "",
        "errors": [
            f"{e.findtext('ErrorCode')}: {e.findtext('LongMessage')}"
            for e in root.findall(".//Errors")
        ],
        "total_entries": _as_int(pagination.findtext("TotalNumberOfEntries") if pagination is not None else None),
    }


def _post(call_name: str, body: str, timeout: int = 180, attempts: int = 3) -> bytes:
    """POST one Trading call, retrying only transport failures.

    A sweep is dozens of sequential calls, so a single dropped connection must
    not lose the account. An eBay-level failure (a Failure ``Ack``) is not
    retried here — the caller reads the ack and decides, because retrying a
    rejected request just repeats it.

    Args:
        call_name: Trading call name for the header.
        body: The XML request body.
        timeout: Per-request timeout in seconds.
        attempts: Total transport attempts before giving up.

    Returns:
        The raw response body.

    Raises:
        requests.RequestException: Every attempt failed at the transport level.
    """
    headers = trading_headers(call_name)
    payload = body.encode("utf-8")
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(TRADING_ENDPOINT, data=payload, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.content
        except requests.RequestException:
            if attempt == attempts:
                raise
            log.warning(f"{call_name} transport error (attempt #{attempt}/{attempts}). Retrying.")
    raise AssertionError("unreachable")


def count_active_listings(token: str) -> int:
    """Ask eBay how many active listings an account has.

    A single call, independent of the GetSellerList sweep, so the two can be
    compared as a completeness check.

    Args:
        token: The seller account's Trading API user token.

    Returns:
        The active-listing count.

    Raises:
        RuntimeError: eBay returned a failure ack.
    """
    result = parse_active_count(_post("GetMyeBaySelling", build_active_count_xml(token)))
    if result["ack"] not in ("Success", "Warning"):
        raise RuntimeError(f"GetMyeBaySelling failed: {result['errors'] or result['ack']}")
    return result["total_entries"]


def get_active_listings(
    token: str,
    now: datetime | None = None,
    per_page: int = MAX_ENTRIES_PER_PAGE,
) -> list[dict]:
    """Fetch every active listing for one seller account.

    Walks GetSellerList over an end-time window wide enough to cover the whole
    active inventory, then keeps only listings eBay reports as ``Active``.
    That filter is not cosmetic: the response is ordered by end time ascending,
    so the first page is dense with listings that ended earlier the same day
    (22 of 200 when measured), and inserting them would quietly pad the report.

    Args:
        token: The seller account's Trading API user token.
        now: Reference time, defaulting to the current UTC time. Injectable so
            the window can be pinned in tests.
        per_page: Entries per page; clamped to :data:`MAX_ENTRIES_PER_PAGE`.

    Returns:
        One dict per active listing, in the order eBay returned them.

    Raises:
        RuntimeError: eBay returned a failure ack, or the page count exceeded
            :data:`_MAX_PAGES`.
    """
    now = now or datetime.now(timezone.utc)
    end_from = now - timedelta(days=WINDOW_LOOKBACK_DAYS)
    end_to = now + timedelta(days=WINDOW_FORWARD_DAYS)

    listings: list[dict] = []
    dropped = 0
    page = 1
    total_pages = 1
    while page <= total_pages:
        if page > _MAX_PAGES:
            raise RuntimeError(f"GetSellerList exceeded {_MAX_PAGES} pages — refusing to keep paging.")

        body = build_get_seller_list_xml(token, page, end_from, end_to, per_page)
        result = parse_seller_list(_post("GetSellerList", body))
        if result["ack"] not in ("Success", "Warning"):
            raise RuntimeError(f"GetSellerList failed on page {page}: {result['errors'] or result['ack']}")

        total_pages = result["total_pages"] or 1
        active = [i for i in result["items"] if i["listing_status"] == "Active"]
        dropped += len(result["items"]) - len(active)
        listings.extend(active)
        log.info(f"Fetched page {page}/{total_pages} ({len(listings):,} active so far).")
        page += 1

    if dropped:
        log.info(f"Skipped {dropped:,} listings that were no longer active.")
    return listings
