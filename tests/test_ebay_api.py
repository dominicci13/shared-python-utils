"""Unit tests for `seller_automation_utils.ebay_api`.

The build and parse halves are pure, so they are tested directly against the XML
shapes eBay actually returned on 2026-08-10 (namespaced, with the fields the
Items-Categories report reads). The paging loop is tested by replacing the single
HTTP helper, so no test touches the network.
"""
from __future__ import annotations

import inspect
import sys
import traceback
from datetime import datetime, timezone
from xml.sax.saxutils import escape

import pytest
import requests

from seller_automation_utils import ebay_api

NS = "urn:ebay:apis:eBLBaseComponents"


def item_xml(
    item_id: str = "123456789012",
    title: str = "Acme AM-16 16 Channel Audio Monitor",
    sku: str | None = "ACM-AM16",
    price: str | None = "999.0",
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


def error_xml(code: str = "21917053", message: str = "Invalid token.",
              severity: str | None = None) -> str:
    severity_xml = f"<SeverityCode>{severity}</SeverityCode>" if severity else ""
    return (f"<Errors><ErrorCode>{code}</ErrorCode><LongMessage>{message}</LongMessage>"
            f"{severity_xml}</Errors>")


# --- naming and credentials --------------------------------------------------

@pytest.mark.parametrize("account,expected", [
    ("AccountB", "EBAY_AUTH_TOKEN_ACCOUNTB"),
    ("AccountA", "EBAY_AUTH_TOKEN_ACCOUNTA"),
    ("Account-C", "EBAY_AUTH_TOKEN_ACCOUNTC"),
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
    assert item["current_price"] == 999.0
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
    """Replace the HTTP helper, recording each request body and serving queued responses.

    A queued exception is raised instead of returned, standing in for a transport
    failure. Real sleeping is made a test failure, so a retry path that forgets
    the injected pause cannot slow the suite down unnoticed.
    """
    calls: list[tuple[str, str]] = []
    queue: list[str | Exception] = []

    def fake_post(call_name: str, body: str, **kwargs):
        calls.append((call_name, body))
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response.encode("utf-8")

    def no_real_sleep(seconds):
        raise AssertionError(f"time.sleep({seconds}) called for real inside a test")

    monkeypatch.setattr(ebay_api, "_post", fake_post)
    monkeypatch.setattr(ebay_api.time, "sleep", no_real_sleep)
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


# --- per-page retry ----------------------------------------------------------

SYSTEM_ERROR = "System error. Unable to process your request. Please try again later."


def http_error(status: int) -> requests.HTTPError:
    """An HTTPError carrying a response, as `raise_for_status` builds it."""
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} for url: {ebay_api.TRADING_ENDPOINT}", response=response)


def transient_ack() -> str:
    return seller_list_xml(ack="Failure", errors=error_xml("10007", SYSTEM_ERROR, "Error"))


# Each failure kind the page retry must absorb, as it reaches the pager.
TRANSIENT_FAILURES = {
    "ebay-10007": transient_ack,
    "ebay-10007-plus-warning": lambda: seller_list_xml(
        ack="Failure",
        errors=error_xml("10007", SYSTEM_ERROR, "Error") + error_xml("21917062", "Heads up.", "Warning"),
    ),
    "http-500": lambda: http_error(500),
    "http-503": lambda: http_error(503),
    "connection": lambda: requests.ConnectionError("connection reset"),
    "read-timeout": lambda: requests.ReadTimeout("read timed out"),
    "dropped-body": lambda: requests.exceptions.ChunkedEncodingError("connection broken"),
}


def page_numbers(calls) -> list[int]:
    return [int(body.split("<PageNumber>")[1].split("<")[0]) for _, body in calls]


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.mark.parametrize("failures", [
    ["ebay-10007"],
    ["ebay-10007-plus-warning"],
    ["http-500"],
    ["http-503"],
    ["connection"],
    ["read-timeout"],
    ["dropped-body"],
    ["ebay-10007", "connection"],
])
def test_get_active_listings_retries_a_transient_failure_then_succeeds(captured_posts, sleeps, failures):
    calls, queue = captured_posts
    queue.extend(TRANSIENT_FAILURES[kind]() for kind in failures)
    queue.append(seller_list_xml(item_xml(item_id="1") + item_xml(item_id="2"), pages=1))

    listings = ebay_api.get_active_listings("tok", account="AccountA", sleep=sleeps.append)

    assert [i["item_number"] for i in listings] == ["1", "2"]
    assert page_numbers(calls) == [1] * (len(failures) + 1)
    assert sleeps == list(ebay_api.TRADING_RETRY_DELAYS[:len(failures)])


@pytest.mark.parametrize("kind,error,match", [
    ("ebay-10007", RuntimeError, r"^GetSellerList failed on page 1: \['10007: System error"),
    ("http-500", requests.HTTPError, "500"),
    ("connection", requests.ConnectionError, "connection reset"),
    ("read-timeout", requests.Timeout, "read timed out"),
])
def test_get_active_listings_gives_up_after_three_transient_failures(
        captured_posts, sleeps, caplog, kind, error, match):
    calls, queue = captured_posts
    queue.extend(TRANSIENT_FAILURES[kind]() for _ in range(3))

    with caplog.at_level("WARNING", logger=ebay_api.log.name):
        with pytest.raises(error, match=match):
            ebay_api.get_active_listings("tok", account="AccountA", sleep=sleeps.append)

    assert len(calls) == 3
    assert sleeps == [5.0, 15.0]
    retries = [r.getMessage() for r in caplog.records if "failed transiently" in r.getMessage()]
    assert [("attempt 1/3" in m, "attempt 2/3" in m) for m in retries] == [(True, False), (False, True)]


def test_get_active_listings_permanent_failure_after_a_transient_one_stops_there(captured_posts, sleeps):
    calls, queue = captured_posts
    queue.append(transient_ack())
    queue.append(seller_list_xml(ack="Failure", errors=error_xml("931", "Auth token is invalid.")))
    queue.append(seller_list_xml(item_xml()))  # would succeed if (wrongly) retried

    with pytest.raises(RuntimeError) as excinfo:
        ebay_api.get_active_listings("tok", account="AccountA", sleep=sleeps.append)

    assert str(excinfo.value) == "GetSellerList failed on page 1: ['931: Auth token is invalid.']"
    assert len(calls) == 2
    assert sleeps == [5.0]


# The crash mail carries traceback.format_exc(), not the log, so the causes of the
# earlier attempts must ride on the exception itself. add_note is 3.11+, and CI
# still runs 3.10, where the log is the only record.
@pytest.mark.skipif(sys.version_info < (3, 11), reason="exception notes need Python 3.11")
@pytest.mark.parametrize("queued,raised,expected_in_note", [
    pytest.param([transient_ack, transient_ack, transient_ack], RuntimeError,
                 ["attempt 1/3: 10007", "attempt 2/3: 10007"], id="three-10007"),
    pytest.param([lambda: http_error(500), lambda: requests.ConnectionError("reset"),
                  lambda: requests.ReadTimeout("read timed out")], requests.Timeout,
                 ["attempt 1/3: HTTPError", "attempt 2/3: ConnectionError"], id="mixed-transport"),
    pytest.param([transient_ack,
                  lambda: seller_list_xml(ack="Failure", errors=error_xml("931", "Auth token is invalid."))],
                 RuntimeError, ["attempt 1/3: 10007"], id="transient-then-permanent"),
])
def test_get_active_listings_final_exception_carries_earlier_attempts(
        captured_posts, sleeps, queued, raised, expected_in_note):
    calls, queue = captured_posts
    queue.extend(make() for make in queued)

    with pytest.raises(raised) as excinfo:
        ebay_api.get_active_listings("tok", account="AccountA", sleep=sleeps.append)

    notes = excinfo.value.__notes__
    assert len(notes) == 1
    assert all(f in notes[0] for f in ["page 1", "AccountA", *expected_in_note])
    assert f"attempt {len(queued)}/3" not in notes[0]
    assert all(f in "".join(traceback.format_exception(excinfo.value)) for f in expected_in_note)


@pytest.mark.parametrize("response", [
    pytest.param(lambda: seller_list_xml(ack="Failure", errors=error_xml()), id="invalid-token-21917053"),
    pytest.param(lambda: seller_list_xml(ack="Failure", errors=error_xml("931", "Auth token is invalid.")),
                 id="auth-931"),
    pytest.param(lambda: seller_list_xml(ack="Failure", errors=error_xml("518", "Call usage limit reached.")),
                 id="call-limit-518"),
    pytest.param(lambda: seller_list_xml(ack="Failure", errors=error_xml("10007", SYSTEM_ERROR, "Error")
                                         + error_xml("37", "Input data is invalid.", "Error")),
                 id="10007-alongside-a-request-error"),
    pytest.param(lambda: seller_list_xml(ack="Failure", errors=error_xml("10007", SYSTEM_ERROR, "Warning")),
                 id="10007-as-warning-only"),
    pytest.param(lambda: seller_list_xml(ack="Failure"), id="failure-with-no-errors"),
    pytest.param(lambda: seller_list_xml(ack="PartialFailure", errors=error_xml()), id="partial-failure"),
    pytest.param(lambda: http_error(400), id="http-400"),
    pytest.param(lambda: http_error(401), id="http-401"),
    pytest.param(lambda: http_error(403), id="http-403"),
    pytest.param(lambda: http_error(404), id="http-404"),
    pytest.param(lambda: http_error(429), id="http-429"),
    pytest.param(lambda: requests.exceptions.InvalidURL("bad url"), id="invalid-url"),
])
def test_get_active_listings_never_retries_a_permanent_failure(captured_posts, sleeps, caplog, response):
    calls, queue = captured_posts
    queue.append(response())
    queue.append(seller_list_xml(item_xml()))  # would succeed if (wrongly) retried

    with caplog.at_level("WARNING", logger=ebay_api.log.name):
        with pytest.raises((RuntimeError, requests.RequestException)) as excinfo:
            ebay_api.get_active_listings("tok", account="AccountA", sleep=sleeps.append)

    assert len(calls) == 1
    assert sleeps == []
    assert not [r for r in caplog.records if "failed transiently" in r.getMessage()]
    assert not getattr(excinfo.value, "__notes__", [])


def test_get_active_listings_permanent_failure_message_is_unchanged(captured_posts, sleeps):
    calls, queue = captured_posts
    queue.append(seller_list_xml(ack="Failure", errors=error_xml()))
    with pytest.raises(RuntimeError) as excinfo:
        ebay_api.get_active_listings("tok", sleep=sleeps.append)
    assert str(excinfo.value) == "GetSellerList failed on page 1: ['21917053: Invalid token.']"


def test_get_active_listings_mid_sweep_retry_keeps_page_order(captured_posts, sleeps, caplog):
    calls, queue = captured_posts
    queue.append(seller_list_xml(item_xml(item_id="11") + item_xml(item_id="12"), pages=3))
    queue.append(transient_ack())
    queue.append(http_error(502))
    queue.append(seller_list_xml(item_xml(item_id="21") + item_xml(item_id="22"), pages=3))
    queue.append(seller_list_xml(item_xml(item_id="31"), pages=3))

    with caplog.at_level("WARNING", logger=ebay_api.log.name):
        listings = ebay_api.get_active_listings("tok", account="AccountA", sleep=sleeps.append)

    ids = [i["item_number"] for i in listings]
    assert ids == ["11", "12", "21", "22", "31"]
    assert len(ids) == len(set(ids))
    assert page_numbers(calls) == [1, 2, 2, 2, 3]
    assert sleeps == [5.0, 15.0]

    retries = [r for r in caplog.records if "failed transiently" in r.getMessage()]
    assert [r.levelname for r in retries] == ["WARNING", "WARNING"]
    first, second = (r.getMessage() for r in retries)
    for fragment in ("page 2", "AccountA", "attempt 1/3", "10007", "Please try again later"):
        assert fragment in first
    for fragment in ("page 2", "AccountA", "attempt 2/3", "HTTPError", "502"):
        assert fragment in second


def test_get_active_listings_retry_warning_never_carries_the_token(captured_posts, sleeps, caplog):
    calls, queue = captured_posts
    queue.append(transient_ack())
    queue.append(seller_list_xml(item_xml()))
    with caplog.at_level("WARNING", logger=ebay_api.log.name):
        ebay_api.get_active_listings("fake-user-token", sleep=sleeps.append)
    assert "fake-user-token" not in caplog.text
    assert "an unnamed account" in caplog.text


def test_get_active_listings_page_retry_is_the_only_retry_layer(monkeypatch, sleeps):
    # _post has its own transport retry; left on, a page would get 3 x 3 attempts.
    seen: list[dict] = []

    def fake_post(call_name, body, **kwargs):
        seen.append(kwargs)
        return seller_list_xml(item_xml()).encode("utf-8")

    monkeypatch.setattr(ebay_api, "_post", fake_post)
    ebay_api.get_active_listings("tok", sleep=sleeps.append)
    assert seen == [{"attempts": 1}]


def test_get_active_listings_defaults_to_time_sleep(captured_posts, monkeypatch):
    calls, queue = captured_posts
    slept: list[float] = []
    monkeypatch.setattr(ebay_api.time, "sleep", slept.append)
    queue.append(transient_ack())
    queue.append(seller_list_xml(item_xml()))
    ebay_api.get_active_listings("tok")
    assert slept == [5.0]


# --- transient classification ------------------------------------------------

@pytest.mark.parametrize("codes,expected", [
    (["10007"], True),
    (["10007", "10007"], True),
    ([], False),
    (["21917053"], False),
    (["10007", "21917053"], False),
    (["518"], False),
    ([""], False),
])
def test_is_transient_trading_failure(codes, expected):
    assert ebay_api.is_transient_trading_failure(codes) is expected


@pytest.mark.parametrize("exc,expected", [
    (http_error(500), True),
    (http_error(502), True),
    (http_error(503), True),
    (http_error(504), True),
    (http_error(400), False),
    (http_error(401), False),
    (http_error(403), False),
    (http_error(404), False),
    (http_error(429), False),
    (requests.HTTPError("no response attached"), False),
    (requests.ConnectionError("reset"), True),
    (requests.ConnectTimeout("connect timed out"), True),
    (requests.ReadTimeout("read timed out"), True),
    (requests.exceptions.ChunkedEncodingError("broken"), True),
    (requests.exceptions.InvalidURL("bad"), False),
    (requests.exceptions.TooManyRedirects("loop"), False),
    (requests.RequestException("generic"), False),
])
def test_is_transient_transport_error(exc, expected):
    assert ebay_api.is_transient_transport_error(exc) is expected


def test_parse_seller_list_error_codes_skip_warnings():
    xml = seller_list_xml(ack="Failure", errors=(
        error_xml("10007", SYSTEM_ERROR, "Error")
        + error_xml("21917062", "Heads up.", "Warning")
        + error_xml("37", "Input data is invalid.")
    ))
    result = ebay_api.parse_seller_list(xml)
    assert result["error_codes"] == ["10007", "37"]
    assert len(result["errors"]) == 3


# --- count_active_listings ---------------------------------------------------

def active_count_xml(total: int = 12254, ack: str = "Success", errors: str = "") -> str:
    """Build a namespaced GetMyeBaySellingResponse carrying only the active count."""
    active = (
        "<ActiveList><PaginationResult>"
        f"<TotalNumberOfEntries>{total}</TotalNumberOfEntries>"
        "</PaginationResult></ActiveList>"
    ) if ack in ("Success", "Warning") else ""
    return f'<GetMyeBaySellingResponse xmlns="{NS}"><Ack>{ack}</Ack>{errors}{active}</GetMyeBaySellingResponse>'


def count_transient_ack() -> str:
    return active_count_xml(ack="Failure", errors=error_xml("10007", SYSTEM_ERROR, "Error"))


# The failures the count must absorb, shaped as GetMyeBaySelling returns them.
COUNT_TRANSIENT_FAILURES = {
    "ebay-10007": count_transient_ack,
    "http-500": lambda: http_error(500),
    "http-503": lambda: http_error(503),
    "connection": lambda: requests.ConnectionError("connection reset"),
    "read-timeout": lambda: requests.ReadTimeout("read timed out"),
    "dropped-body": lambda: requests.exceptions.ChunkedEncodingError("connection broken"),
}


def test_count_active_listings_returns_the_total(captured_posts):
    calls, queue = captured_posts
    queue.append(active_count_xml(12254))
    assert ebay_api.count_active_listings("tok") == 12254
    assert [name for name, _ in calls] == ["GetMyeBaySelling"]


def test_count_active_listings_raises_on_a_failure_ack(captured_posts):
    calls, queue = captured_posts
    queue.append(active_count_xml(ack="Failure", errors=error_xml()))
    with pytest.raises(RuntimeError, match="Invalid token"):
        ebay_api.count_active_listings("tok")


def test_count_active_listings_keeps_its_positional_signature():
    """ebay-items-categories calls `count_active_listings(token)`; the new knobs are keyword-only."""
    params = inspect.signature(ebay_api.count_active_listings).parameters
    assert list(params) == ["token", "account", "sleep"]
    assert params["account"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["sleep"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["account"].default is None and params["sleep"].default is None


def test_parse_active_count_error_codes_skip_warnings():
    xml = active_count_xml(ack="Failure", errors=(
        error_xml("10007", SYSTEM_ERROR, "Error") + error_xml("21917062", "Heads up.", "Warning")
    ))
    result = ebay_api.parse_active_count(xml)
    assert result["error_codes"] == ["10007"]
    assert len(result["errors"]) == 2


@pytest.mark.parametrize("failures", [
    ["ebay-10007"],
    ["http-500"],
    ["http-503"],
    ["connection"],
    ["read-timeout"],
    ["dropped-body"],
    ["ebay-10007", "connection"],
])
def test_count_active_listings_retries_a_transient_failure_then_succeeds(captured_posts, sleeps, failures):
    calls, queue = captured_posts
    queue.extend(COUNT_TRANSIENT_FAILURES[kind]() for kind in failures)
    queue.append(active_count_xml(4321))

    assert ebay_api.count_active_listings("tok", account="AccountA", sleep=sleeps.append) == 4321
    assert len(calls) == len(failures) + 1
    assert sleeps == list(ebay_api.TRADING_RETRY_DELAYS[:len(failures)])


def test_count_active_listings_gives_up_with_the_2026_09_05_message(captured_posts, sleeps, caplog):
    """Three 10007s in a row still crash, with the exact message production logged that night."""
    calls, queue = captured_posts
    queue.extend(count_transient_ack() for _ in range(3))

    with caplog.at_level("WARNING", logger=ebay_api.log.name):
        with pytest.raises(RuntimeError) as excinfo:
            ebay_api.count_active_listings("tok", account="AccountA", sleep=sleeps.append)

    assert str(excinfo.value) == f"GetMyeBaySelling failed: ['10007: {SYSTEM_ERROR}']"
    assert len(calls) == 3
    assert sleeps == [5.0, 15.0]
    retries = [r for r in caplog.records if "failed transiently" in r.getMessage()]
    assert [r.levelname for r in retries] == ["WARNING", "WARNING"]
    for record, attempt in zip(retries, ("attempt 1/3", "attempt 2/3")):
        message = record.getMessage()
        assert all(f in message for f in ("GetMyeBaySelling for AccountA", attempt, "10007", "try again later"))
        assert "page" not in message


@pytest.mark.parametrize("kind,error,match", [
    ("http-500", requests.HTTPError, "500"),
    ("connection", requests.ConnectionError, "connection reset"),
    ("read-timeout", requests.Timeout, "read timed out"),
])
def test_count_active_listings_transport_give_up_raises_the_last_error(captured_posts, sleeps, kind, error, match):
    calls, queue = captured_posts
    queue.extend(COUNT_TRANSIENT_FAILURES[kind]() for _ in range(3))
    with pytest.raises(error, match=match):
        ebay_api.count_active_listings("tok", account="AccountA", sleep=sleeps.append)
    assert len(calls) == 3


@pytest.mark.parametrize("response", [
    pytest.param(lambda: active_count_xml(ack="Failure", errors=error_xml()), id="invalid-token-21917053"),
    pytest.param(lambda: active_count_xml(ack="Failure", errors=error_xml("931", "Auth token is invalid.")),
                 id="auth-931"),
    pytest.param(lambda: active_count_xml(ack="Failure", errors=error_xml("518", "Call usage limit reached.")),
                 id="call-limit-518"),
    pytest.param(lambda: active_count_xml(ack="Failure", errors=error_xml("10007", SYSTEM_ERROR, "Error")
                                          + error_xml("37", "Input data is invalid.", "Error")),
                 id="10007-alongside-a-request-error"),
    pytest.param(lambda: active_count_xml(ack="Failure", errors=error_xml("10007", SYSTEM_ERROR, "Warning")),
                 id="10007-as-warning-only"),
    pytest.param(lambda: active_count_xml(ack="Failure"), id="failure-with-no-errors"),
    pytest.param(lambda: http_error(400), id="http-400"),
    pytest.param(lambda: http_error(401), id="http-401"),
    pytest.param(lambda: http_error(429), id="http-429"),
    pytest.param(lambda: requests.exceptions.InvalidURL("bad url"), id="invalid-url"),
])
def test_count_active_listings_never_retries_a_permanent_failure(captured_posts, sleeps, caplog, response):
    calls, queue = captured_posts
    queue.append(response())
    queue.append(active_count_xml())  # would succeed if (wrongly) retried

    with caplog.at_level("WARNING", logger=ebay_api.log.name):
        with pytest.raises((RuntimeError, requests.RequestException)) as excinfo:
            ebay_api.count_active_listings("tok", account="AccountA", sleep=sleeps.append)

    assert len(calls) == 1
    assert sleeps == []
    assert not [r for r in caplog.records if "failed transiently" in r.getMessage()]
    assert not getattr(excinfo.value, "__notes__", [])


@pytest.mark.skipif(sys.version_info < (3, 11), reason="exception notes need Python 3.11")
def test_count_active_listings_final_exception_carries_earlier_attempts(captured_posts, sleeps):
    calls, queue = captured_posts
    queue.extend([count_transient_ack(), http_error(502), count_transient_ack()])
    with pytest.raises(RuntimeError) as excinfo:
        ebay_api.count_active_listings("tok", account="AccountA", sleep=sleeps.append)
    [note] = excinfo.value.__notes__
    assert note.startswith("GetMyeBaySelling for AccountA failed on earlier attempts: ")
    assert "attempt 1/3: 10007" in note and "attempt 2/3: HTTPError" in note
    assert "attempt 3/3" not in note


def test_count_active_listings_warning_never_carries_the_token(captured_posts, sleeps, caplog):
    calls, queue = captured_posts
    queue.extend([count_transient_ack(), active_count_xml()])
    with caplog.at_level("WARNING", logger=ebay_api.log.name):
        ebay_api.count_active_listings("fake-user-token", sleep=sleeps.append)
    assert "fake-user-token" not in caplog.text
    assert "an unnamed account" in caplog.text


def test_count_active_listings_is_the_only_retry_layer(monkeypatch, sleeps):
    seen: list[dict] = []

    def fake_post(call_name, body, **kwargs):
        seen.append(kwargs)
        return active_count_xml().encode("utf-8")

    monkeypatch.setattr(ebay_api, "_post", fake_post)
    ebay_api.count_active_listings("tok", sleep=sleeps.append)
    assert seen == [{"attempts": 1}]


def test_count_active_listings_defaults_to_time_sleep(captured_posts, monkeypatch):
    calls, queue = captured_posts
    slept: list[float] = []
    monkeypatch.setattr(ebay_api.time, "sleep", slept.append)
    queue.extend([count_transient_ack(), active_count_xml()])
    ebay_api.count_active_listings("tok")
    assert slept == [5.0]


# --- _post: the transport layer ----------------------------------------------

def http_response(status: int, body: bytes = b"<ok/>") -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = body
    response.url = ebay_api.TRADING_ENDPOINT
    return response


@pytest.fixture
def http_queue(monkeypatch):
    """Replace `requests.post` under the real `_post`, serving queued responses or raising queued exceptions."""
    sent: list[str] = []
    queue: list[requests.Response | Exception] = []

    def fake_requests_post(url, data=None, headers=None, timeout=None):
        sent.append(headers["X-EBAY-API-CALL-NAME"])
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(ebay_api.requests, "post", fake_requests_post)
    monkeypatch.setattr(ebay_api, "trading_headers", lambda call_name: {"X-EBAY-API-CALL-NAME": call_name})
    return sent, queue


@pytest.mark.parametrize("failure", [
    pytest.param(lambda: http_response(400), id="http-400"),
    pytest.param(lambda: http_response(401), id="http-401"),
    pytest.param(lambda: http_response(403), id="http-403"),
    pytest.param(lambda: http_response(404), id="http-404"),
    pytest.param(lambda: http_response(429), id="http-429"),
    pytest.param(lambda: requests.exceptions.InvalidURL("bad url"), id="invalid-url"),
    pytest.param(lambda: requests.exceptions.TooManyRedirects("loop"), id="redirect-loop"),
])
@pytest.mark.parametrize("call_name", ["GetItem", "GetMyeBaySelling"])
def test_post_raises_a_permanent_failure_on_the_first_attempt(http_queue, caplog, failure, call_name):
    sent, queue = http_queue
    queue.extend([failure(), http_response(200)])  # the 200 is reached only by a wrong retry
    with caplog.at_level("WARNING", logger=ebay_api.log.name):
        with pytest.raises(requests.RequestException):
            ebay_api._post(call_name, "<x/>")
    assert sent == [call_name]
    assert "Retrying" not in caplog.text


@pytest.mark.parametrize("failure,cause", [
    pytest.param(lambda: http_response(500), "HTTPError", id="http-500"),
    pytest.param(lambda: http_response(503), "HTTPError", id="http-503"),
    pytest.param(lambda: requests.ConnectionError("reset"), "ConnectionError", id="connection"),
    pytest.param(lambda: requests.ConnectTimeout("connect timed out"), "ConnectTimeout", id="connect-timeout"),
    pytest.param(lambda: requests.ReadTimeout("read timed out"), "ReadTimeout", id="read-timeout"),
    pytest.param(lambda: requests.exceptions.ChunkedEncodingError("broken"), "ChunkedEncodingError",
                 id="dropped-body"),
])
def test_post_retries_a_transient_failure(http_queue, caplog, failure, cause):
    sent, queue = http_queue
    queue.extend([failure(), http_response(200, b"<done/>")])
    with caplog.at_level("WARNING", logger=ebay_api.log.name):
        assert ebay_api._post("GetItem", "<x/>") == b"<done/>"
    assert sent == ["GetItem", "GetItem"]
    assert f"GetItem transport error (attempt 1/3): {cause}" in caplog.text


def test_post_gives_up_after_its_attempts(http_queue):
    sent, queue = http_queue
    queue.extend([http_response(502), requests.ConnectionError("reset"), http_response(503)])
    with pytest.raises(requests.HTTPError, match="503"):
        ebay_api._post("GetItem", "<x/>")
    assert len(sent) == 3


def test_post_permanent_failure_after_a_transient_one_stops_there(http_queue):
    sent, queue = http_queue
    queue.extend([http_response(500), http_response(401), http_response(200)])
    with pytest.raises(requests.HTTPError, match="401"):
        ebay_api._post("GetItem", "<x/>")
    assert len(sent) == 2


def test_post_with_one_attempt_never_retries(http_queue):
    sent, queue = http_queue
    queue.extend([http_response(500), http_response(200)])
    with pytest.raises(requests.HTTPError):
        ebay_api._post("GetSellerList", "<x/>", attempts=1)
    assert len(sent) == 1


def test_get_item_raises_a_401_after_one_request(http_queue):
    sent, queue = http_queue
    queue.extend([http_response(401), http_response(200)])
    with pytest.raises(requests.HTTPError, match="401"):
        ebay_api.get_item("tok", "123456789012")
    assert sent == ["GetItem"]


# The retry layers must not multiply: 3 HTTP requests per page or count, never 3 x 3.
def test_sweep_page_makes_three_http_requests_not_nine(http_queue, sleeps):
    sent, queue = http_queue
    queue.extend(http_response(500) for _ in range(9))
    with pytest.raises(requests.HTTPError):
        ebay_api.get_active_listings("tok", account="AccountA", sleep=sleeps.append)
    assert sent == ["GetSellerList"] * 3


def test_count_makes_three_http_requests_not_nine(http_queue, sleeps):
    sent, queue = http_queue
    queue.extend(http_response(500) for _ in range(9))
    with pytest.raises(requests.HTTPError):
        ebay_api.count_active_listings("tok", account="AccountA", sleep=sleeps.append)
    assert sent == ["GetMyeBaySelling"] * 3


# --- get_item ------------------------------------------------------------------

ITEM_ID = "123456789012"


def get_item_xml(item_id: str = ITEM_ID, ack: str = "Success", errors: str = "",
                 with_item: bool = True) -> str:
    """Build a namespaced GetItemResponse; a failure carries no <Item>, as eBay's does."""
    item = (
        f"<Item><ItemID>{item_id}</ItemID><Title>Acme AM-16 16 Channel Audio Monitor</Title>"
        "<SKU>ACM-AM16</SKU><Quantity>5</Quantity>"
        '<SellingStatus><CurrentPrice currencyID="USD">999.0</CurrentPrice>'
        "<QuantitySold>2</QuantitySold><ListingStatus>Active</ListingStatus></SellingStatus></Item>"
    ) if with_item else ""
    return f'<GetItemResponse xmlns="{NS}"><Ack>{ack}</Ack>{errors}{item}</GetItemResponse>'


def item_transient_ack() -> str:
    return get_item_xml(ack="Failure", errors=error_xml("10007", SYSTEM_ERROR, "Error"), with_item=False)


def item_failure(code: str, message: str) -> str:
    return get_item_xml(ack="Failure", errors=error_xml(code, message), with_item=False)


# The failures GetItem must absorb, shaped as it returns them.
ITEM_TRANSIENT_FAILURES = {
    "ebay-10007": item_transient_ack,
    "http-500": lambda: http_error(500),
    "http-503": lambda: http_error(503),
    "connection": lambda: requests.ConnectionError("connection reset"),
    "read-timeout": lambda: requests.ReadTimeout("read timed out"),
    "dropped-body": lambda: requests.exceptions.ChunkedEncodingError("connection broken"),
}


def test_get_item_keeps_its_positional_signature():
    """ebay-best-offers calls `get_item(token, item_id)`; the new knobs are keyword-only."""
    params = inspect.signature(ebay_api.get_item).parameters
    assert list(params) == ["token", "item_id", "account", "sleep"]
    assert params["token"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert params["item_id"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert params["account"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["sleep"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["account"].default is None and params["sleep"].default is None


def test_get_item_returns_the_parsed_item(captured_posts):
    calls, queue = captured_posts
    queue.append(get_item_xml())
    item = ebay_api.get_item("tok", ITEM_ID)
    assert item["item_number"] == ITEM_ID
    assert item["quantity_available"] == 3
    assert [name for name, _ in calls] == ["GetItem"]
    assert f"<ItemID>{ITEM_ID}</ItemID>" in calls[0][1]


@pytest.mark.parametrize("failures", [
    ["ebay-10007"],
    ["http-500"],
    ["http-503"],
    ["connection"],
    ["read-timeout"],
    ["dropped-body"],
    ["ebay-10007", "connection"],
])
def test_get_item_retries_a_transient_failure_then_succeeds(captured_posts, sleeps, caplog, failures):
    calls, queue = captured_posts
    queue.extend(ITEM_TRANSIENT_FAILURES[kind]() for kind in failures)
    queue.append(get_item_xml())

    with caplog.at_level("WARNING", logger=ebay_api.log.name):
        item = ebay_api.get_item("tok", ITEM_ID, account="AccountA", sleep=sleeps.append)

    assert item["item_number"] == ITEM_ID
    assert len(calls) == len(failures) + 1
    assert sleeps == list(ebay_api.TRADING_RETRY_DELAYS[:len(failures)])
    retries = [r for r in caplog.records if "failed transiently" in r.getMessage()]
    assert [r.levelname for r in retries] == ["WARNING"] * len(failures)
    for record, attempt in zip(retries, ("attempt 1/3", "attempt 2/3")):
        assert all(f in record.getMessage() for f in (f"GetItem item {ITEM_ID} for AccountA", attempt))


@pytest.mark.parametrize("kind,error,match", [
    ("ebay-10007", RuntimeError, rf"^GetItem failed for {ITEM_ID}: \['10007: System error"),
    ("http-500", requests.HTTPError, "500"),
    ("connection", requests.ConnectionError, "connection reset"),
    ("read-timeout", requests.Timeout, "read timed out"),
])
def test_get_item_gives_up_after_three_transient_failures(captured_posts, sleeps, caplog, kind, error, match):
    calls, queue = captured_posts
    queue.extend(ITEM_TRANSIENT_FAILURES[kind]() for _ in range(3))
    queue.append(get_item_xml())  # reached only by a wrong fourth attempt

    with caplog.at_level("WARNING", logger=ebay_api.log.name):
        with pytest.raises(error, match=match):
            ebay_api.get_item("tok", ITEM_ID, account="AccountA", sleep=sleeps.append)

    assert len(calls) == 3
    assert sleeps == [5.0, 15.0]
    retries = [r.getMessage() for r in caplog.records if "failed transiently" in r.getMessage()]
    assert [("attempt 1/3" in m, "attempt 2/3" in m) for m in retries] == [(True, False), (False, True)]


# Pinned byte for byte: the text get_item raised before retries existed, which a
# caller or a crash-mail search may match on.
@pytest.mark.parametrize("queued,expected", [
    pytest.param([item_transient_ack] * 3, f"GetItem failed for {ITEM_ID}: ['10007: {SYSTEM_ERROR}']",
                 id="three-10007"),
    pytest.param([lambda: item_failure("931", "Auth token is invalid.")],
                 f"GetItem failed for {ITEM_ID}: ['931: Auth token is invalid.']", id="auth-931"),
    pytest.param([lambda: get_item_xml(ack="Failure", with_item=False)],
                 f"GetItem failed for {ITEM_ID}: Failure", id="failure-with-no-errors"),
    pytest.param([lambda: get_item_xml(with_item=False)],
                 f"GetItem returned no item for {ITEM_ID}.", id="success-without-item"),
])
def test_get_item_final_message_is_unchanged(captured_posts, sleeps, queued, expected):
    calls, queue = captured_posts
    queue.extend(make() for make in queued)
    with pytest.raises(RuntimeError) as excinfo:
        ebay_api.get_item("tok", ITEM_ID, account="AccountA", sleep=sleeps.append)
    assert str(excinfo.value) == expected
    assert len(calls) == len(queued)


@pytest.mark.parametrize("response", [
    pytest.param(lambda: item_failure("21917053", "Invalid token."), id="invalid-token-21917053"),
    pytest.param(lambda: item_failure("931", "Auth token is invalid."), id="auth-931"),
    pytest.param(lambda: item_failure("17", "This item cannot be accessed."), id="item-not-found-17"),
    pytest.param(lambda: item_failure("518", "Call usage limit reached."), id="call-limit-518"),
    pytest.param(lambda: get_item_xml(ack="Failure", with_item=False,
                                      errors=error_xml("10007", SYSTEM_ERROR, "Error")
                                      + error_xml("37", "Input data is invalid.", "Error")),
                 id="10007-alongside-a-request-error"),
    pytest.param(lambda: get_item_xml(ack="Failure", with_item=False,
                                      errors=error_xml("10007", SYSTEM_ERROR, "Warning")),
                 id="10007-as-warning-only"),
    pytest.param(lambda: get_item_xml(ack="Failure", with_item=False), id="failure-with-no-errors"),
    pytest.param(lambda: get_item_xml(with_item=False), id="success-without-item"),
    pytest.param(lambda: http_error(400), id="http-400"),
    pytest.param(lambda: http_error(401), id="http-401"),
    pytest.param(lambda: http_error(403), id="http-403"),
    pytest.param(lambda: http_error(404), id="http-404"),
    pytest.param(lambda: http_error(429), id="http-429"),
    pytest.param(lambda: requests.exceptions.InvalidURL("bad url"), id="invalid-url"),
])
def test_get_item_never_retries_a_permanent_failure(captured_posts, sleeps, caplog, response):
    calls, queue = captured_posts
    queue.append(response())
    queue.append(get_item_xml())  # would succeed if (wrongly) retried

    with caplog.at_level("WARNING", logger=ebay_api.log.name):
        with pytest.raises((RuntimeError, requests.RequestException)) as excinfo:
            ebay_api.get_item("tok", ITEM_ID, account="AccountA", sleep=sleeps.append)

    assert len(calls) == 1
    assert sleeps == []
    assert not [r for r in caplog.records if "failed transiently" in r.getMessage()]
    assert not getattr(excinfo.value, "__notes__", [])


@pytest.mark.skipif(sys.version_info < (3, 11), reason="exception notes need Python 3.11")
def test_get_item_final_exception_carries_earlier_attempts(captured_posts, sleeps):
    calls, queue = captured_posts
    queue.extend([item_transient_ack(), http_error(502), item_transient_ack()])
    with pytest.raises(RuntimeError) as excinfo:
        ebay_api.get_item("tok", ITEM_ID, account="AccountA", sleep=sleeps.append)
    [note] = excinfo.value.__notes__
    assert note.startswith(f"GetItem item {ITEM_ID} for AccountA failed on earlier attempts: ")
    assert "attempt 1/3: 10007" in note and "attempt 2/3: HTTPError" in note
    assert "attempt 3/3" not in note


def test_get_item_never_carries_the_token(captured_posts, sleeps, caplog):
    calls, queue = captured_posts
    queue.extend(item_transient_ack() for _ in range(3))
    with caplog.at_level("WARNING", logger=ebay_api.log.name):
        with pytest.raises(RuntimeError) as excinfo:
            ebay_api.get_item("fake-user-token", ITEM_ID, sleep=sleeps.append)
    rendered = "".join(traceback.format_exception_only(type(excinfo.value), excinfo.value))
    rendered += "".join(getattr(excinfo.value, "__notes__", []))
    assert "fake-user-token" not in caplog.text
    assert "fake-user-token" not in rendered
    assert ITEM_ID in caplog.text
    assert "an unnamed account" in caplog.text


def test_get_item_is_the_only_retry_layer(monkeypatch, sleeps):
    seen: list[dict] = []

    def fake_post(call_name, body, **kwargs):
        seen.append(kwargs)
        return get_item_xml().encode("utf-8")

    monkeypatch.setattr(ebay_api, "_post", fake_post)
    ebay_api.get_item("tok", ITEM_ID, sleep=sleeps.append)
    assert seen == [{"attempts": 1}]


def test_get_item_defaults_to_time_sleep(captured_posts, monkeypatch):
    calls, queue = captured_posts
    slept: list[float] = []
    monkeypatch.setattr(ebay_api.time, "sleep", slept.append)
    queue.extend([item_transient_ack(), get_item_xml()])
    ebay_api.get_item("tok", ITEM_ID)
    assert slept == [5.0]


def test_get_item_makes_three_http_requests_not_nine(http_queue, sleeps):
    sent, queue = http_queue
    queue.extend(http_response(500) for _ in range(9))
    with pytest.raises(requests.HTTPError):
        ebay_api.get_item("tok", ITEM_ID, account="AccountA", sleep=sleeps.append)
    assert sent == ["GetItem"] * 3


def test_parse_item_error_codes_skip_warnings():
    xml = get_item_xml(ack="Failure", with_item=False, errors=(
        error_xml("10007", SYSTEM_ERROR, "Error") + error_xml("21917062", "Heads up.", "Warning")
    ))
    result = ebay_api.parse_item(xml)
    assert result["error_codes"] == ["10007"]
    assert len(result["errors"]) == 2
    assert result["item"] is None


def test_call_with_retry_refuses_both_page_and_item_id(captured_posts, sleeps):
    calls, queue = captured_posts
    with pytest.raises(ValueError, match="not both"):
        ebay_api._call_with_retry("GetItem", "<x/>", ebay_api.parse_item, "AccountA", sleeps.append,
                                  page=1, item_id=ITEM_ID)
    assert calls == []
