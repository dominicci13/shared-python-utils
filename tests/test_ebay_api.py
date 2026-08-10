"""Unit tests for `seller_automation_utils.ebay_api`.

The build and parse halves are pure, so they are tested directly against the XML
shapes eBay actually returned on 2026-08-10 (namespaced, with the fields the
Items-Categories report reads). The paging loop is tested by replacing the single
HTTP helper, so no test touches the network.
"""
from __future__ import annotations

from datetime import datetime, timezone
from xml.sax.saxutils import escape

import pytest

from seller_automation_utils import ebay_api

NS = "urn:ebay:apis:eBLBaseComponents"


def item_xml(
    item_id: str = "123456789012",
    title: str = "Acme AM-16 16 Channel Audio Monitor",
    sku: str | None = "ACM-AM16",
    price: str | None = "1088.0",
    sold: str | None = "0",
    watchers: str | None = "3",
    start: str | None = "2025-06-10T03:07:39.000Z",
    category: str | None = "Cameras & Photo:Video Production & Editing:Video Monitors",
    status: str = "Active",
) -> str:
    """Build one <Item> the way GetSellerList returns it, omitting empty fields.

    Text is escaped because real category names carry ``&`` ("Cameras & Photo"),
    which is not well-formed XML unescaped.
    """
    parts = [f"<ItemID>{item_id}</ItemID>", f"<Title>{escape(title)}</Title>"]
    if sku is not None:
        parts.append(f"<SKU>{escape(sku)}</SKU>")
    if watchers is not None:
        parts.append(f"<WatchCount>{watchers}</WatchCount>")
    if category is not None:
        parts.append(
            "<PrimaryCategory><CategoryID>1</CategoryID>"
            f"<CategoryName>{escape(category)}</CategoryName></PrimaryCategory>"
        )
    if start is not None:
        parts.append(f"<ListingDetails><StartTime>{start}</StartTime></ListingDetails>")
    selling = [f"<ListingStatus>{status}</ListingStatus>"]
    if price is not None:
        selling.append(f'<CurrentPrice currencyID="USD">{price}</CurrentPrice>')
    if sold is not None:
        selling.append(f"<QuantitySold>{sold}</QuantitySold>")
    parts.append(f"<SellingStatus>{''.join(selling)}</SellingStatus>")
    return f"<Item>{''.join(parts)}</Item>"


def seller_list_xml(items: str = "", ack: str = "Success", entries: int = 1,
                    pages: int = 1, errors: str = "", paginate: bool = True) -> str:
    """Build a namespaced GetSellerListResponse."""
    pagination = (
        f"<PaginationResult><TotalNumberOfEntries>{entries}</TotalNumberOfEntries>"
        f"<TotalNumberOfPages>{pages}</TotalNumberOfPages></PaginationResult>"
    ) if paginate else ""
    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<GetSellerListResponse xmlns="{NS}">'
        f"<Ack>{ack}</Ack>{errors}"
        f"<ItemArray>{items}</ItemArray>{pagination}"
        f"</GetSellerListResponse>"
    )


def error_xml(code: str = "21917053", message: str = "Invalid token.") -> str:
    return f"<Errors><ErrorCode>{code}</ErrorCode><LongMessage>{message}</LongMessage></Errors>"


# --- naming and credentials --------------------------------------------------

@pytest.mark.parametrize("account,expected", [
    ("AccountB", "EBAY_AUTH_TOKEN_ACCOUNTB"),
    ("AccountA", "EBAY_AUTH_TOKEN_ACCOUNTA"),
    ("AccountC", "EBAY_AUTH_TOKEN_ACCOUNTC"),
    ("Some Account-1", "EBAY_AUTH_TOKEN_SOMEACCOUNT1"),
])
def test_token_env_var_strips_non_alphanumerics(account, expected):
    assert ebay_api.token_env_var(account) == expected


