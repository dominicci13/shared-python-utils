"""Unit tests for `ebay_api.get_item` and its parser.

`quantity_available` is the field with teeth: it replaces a signal the Seller Hub
grid used to give directly (eBay hiding the respond button), and it decides
whether an offer is answered at all.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

import pytest

from seller_automation_utils import ebay_api

NS = "urn:ebay:apis:eBLBaseComponents"


def item_xml(item_id: str = "123456789012", title: str = "A Camera & Lens",
             sku: str | None = "10000001_ABCD", price: str | None = "129.99",
             quantity: str | None = "7", sold: str | None = "7",
             status: str = "Active", ack: str = "Success", with_item: bool = True) -> str:
    parts = [f"<ItemID>{item_id}</ItemID>", f"<Title>{escape(title)}</Title>"]
    if sku is not None:
        parts.append(f"<SKU>{escape(sku)}</SKU>")
    if quantity is not None:
        parts.append(f"<Quantity>{quantity}</Quantity>")
    selling = [f"<ListingStatus>{status}</ListingStatus>"]
    if price is not None:
        selling.append(f'<CurrentPrice currencyID="USD">{price}</CurrentPrice>')
    if sold is not None:
        selling.append(f"<QuantitySold>{sold}</QuantitySold>")
    parts.append(f"<SellingStatus>{''.join(selling)}</SellingStatus>")
    body = f"<Item>{''.join(parts)}</Item>" if with_item else ""
    return f'<GetItemResponse xmlns="{NS}"><Ack>{ack}</Ack>{body}</GetItemResponse>'


# --- request building --------------------------------------------------------

def test_the_request_carries_the_item_and_escapes_the_token():
    body = ebay_api.build_get_item_xml("a&b", "123456789012")
    assert "<ItemID>123456789012</ItemID>" in body
    assert "a&amp;b" in body


def test_an_integer_item_id_is_accepted():
    assert "<ItemID>123</ItemID>" in ebay_api.build_get_item_xml("tok", 123)


# --- parsing -----------------------------------------------------------------

def test_every_needed_field_is_mapped():
    item = ebay_api.parse_item(item_xml())["item"]
    assert item["item_number"] == "123456789012"
    assert item["title"] == "A Camera & Lens"
    assert item["sku"] == "10000001_ABCD"
    assert item["current_price"] == 129.99
    assert item["listing_status"] == "Active"


def test_a_sold_out_listing_reports_zero_available_while_still_active():
    # The real case that drove this: qty 7, sold 7, still Active. eBay hides the
    # respond button here, which is what the old grid read as out of stock.
    item = ebay_api.parse_item(item_xml(quantity="7", sold="7"))["item"]
    assert item["quantity"] == 7
    assert item["quantity_sold"] == 7
    assert item["quantity_available"] == 0


def test_available_quantity_is_the_remainder():
    item = ebay_api.parse_item(item_xml(quantity="10", sold="3"))["item"]
    assert item["quantity_available"] == 7


def test_available_quantity_never_goes_negative():
    item = ebay_api.parse_item(item_xml(quantity="2", sold="5"))["item"]
    assert item["quantity_available"] == 0


def test_absent_quantities_read_as_zero_rather_than_raising():
    item = ebay_api.parse_item(item_xml(quantity=None, sold=None))["item"]
    assert (item["quantity"], item["quantity_sold"], item["quantity_available"]) == (0, 0, 0)


def test_a_missing_sku_is_none_not_empty_string():
    assert ebay_api.parse_item(item_xml(sku=None))["item"]["sku"] is None


def test_a_failure_ack_carries_the_errors():
    xml = (f'<GetItemResponse xmlns="{NS}"><Ack>Failure</Ack>'
           "<Errors><ErrorCode>17</ErrorCode><LongMessage>Item not found.</LongMessage></Errors>"
           "</GetItemResponse>")
    result = ebay_api.parse_item(xml)
    assert result["ack"] == "Failure"
    assert result["errors"] == ["17: Item not found."]
    assert result["item"] is None


# --- the call ----------------------------------------------------------------

def test_get_item_returns_the_parsed_item(monkeypatch):
    monkeypatch.setattr(ebay_api, "_post", lambda *a, **k: item_xml().encode("utf-8"))
    assert ebay_api.get_item("tok", "123456789012")["quantity_available"] == 0


def test_get_item_raises_on_a_failure_ack(monkeypatch):
    monkeypatch.setattr(ebay_api, "_post",
                        lambda *a, **k: item_xml(ack="Failure", with_item=False).encode("utf-8"))
    with pytest.raises(RuntimeError, match="GetItem failed"):
        ebay_api.get_item("tok", "1")


def test_get_item_raises_when_the_response_carries_no_item(monkeypatch):
    # A Success ack with no Item would otherwise return None and surface as an
    # AttributeError somewhere far from the cause.
    monkeypatch.setattr(ebay_api, "_post",
                        lambda *a, **k: item_xml(with_item=False).encode("utf-8"))
    with pytest.raises(RuntimeError, match="no item"):
        ebay_api.get_item("tok", "1")
