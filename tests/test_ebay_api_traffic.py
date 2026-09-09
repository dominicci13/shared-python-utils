"""Unit tests for the OAuth / Analytics half of `ebay_api`.

No network: `requests` is replaced per test. The cases that matter most are the
two that would corrupt a report silently — reading the wrong metric, and losing
listings that have no traffic.
"""
from __future__ import annotations

import logging

import pytest

from seller_automation_utils import ebay_api


def report(metrics: list[str], records: list[tuple[str, list[int]]]) -> dict:
    """Build a getTrafficReport payload in eBay's real response shape."""
    return {
        "header": {"dimensionKeys": [{"key": "LISTING"}],
                   "metrics": [{"key": m} for m in metrics]},
        "records": [
            {"dimensionValues": [{"value": listing_id, "applicable": True}],
             "metricValues": [{"value": v, "applicable": True} for v in values]}
            for listing_id, values in records
        ],
    }


@pytest.fixture(autouse=True)
def clear_token_cache():
    ebay_api._access_tokens.clear()
    yield
    ebay_api._access_tokens.clear()


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("EBAY_APP_ID", "app")
    monkeypatch.setenv("EBAY_CERT_ID", "cert")
    monkeypatch.setenv("EBAY_OAUTH_REFRESH_TOKEN_ACCOUNTA", "refresh-token")


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload, self.status_code, self.text = payload, status, str(payload)

    def json(self) -> dict:
        return self._payload


# --- naming ------------------------------------------------------------------

@pytest.mark.parametrize("account,expected", [
    ("AccountA", "EBAY_OAUTH_REFRESH_TOKEN_ACCOUNTA"),
    ("AccountB", "EBAY_OAUTH_REFRESH_TOKEN_ACCOUNTB"),
])
def test_oauth_refresh_env_var(account, expected):
    assert ebay_api.oauth_refresh_env_var(account) == expected


def test_oauth_var_is_distinct_from_the_trading_token_var():
    # The two credentials are not interchangeable; sharing a name would let one
    # silently stand in for the other.
    assert ebay_api.oauth_refresh_env_var("X") != ebay_api.token_env_var("X")


def test_missing_refresh_token_names_the_variable(monkeypatch):
    monkeypatch.delenv("EBAY_OAUTH_REFRESH_TOKEN_ACCOUNTA", raising=False)
    with pytest.raises(RuntimeError, match="EBAY_OAUTH_REFRESH_TOKEN_ACCOUNTA"):
        ebay_api.oauth_access_token("AccountA")


# --- metric extraction -------------------------------------------------------

def test_views_are_read_by_header_position_not_blindly():
    # Impressions come first in the response; taking metricValues[0] would report
    # 40000 views instead of 900.
    payload = report(["LISTING_IMPRESSION_TOTAL", "LISTING_VIEWS_TOTAL"],
                     [("111", [40000, 900])])
    assert ebay_api.parse_traffic_report(payload) == {"111": 900}


def test_views_are_found_when_the_metric_comes_first():
    payload = report(["LISTING_VIEWS_TOTAL", "LISTING_IMPRESSION_TOTAL"],
                     [("111", [900, 40000])])
    assert ebay_api.parse_traffic_report(payload) == {"111": 900}


def test_an_absent_metric_raises_rather_than_guessing():
    payload = report(["LISTING_IMPRESSION_TOTAL"], [("111", [40000])])
    with pytest.raises(RuntimeError, match="LISTING_VIEWS_TOTAL"):
        ebay_api.parse_traffic_report(payload)


def test_a_null_metric_value_counts_as_zero():
    payload = report(["LISTING_VIEWS_TOTAL"], [("111", [None])])
    assert ebay_api.parse_traffic_report(payload) == {"111": 0}


def test_an_empty_report_is_not_an_error():
    assert ebay_api.parse_traffic_report(report(["LISTING_VIEWS_TOTAL"], [])) == {}


# --- batching and zero-filling ----------------------------------------------

def test_listings_with_no_traffic_come_back_as_zero(creds, monkeypatch):
    # eBay omits them entirely; without the zero-fill they would land as NULL.
    monkeypatch.setattr(ebay_api, "oauth_access_token", lambda *a, **k: "tok")
    monkeypatch.setattr(ebay_api.requests, "get",
                        lambda *a, **k: FakeResponse(report(["LISTING_VIEWS_TOTAL"], [("111", [5])])))
    assert ebay_api.get_listing_views("AccountA", ["111", "222"]) == {"111": 5, "222": 0}


def test_requests_are_split_into_batches_of_200(creds, monkeypatch):
    monkeypatch.setattr(ebay_api, "oauth_access_token", lambda *a, **k: "tok")
    calls: list[int] = []

    def fake_get(url, headers=None, params=None, timeout=None):
        ids = params["filter"].split("listing_ids:{")[1].rstrip("}").split("|")
        calls.append(len(ids))
        return FakeResponse(report(["LISTING_VIEWS_TOTAL"], [(i, [1]) for i in ids]))

    monkeypatch.setattr(ebay_api.requests, "get", fake_get)
    result = ebay_api.get_listing_views("AccountA", [str(n) for n in range(450)])
    assert calls == [200, 200, 50]
    assert len(result) == 450