def test_account_token_reads_the_environment(monkeypatch):
    monkeypatch.setenv("EBAY_AUTH_TOKEN_ACCOUNTA", "a-token")
    assert ebay_api.account_token("AccountA") == "a-token"


@pytest.mark.parametrize("value", ["", "   "])
def test_account_token_names_the_missing_variable(monkeypatch, value):
    monkeypatch.setenv("EBAY_AUTH_TOKEN_ACCOUNTA", value)
    with pytest.raises(RuntimeError, match="EBAY_AUTH_TOKEN_ACCOUNTA"):
        ebay_api.account_token("AccountA")


def test_account_token_error_never_leaks_a_token(monkeypatch):
    monkeypatch.delenv("EBAY_AUTH_TOKEN_ACCOUNTA", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        ebay_api.account_token("AccountA")
    assert "token for 'AccountA'" in str(excinfo.value)


# --- category rollup ---------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("Cameras & Photo:Video Production & Editing:Video Monitors", "Cameras & Photo"),
    ("Sporting Goods:Hunting:Scopes", "Sporting Goods"),
    # The scraper wrote "/" as "-"; the API path must land on the same string.
    ("Computers/Tablets & Networking:Laptops", "Computers-Tablets & Networking"),
    ("Consumer Electronics", "Consumer Electronics"),
    ("  Home & Garden : Tools ", "Home & Garden"),
    ("", ""),
    (None, ""),
])
def test_l1_category(path, expected):
    assert ebay_api.l1_category(path) == expected


# --- timestamps --------------------------------------------------------------

def test_to_seller_local_converts_utc_to_pacific_daylight():
    moment = datetime(2025, 6, 10, 3, 7, 39, tzinfo=timezone.utc)
    assert ebay_api.to_seller_local(moment) == datetime(2025, 6, 9, 20, 7, 39)


def test_to_seller_local_handles_standard_time():
    moment = datetime(2025, 11, 10, 21, 26, 29, tzinfo=timezone.utc)
    assert ebay_api.to_seller_local(moment) == datetime(2025, 11, 10, 13, 26, 29)


def test_to_seller_local_result_is_naive_for_sql():
    assert ebay_api.to_seller_local(datetime(2025, 6, 10, 3, 0, tzinfo=timezone.utc)).tzinfo is None


def test_to_seller_local_passes_none_through():
    assert ebay_api.to_seller_local(None) is None


# --- request building --------------------------------------------------------

def test_build_get_seller_list_xml_carries_page_and_window():
    body = ebay_api.build_get_seller_list_xml(
        "tok", 7,
        datetime(2026, 8, 9, tzinfo=timezone.utc),
        datetime(2026, 11, 8, tzinfo=timezone.utc),
    )
    assert "<PageNumber>7</PageNumber>" in body
    assert "<EndTimeFrom>2026-08-09T00:00:00.000Z</EndTimeFrom>" in body
    assert "<EndTimeTo>2026-11-08T00:00:00.000Z</EndTimeTo>" in body
    assert "<IncludeWatchCount>true</IncludeWatchCount>" in body


def test_build_get_seller_list_xml_clamps_oversized_pages():
    body = ebay_api.build_get_seller_list_xml(
        "tok", 1, datetime(2026, 8, 9, tzinfo=timezone.utc), datetime(2026, 11, 8, tzinfo=timezone.utc),
        per_page=5000,
    )
    assert f"<EntriesPerPage>{ebay_api.MAX_ENTRIES_PER_PAGE}</EntriesPerPage>" in body


def test_build_get_seller_list_xml_escapes_the_token():
    body = ebay_api.build_get_seller_list_xml(
        "a&b<c", 1, datetime(2026, 8, 9, tzinfo=timezone.utc), datetime(2026, 11, 8, tzinfo=timezone.utc),
    )
    assert "a&amp;b&lt;c" in body
    assert "a&b<c" not in body


