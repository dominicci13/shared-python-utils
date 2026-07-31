"""Unit tests for `database_utils._input_sizes`, the fast_executemany bind widths.

This helper is the reason the bulk insert is safe: fast_executemany otherwise sizes
each string parameter from the FIRST row, so a longer later value raises "String
data, right truncation". Getting a width wrong here silently corrupts data, so every
branch is covered against a fake cursor rather than a live database.
"""
from __future__ import annotations

import pyodbc
import pytest

from seller_automation_utils.database_utils import _input_sizes


class FakeColumn:
    """Mimics one row of `pyodbc.Cursor.columns()`."""

    def __init__(self, column_name, data_type, column_size=None, decimal_digits=None, type_name=""):
        self.column_name = column_name
        self.data_type = data_type
        self.column_size = column_size
        self.decimal_digits = decimal_digits
        self.type_name = type_name


class FakeCursor:
    def __init__(self, columns: list[FakeColumn]) -> None:
        self._columns = columns
        self.asked_table: str | None = None

    def columns(self, table: str):
        self.asked_table = table
        return self._columns


def sizes_for(cols: list[FakeColumn], names: list[str] | None = None):
    cur = FakeCursor(cols)
    # `names is None` rather than falsy: an explicitly empty list is a real case.
    return _input_sizes(cur, "AllItems", [c.column_name for c in cols] if names is None else names)


def test_string_column_pinned_to_declared_width():
    got = sizes_for([FakeColumn("SKU", pyodbc.SQL_VARCHAR, column_size=64)])
    assert got == [(pyodbc.SQL_WVARCHAR, 64, 0)]


@pytest.mark.parametrize(
    "sql_type",
    [pyodbc.SQL_CHAR, pyodbc.SQL_VARCHAR, pyodbc.SQL_LONGVARCHAR,
     pyodbc.SQL_WCHAR, pyodbc.SQL_WVARCHAR, pyodbc.SQL_WLONGVARCHAR],
)
def test_every_string_type_is_pinned(sql_type):
    got = sizes_for([FakeColumn("C", sql_type, column_size=10)])
    assert got == [(pyodbc.SQL_WVARCHAR, 10, 0)]


def test_wide_string_becomes_max():
    """Over 4000 means (n)varchar(max); pinning the real width would truncate."""
    got = sizes_for([FakeColumn("ItemDescription", pyodbc.SQL_WVARCHAR, column_size=8000)])
    assert got == [(pyodbc.SQL_WVARCHAR, 0, 0)]


@pytest.mark.parametrize("size", [0, None, -1])
def test_unknown_string_width_becomes_max(size):
    got = sizes_for([FakeColumn("Blob", pyodbc.SQL_WVARCHAR, column_size=size)])
    assert got == [(pyodbc.SQL_WVARCHAR, 0, 0)]


def test_boundary_4000_is_pinned_not_maxed():
    got = sizes_for([FakeColumn("C", pyodbc.SQL_WVARCHAR, column_size=4000)])
    assert got == [(pyodbc.SQL_WVARCHAR, 4000, 0)]


def test_decimal_keeps_precision_and_scale():
    got = sizes_for([FakeColumn("AmazonPrice", pyodbc.SQL_DECIMAL, column_size=19, decimal_digits=4)])
    assert got == [(pyodbc.SQL_DECIMAL, 19, 4)]


def test_numeric_treated_as_decimal():
    got = sizes_for([FakeColumn("N", pyodbc.SQL_NUMERIC, column_size=10, decimal_digits=2)])
    assert got == [(pyodbc.SQL_DECIMAL, 10, 2)]


def test_decimal_without_metadata_defaults():
    got = sizes_for([FakeColumn("D", pyodbc.SQL_DECIMAL, column_size=None, decimal_digits=None)])
    assert got == [(pyodbc.SQL_DECIMAL, 18, 0)]


@pytest.mark.parametrize(
    "type_name",
    ["date", "datetime", "datetime2", "smalldatetime", "time", "datetimeoffset",
     "DATE", "DateTime2"],
)
def test_datetime_columns_bound_as_wvarchar(type_name):
    """Callers bind dates as strings; without this the driver cannot cast them."""
    got = sizes_for([FakeColumn("Date", pyodbc.SQL_TYPE_TIMESTAMP, type_name=type_name)])
    assert got == [(pyodbc.SQL_WVARCHAR, 40, 0)]


def test_unknown_column_gets_none():
    """A column absent from the table is left to pyodbc rather than guessed."""
    cur = FakeCursor([FakeColumn("SKU", pyodbc.SQL_VARCHAR, column_size=64)])
    assert _input_sizes(cur, "AllItems", ["SKU", "NotAColumn"]) == [
        (pyodbc.SQL_WVARCHAR, 64, 0),
        None,
    ]


def test_other_types_left_to_pyodbc():
    got = sizes_for([FakeColumn("Qty", pyodbc.SQL_INTEGER, type_name="int")])
    assert got == [None]


def test_result_is_aligned_to_requested_column_order():
    """setinputsizes is positional - misalignment would bind the wrong widths."""
    cols = [
        FakeColumn("SKU", pyodbc.SQL_VARCHAR, column_size=64),
        FakeColumn("Price", pyodbc.SQL_DECIMAL, column_size=19, decimal_digits=4),
        FakeColumn("Qty", pyodbc.SQL_INTEGER, type_name="int"),
    ]
    got = _input_sizes(FakeCursor(cols), "AllItems", ["Qty", "SKU", "Price"])
    assert got == [
        None,
        (pyodbc.SQL_WVARCHAR, 64, 0),
        (pyodbc.SQL_DECIMAL, 19, 4),
    ]


def test_introspects_the_named_table():
    cur = FakeCursor([FakeColumn("SKU", pyodbc.SQL_VARCHAR, column_size=8)])
    _input_sizes(cur, "InactiveListings", ["SKU"])
    assert cur.asked_table == "InactiveListings"


def test_empty_column_list_returns_empty():
    assert sizes_for([FakeColumn("SKU", pyodbc.SQL_VARCHAR, column_size=8)], names=[]) == []
