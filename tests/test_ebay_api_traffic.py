"""Unit tests for the OAuth / Analytics half of `ebay_api`.

No network: `requests` is replaced per test. The cases that matter most are the
two that would corrupt a report silently — reading the wrong metric, and losing
listings that have no traffic.
"""
from __future__ import annotations

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
    # 92089 views instead of 1672.
    payload = report(["LISTING_IMPRESSION_TOTAL", "LISTING_VIEWS_TOTAL"],
                     [("111", [92089, 1672])])
    assert ebay_api.parse_traffic_report(payload) == {"111": 1672}


def test_views_are_found_when_the_metric_comes_first():
    payload = report(["LISTING_VIEWS_TOTAL", "LISTING_IMPRESSION_TOTAL"],
                     [("111", [1672, 92089])])
    assert ebay_api.parse_traffic_report(payload) == {"111": 1672}


def test_an_absent_metric_raises_rather_than_guessing():
    payload = report(["LISTING_IMPRESSION_TOTAL"], [("111", [92089])])
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