def test_build_get_seller_list_xml_keeps_both_detail_switches():
    # Both were measured returning every field the report needs; dropping either
    # is untested and would surface as silently blank data.
    body = ebay_api.build_get_seller_list_xml(
        "tok", 1, datetime(2026, 8, 9, tzinfo=timezone.utc), datetime(2026, 11, 8, tzinfo=timezone.utc),
    )
    assert "<GranularityLevel>Fine</GranularityLevel>" in body
    assert "<DetailLevel>ReturnAll</DetailLevel>" in body


# --- response parsing --------------------------------------------------------

def test_parse_seller_list_maps_every_reported_field():
    result = ebay_api.parse_seller_list(seller_list_xml(item_xml()))
    assert result["ack"] == "Success"
    assert result["total_entries"] == 1
    item = result["items"][0]
    assert item["item_number"] == "123456789012"
    assert item["sku"] == "ACM-AM16"
    assert item["current_price"] == 1088.0
    assert item["sold_quantity"] == 0
    assert item["watchers"] == 3
    assert item["start_time"] == datetime(2025, 6, 10, 3, 7, 39, tzinfo=timezone.utc)
    assert item["category"] == "Cameras & Photo"
    assert item["listing_status"] == "Active"


def test_parse_seller_list_defaults_absent_numerics_to_zero():
    xml = seller_list_xml(item_xml(price=None, sold=None, watchers=None))
    item = ebay_api.parse_seller_list(xml)["items"][0]
    assert (item["current_price"], item["sold_quantity"], item["watchers"]) == (0.0, 0, 0)


def test_parse_seller_list_reports_absent_sku_as_none():
    # The report drops SKU-less rows, so "" and None must not be confused.
    item = ebay_api.parse_seller_list(seller_list_xml(item_xml(sku=None)))["items"][0]
    assert item["sku"] is None


def test_parse_seller_list_tolerates_an_unparseable_start_time():
    item = ebay_api.parse_seller_list(seller_list_xml(item_xml(start="not-a-date")))["items"][0]
    assert item["start_time"] is None


def test_parse_seller_list_accepts_a_start_time_without_milliseconds():
    item = ebay_api.parse_seller_list(seller_list_xml(item_xml(start="2025-06-10T03:07:39Z")))["items"][0]
    assert item["start_time"] == datetime(2025, 6, 10, 3, 7, 39, tzinfo=timezone.utc)


def test_parse_seller_list_handles_a_missing_category():
    item = ebay_api.parse_seller_list(seller_list_xml(item_xml(category=None)))["items"][0]
    assert item["category"] == ""
    assert item["category_path"] is None


def test_parse_seller_list_collects_errors_and_failure_ack():
    xml = seller_list_xml(ack="Failure", errors=error_xml())
    result = ebay_api.parse_seller_list(xml)
    assert result["ack"] == "Failure"
    assert result["errors"] == ["21917053: Invalid token."]


def test_parse_seller_list_survives_a_response_with_no_pagination():
    result = ebay_api.parse_seller_list(seller_list_xml(paginate=False))
    assert (result["total_entries"], result["total_pages"]) == (0, 0)


def test_parse_seller_list_keeps_inactive_listings_for_the_caller_to_judge():
    xml = seller_list_xml(item_xml(status="Completed") + item_xml(status="Active"))
    statuses = [i["listing_status"] for i in ebay_api.parse_seller_list(xml)["items"]]
    assert statuses == ["Completed", "Active"]


def test_parse_active_count_reads_the_total():
    xml = (
        f'<GetMyeBaySellingResponse xmlns="{NS}"><Ack>Success</Ack>'
        "<ActiveList><PaginationResult><TotalNumberOfEntries>12254</TotalNumberOfEntries>"
        "</PaginationResult></ActiveList></GetMyeBaySellingResponse>"
    )
    assert ebay_api.parse_active_count(xml)["total_entries"] == 12254


def test_parse_active_count_survives_a_missing_active_list():
    xml = f'<GetMyeBaySellingResponse xmlns="{NS}"><Ack>Failure</Ack>{error_xml()}</GetMyeBaySellingResponse>'
    result = ebay_api.parse_active_count(xml)
    assert result["total_entries"] == 0
    assert result["errors"] == ["21917053: Invalid token."]


