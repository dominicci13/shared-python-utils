"""Unit tests for the Negotiation half of `ebay_api`.

No network: `requests` is replaced per test. The cases that matter most are the
ones that would quietly under-report the bucket — a short page skipping listings,
and a missing scope being read as "this account has none eligible".
"""
from __future__ import annotations

import pytest

from seller_automation_utils import ebay_api


def eligible(listing_ids: list[str], total: int | None = None) -> dict:
    """Build a find_eligible_items payload in eBay's real response shape."""
    return {
        "eligibleItems": [{"listingId": listing_id} for listing_id in listing_ids],
        "limit": ebay_api.MAX_ELIGIBLE_ITEMS_PER_PAGE,
        "offset": 0,
        "total": len(listing_ids) if total is None else total,
    }


@pytest.fixture(autouse=True)
def clear_token_cache():
    ebay_api._access_tokens.clear()
    yield
    ebay_api._access_tokens.clear()


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(ebay_api, "oauth_access_token", lambda *a, **k: "tok")


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload, self.status_code, self.text = payload, status, str(payload)

    def json(self) -> dict:
        return self._payload


# --- parsing -----------------------------------------------------------------

def test_parse_eligible_items_reads_the_listing_ids():
    assert ebay_api.parse_eligible_items(eligible(["111", "222"])) == ["111", "222"]


def test_parse_eligible_items_returns_ids_as_strings():
    # eBay has returned listingId as a JSON number; the frame keys on strings.
    payload = {"eligibleItems": [{"listingId": 111}]}
    assert ebay_api.parse_eligible_items(payload) == ["111"]


def test_parse_eligible_items_skips_an_entry_with_no_id():
    payload = {"eligibleItems": [{"listingId": "111"}, {"reason": "no id here"}]}
    assert ebay_api.parse_eligible_items(payload) == ["111"]


def test_an_account_with_nothing_eligible_is_not_an_error():
    assert ebay_api.parse_eligible_items({"total": 0}) == []


# --- paging ------------------------------------------------------------------

def test_a_single_page_makes_one_call(token, monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(params["offset"])
        return FakeResponse(eligible(["111", "222"]))

    monkeypatch.setattr(ebay_api.requests, "get", fake_get)
    assert ebay_api.get_offer_eligible_items("AccountA") == {"111", "222"}
    assert calls == [0]


def test_paging_walks_past_the_first_page(token, monkeypatch):
    pages = [eligible([str(n) for n in range(200)], total=250),
             eligible([str(n) for n in range(200, 250)], total=250)]
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(params["offset"])
        return FakeResponse(pages[len(calls) - 1])

    monkeypatch.setattr(ebay_api.requests, "get", fake_get)
    assert len(ebay_api.get_offer_eligible_items("AccountA")) == 250
    assert calls == [0, 200]


def test_the_offset_follows_what_came_back_not_the_page_size(token, monkeypatch):
    # A short page must not advance by 200 — doing so would skip 199 listings.
    pages = [eligible(["1"], total=2), eligible(["2"], total=2)]
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(params["offset"])
        return FakeResponse(pages[len(calls) - 1])

    monkeypatch.setattr(ebay_api.requests, "get", fake_get)
    assert ebay_api.get_offer_eligible_items("AccountA") == {"1", "2"}
    assert calls == [0, 1]


def test_an_empty_page_stops_the_walk(token, monkeypatch):
    # A total that overstates what eBay will actually return must not spin.
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(params["offset"])
        return FakeResponse(eligible([], total=500))

    monkeypatch.setattr(ebay_api.requests, "get", fake_get)
    assert ebay_api.get_offer_eligible_items("AccountA") == set()
    assert calls == [0]


def test_a_response_with_no_total_keeps_paging_while_pages_are_full(token, monkeypatch):
    # Reading a missing total as zero would stop after page 1 and under-report the
    # bucket silently, which is the one failure this whole path cannot afford.
    pages = [{"eligibleItems": [{"listingId": str(n)} for n in range(200)]},
             {"eligibleItems": [{"listingId": "200"}]}]
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(params["offset"])
        return FakeResponse(pages[len(calls) - 1])

    monkeypatch.setattr(ebay_api.requests, "get", fake_get)
    assert len(ebay_api.get_offer_eligible_items("AccountA")) == 201
    assert calls == [0, 200]


def test_a_short_page_with_no_total_ends_the_walk(token, monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(params["offset"])
        return FakeResponse({"eligibleItems": [{"listingId": "111"}]})

    monkeypatch.setattr(ebay_api.requests, "get", fake_get)
    assert ebay_api.get_offer_eligible_items("AccountA") == {"111"}
    assert calls == [0]


def test_a_runaway_page_loop_is_stopped(token, monkeypatch):
    monkeypatch.setattr(ebay_api, "_MAX_PAGES", 2)
    monkeypatch.setattr(ebay_api.requests, "get",
                        lambda *a, **k: FakeResponse(eligible(["111"], total=10_000)))
    with pytest.raises(RuntimeError, match="refusing to keep paging"):
        ebay_api.get_offer_eligible_items("AccountA")


# --- request shape and failures ----------------------------------------------

def test_the_call_is_scoped_to_a_marketplace(token, monkeypatch):
    seen = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        seen.update(headers)
        return FakeResponse(eligible([]))

    monkeypatch.setattr(ebay_api.requests, "get", fake_get)
    ebay_api.get_offer_eligible_items("AccountA")
    assert seen["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"


def test_the_token_is_minted_for_the_negotiation_scope(monkeypatch):
    # Asking with the Analytics default would be refused; the two scopes are
    # granted separately and a token only carries what it was consented for.
    scopes = []
    monkeypatch.setattr(ebay_api, "oauth_access_token",
                        lambda account, scope: scopes.append(scope) or "tok")
    monkeypatch.setattr(ebay_api.requests, "get", lambda *a, **k: FakeResponse(eligible([])))
    ebay_api.get_offer_eligible_items("AccountA")
    assert scopes == [ebay_api.NEGOTIATION_SCOPE]


def test_a_403_names_the_missing_scope_rather_than_the_run(token, monkeypatch):
    # Reported as "no eligible listings" this would look like a quiet business
    # fact instead of a keyset that lost its grant.
    monkeypatch.setattr(ebay_api.requests, "get",
                        lambda *a, **k: FakeResponse({"errors": [{"errorId": 1100}]}, status=403))
    with pytest.raises(RuntimeError, match="sell.negotiation"):
        ebay_api.get_offer_eligible_items("AccountA")


def test_an_error_response_raises_with_ebays_text(token, monkeypatch):
    monkeypatch.setattr(ebay_api.requests, "get",
                        lambda *a, **k: FakeResponse({"errors": [{"errorId": 195_001}]}, status=400))
    with pytest.raises(RuntimeError, match="195001"):
        ebay_api.get_offer_eligible_items("AccountA")