def test_no_listings_makes_no_calls(creds, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("should not have called eBay")

    monkeypatch.setattr(ebay_api.requests, "get", explode)
    assert ebay_api.get_listing_views("AccountA", []) == {}


def test_a_429_explains_the_daily_quota_rather_than_looking_transient(creds, monkeypatch):
    # A bare "429 Too Many Requests" reads as something a retry would fix. This
    # one is a daily budget, so the message has to say so.
    monkeypatch.setattr(ebay_api, "oauth_access_token", lambda *a, **k: "tok")
    monkeypatch.setattr(ebay_api.requests, "get",
                        lambda *a, **k: FakeResponse({"errors": [{"errorId": 2001}]}, status=429))
    with pytest.raises(RuntimeError, match="daily call limit"):
        ebay_api.get_listing_views("AccountA", ["111"])


def test_an_error_response_raises_with_ebays_text(creds, monkeypatch):
    monkeypatch.setattr(ebay_api, "oauth_access_token", lambda *a, **k: "tok")
    monkeypatch.setattr(ebay_api.requests, "get",
                        lambda *a, **k: FakeResponse({"errors": [{"errorId": 50028}]}, status=400))
    with pytest.raises(RuntimeError, match="50028"):
        ebay_api.get_listing_views("AccountA", ["111"])


def test_the_requested_window_ends_yesterday(creds, monkeypatch):
    monkeypatch.setattr(ebay_api, "oauth_access_token", lambda *a, **k: "tok")
    seen = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        seen["filter"] = params["filter"]
        return FakeResponse(report(["LISTING_VIEWS_TOTAL"], []))

    monkeypatch.setattr(ebay_api.requests, "get", fake_get)
    ebay_api.get_listing_views("AccountA", ["111"], days=30)
    window = seen["filter"].split("date_range:[")[1].split("]")[0]
    start, end = window.split("..")
    assert len(start) == len(end) == 8
    assert start < end


# --- id validation -----------------------------------------------------------

@pytest.mark.parametrize("bad_id", [
    "123456789012.0",
    123456789012.0,
    "abc",
    "",
    "   ",
    "123|456",
    "12}34",
    "123-456",
])
def test_a_non_digit_listing_id_raises_before_any_request(bad_id, creds, monkeypatch):
    # eBay matches nothing against these, and the zero-fill would report the
    # whole batch as "no traffic" instead. `|` and `}` also close the filter
    # string early, so the same check is what keeps them out of the request.
    def explode(*args, **kwargs):
        raise AssertionError("should not have contacted eBay")

    monkeypatch.setattr(ebay_api.requests, "get", explode)
    monkeypatch.setattr(ebay_api.requests, "post", explode)
    with pytest.raises(ValueError, match="not digit strings"):
        ebay_api.get_listing_views("AccountA", ["111", bad_id, "222"])


def test_the_validation_error_names_the_offenders_and_the_count(creds, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("should not have contacted eBay")

    monkeypatch.setattr(ebay_api.requests, "get", explode)
    monkeypatch.setattr(ebay_api.requests, "post", explode)
    with pytest.raises(ValueError) as excinfo:
        ebay_api.get_listing_views("AccountA", ["111", "123456789012.0", "abc"])

    message = str(excinfo.value)
    assert "AccountA" in message
    assert "2 listing id(s)" in message
    assert "123456789012.0" in message and "abc" in message


@pytest.mark.parametrize("days", [0, -1])
def test_a_window_shorter_than_a_day_raises_before_any_request(creds, monkeypatch, days):
    # days=0 builds an inverted window (start after end), which eBay answers with
    # no records — indistinguishable from a legitimately quiet account.
    def explode(*args, **kwargs):
        raise AssertionError("should not have contacted eBay")

    monkeypatch.setattr(ebay_api.requests, "get", explode)
    monkeypatch.setattr(ebay_api.requests, "post", explode)
    with pytest.raises(ValueError, match="days >= 1"):
        ebay_api.get_listing_views("AccountA", ["111"], days=days)


def test_an_int_id_is_accepted_and_keyed_by_its_string_form(creds, monkeypatch):
    monkeypatch.setattr(ebay_api, "oauth_access_token", lambda *a, **k: "tok")
    monkeypatch.setattr(ebay_api.requests, "get",
                        lambda *a, **k: FakeResponse(
                            report(["LISTING_VIEWS_TOTAL"], [("111", [5])])))
    assert ebay_api.get_listing_views("AccountA", [111, " 111 "]) == {"111": 5}


# --- de-duplication ----------------------------------------------------------

def test_duplicate_ids_are_requested_once_and_returned_once(creds, monkeypatch):
    monkeypatch.setattr(ebay_api, "oauth_access_token", lambda *a, **k: "tok")
    sent: list[list[str]] = []

    def fake_get(url, headers=None, params=None, timeout=None):
        ids = params["filter"].split("listing_ids:{")[1].rstrip("}").split("|")
        sent.append(ids)
        return FakeResponse(report(["LISTING_VIEWS_TOTAL"], [("111", [5])]))

    monkeypatch.setattr(ebay_api.requests, "get", fake_get)
    result = ebay_api.get_listing_views("AccountA", ["111", "222", "111", " 222 "])
    assert sent == [["111", "222"]]
    assert result == {"111": 5, "222": 0}


def test_duplicates_do_not_buy_a_second_batch(creds, monkeypatch):
    # Undeduplicated, 300 ids spanning two batches cost two calls out of the
    # daily budget and still collapse to 150 entries on return.
    monkeypatch.setattr(ebay_api, "oauth_access_token", lambda *a, **k: "tok")
    calls: list[int] = []

    def fake_get(url, headers=None, params=None, timeout=None):
        ids = params["filter"].split("listing_ids:{")[1].rstrip("}").split("|")
        calls.append(len(ids))
        return FakeResponse(report(["LISTING_VIEWS_TOTAL"], [(i, [1]) for i in ids]))

    monkeypatch.setattr(ebay_api.requests, "get", fake_get)
    result = ebay_api.get_listing_views("AccountA", [str(n) for n in range(150)] * 2)
    assert calls == [150]
    assert len(result) == 150


# --- reporting what actually came back ---------------------------------------

def test_a_batch_with_no_records_warns_and_still_zero_fills(creds, monkeypatch, caplog):
    monkeypatch.setattr(ebay_api, "oauth_access_token", lambda *a, **k: "tok")
    monkeypatch.setattr(ebay_api.requests, "get",
                        lambda *a, **k: FakeResponse(report(["LISTING_VIEWS_TOTAL"], [])))
    with caplog.at_level(logging.WARNING, logger="seller_automation_utils.ebay_api"):
        result = ebay_api.get_listing_views("AccountA", ["111", "222"])

    assert result == {"111": 0, "222": 0}
    assert "no records" in caplog.text
    assert "AccountA" in caplog.text
    assert "0 views" in caplog.text


def test_a_batch_with_records_does_not_warn(creds, monkeypatch, caplog):
    monkeypatch.setattr(ebay_api, "oauth_access_token", lambda *a, **k: "tok")
    monkeypatch.setattr(ebay_api.requests, "get",
                        lambda *a, **k: FakeResponse(report(["LISTING_VIEWS_TOTAL"], [("111", [5])])))
    with caplog.at_level(logging.INFO, logger="seller_automation_utils.ebay_api"):
        assert ebay_api.get_listing_views("AccountA", ["111", "222"]) == {"111": 5, "222": 0}

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_the_progress_log_counts_records_returned_not_ids_requested(creds, monkeypatch, caplog):
    # The old line counted ids requested, so a batch eBay answered with nothing
    # still logged "3/3" while all three landed as 0 views.
    monkeypatch.setattr(ebay_api, "oauth_access_token", lambda *a, **k: "tok")
    monkeypatch.setattr(ebay_api.requests, "get",
                        lambda *a, **k: FakeResponse(report(["LISTING_VIEWS_TOTAL"], [("111", [5])])))
    with caplog.at_level(logging.INFO, logger="seller_automation_utils.ebay_api"):
        ebay_api.get_listing_views("AccountA", ["111", "222", "333"])

    assert "1/3" in caplog.text
    assert "3/3" not in caplog.text


def test_the_progress_log_accumulates_across_batches(creds, monkeypatch, caplog):
    monkeypatch.setattr(ebay_api, "oauth_access_token", lambda *a, **k: "tok")

    def fake_get(url, headers=None, params=None, timeout=None):
        first = params["filter"].split("listing_ids:{")[1].rstrip("}").split("|")[0]
        return FakeResponse(report(["LISTING_VIEWS_TOTAL"], [(first, [7])]))

    monkeypatch.setattr(ebay_api.requests, "get", fake_get)
    with caplog.at_level(logging.INFO, logger="seller_automation_utils.ebay_api"):
        ebay_api.get_listing_views("AccountA", [str(n) for n in range(250)])

    assert "1/250" in caplog.text
    assert "2/250" in caplog.text


# --- token caching -----------------------------------------------------------

def test_the_access_token_is_minted_once_and_reused(creds, monkeypatch):
    posts = []

    def fake_post(url, headers=None, data=None, timeout=None):
        posts.append(data)
        return FakeResponse({"access_token": "abc", "expires_in": 7200})

    monkeypatch.setattr(ebay_api.requests, "post", fake_post)
    assert ebay_api.oauth_access_token("AccountA") == "abc"
    assert ebay_api.oauth_access_token("AccountA") == "abc"
    assert len(posts) == 1
    assert posts[0]["grant_type"] == "refresh_token"


def test_a_refused_grant_raises(creds, monkeypatch):
    monkeypatch.setattr(ebay_api.requests, "post",
                        lambda *a, **k: FakeResponse({"error": "invalid_grant"}, status=400))
    with pytest.raises(RuntimeError, match="invalid_grant"):
        ebay_api.oauth_access_token("AccountA")