# --- the paging sweep --------------------------------------------------------

@pytest.fixture
def captured_posts(monkeypatch):
    """Replace the HTTP helper, recording each request body and serving queued responses."""
    calls: list[tuple[str, str]] = []
    queue: list[str] = []

    def fake_post(call_name: str, body: str, **kwargs):
        calls.append((call_name, body))
        return queue.pop(0).encode("utf-8")

    monkeypatch.setattr(ebay_api, "_post", fake_post)
    return calls, queue


def test_get_active_listings_drops_listings_that_already_ended(captured_posts):
    calls, queue = captured_posts
    queue.append(seller_list_xml(item_xml(item_id="1", status="Completed") + item_xml(item_id="2")))
    listings = ebay_api.get_active_listings("tok")
    assert [i["item_number"] for i in listings] == ["2"]


def test_get_active_listings_walks_every_page(captured_posts):
    calls, queue = captured_posts
    queue.append(seller_list_xml(item_xml(item_id="1"), pages=3))
    queue.append(seller_list_xml(item_xml(item_id="2"), pages=3))
    queue.append(seller_list_xml(item_xml(item_id="3"), pages=3))
    listings = ebay_api.get_active_listings("tok")
    assert [i["item_number"] for i in listings] == ["1", "2", "3"]
    assert all(f"<PageNumber>{n}</PageNumber>" in body for n, (_, body) in enumerate(calls, start=1))


def test_get_active_listings_builds_the_window_from_the_given_now(captured_posts):
    calls, queue = captured_posts
    queue.append(seller_list_xml(item_xml()))
    ebay_api.get_active_listings("tok", now=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc))
    body = calls[0][1]
    assert "<EndTimeFrom>2026-08-09T12:00:00.000Z</EndTimeFrom>" in body
    assert "<EndTimeTo>2026-11-08T12:00:00.000Z</EndTimeTo>" in body


def test_get_active_listings_raises_on_a_failure_ack(captured_posts):
    calls, queue = captured_posts
    queue.append(seller_list_xml(ack="Failure", errors=error_xml()))
    with pytest.raises(RuntimeError, match="Invalid token"):
        ebay_api.get_active_listings("tok")


def test_get_active_listings_stops_a_runaway_page_loop(monkeypatch, captured_posts):
    calls, queue = captured_posts
    monkeypatch.setattr(ebay_api, "_MAX_PAGES", 2)
    for _ in range(5):
        queue.append(seller_list_xml(item_xml(), pages=99))
    with pytest.raises(RuntimeError, match="refusing to keep paging"):
        ebay_api.get_active_listings("tok")


def test_get_active_listings_treats_warning_as_success(captured_posts):
    calls, queue = captured_posts
    queue.append(seller_list_xml(item_xml(), ack="Warning"))
    assert len(ebay_api.get_active_listings("tok")) == 1


def test_count_active_listings_returns_the_total(monkeypatch):
    xml = (
        f'<GetMyeBaySellingResponse xmlns="{NS}"><Ack>Success</Ack>'
        "<ActiveList><PaginationResult><TotalNumberOfEntries>12254</TotalNumberOfEntries>"
        "</PaginationResult></ActiveList></GetMyeBaySellingResponse>"
    )
    monkeypatch.setattr(ebay_api, "_post", lambda *a, **k: xml.encode("utf-8"))
    assert ebay_api.count_active_listings("tok") == 12254


def test_count_active_listings_raises_on_a_failure_ack(monkeypatch):
    xml = f'<GetMyeBaySellingResponse xmlns="{NS}"><Ack>Failure</Ack>{error_xml()}</GetMyeBaySellingResponse>'
    monkeypatch.setattr(ebay_api, "_post", lambda *a, **k: xml.encode("utf-8"))
    with pytest.raises(RuntimeError, match="Invalid token"):
        ebay_api.count_active_listings("tok")
