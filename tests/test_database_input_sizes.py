"""Unit tests for `database_utils._input_sizes`, the fast_executemany bind widths.

This helper is the reason the bulk insert is safe: fast_executemany otherwise sizes
each string parameter from the FIRST row, so a longer later value raises "String
data, right truncation". Getting a width wrong here silently corrupts data, so every
branch is covered against a fake cursor rather than a live database.
"""
from __future__ import annotations

import re
from datetime import date, datetime

import pandas as pd
import pyodbc
import pytest

from seller_automation_utils.database_utils import _escape_pattern, _input_sizes, _split_table_name


class FakeColumn:
    """Mimics one row of `pyodbc.Cursor.columns()`.

    `table_name` left None is filled with whichever table was asked for, so these
    tests stay about widths; table matching is covered at the end of this file.
    """

    def __init__(self, column_name, data_type, column_size=None, decimal_digits=None, type_name="",
                 table_name=None, table_schem="dbo"):
        self.column_name = column_name
        self.data_type = data_type
        self.column_size = column_size
        self.decimal_digits = decimal_digits
        self.type_name = type_name
        self.table_name = table_name
        self.table_schem = table_schem


class FakeCursor:
    def __init__(self, columns: list[FakeColumn]) -> None:
        self._columns = columns
        self.asked_table: str | None = None

    def columns(self, table=None, catalog=None, schema=None, column=None):
        self.asked_table = table
        for c in self._columns:
            if c.table_name is None:
                c.table_name = table
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
def test_datetime_pinned_wvarchar_when_values_are_strings(type_name):
    """A "YYYY-MM-DD" string only reaches a date column if pinned WVARCHAR."""
    cols = [FakeColumn("Date", pyodbc.SQL_TYPE_TIMESTAMP, type_name=type_name)]
    df = pd.DataFrame({"Date": ["2026-07-30", "2026-07-31"]})
    assert _input_sizes(FakeCursor(cols), "T", ["Date"], df) == [(pyodbc.SQL_WVARCHAR, 40, 0)]


@pytest.mark.parametrize(
    "value",
    [date(2026, 7, 30), datetime(2026, 7, 30, 12, 0), pd.Timestamp("2026-07-30 12:00")],
)
def test_datetime_left_native_when_values_are_objects(value):
    """Regression: pinning WVARCHAR breaks real date objects.

    sellercloud-sync builds `LastReceived` with `pd.to_datetime(...)`, so it hands
    over Timestamps; 1.3.0 pinned that column WVARCHAR and the insert failed.
    """
    cols = [FakeColumn("LastReceived", pyodbc.SQL_TYPE_TIMESTAMP, type_name="datetime")]
    df = pd.DataFrame({"LastReceived": [value]})
    assert _input_sizes(FakeCursor(cols), "T", ["LastReceived"], df) == [None]


def test_datetime_ignores_leading_nulls_when_sniffing():
    cols = [FakeColumn("Date", pyodbc.SQL_TYPE_TIMESTAMP, type_name="date")]
    df = pd.DataFrame({"Date": [None, None, "2026-07-30"]})
    assert _input_sizes(FakeCursor(cols), "T", ["Date"], df) == [(pyodbc.SQL_WVARCHAR, 40, 0)]


def test_datetime_all_null_column_left_native():
    cols = [FakeColumn("Date", pyodbc.SQL_TYPE_TIMESTAMP, type_name="date")]
    df = pd.DataFrame({"Date": [None, None]})
    assert _input_sizes(FakeCursor(cols), "T", ["Date"], df) == [None]


def test_datetime_left_native_without_a_dataframe():
    """No data to sniff means no assumption about how the caller binds."""
    got = sizes_for([FakeColumn("Date", pyodbc.SQL_TYPE_TIMESTAMP, type_name="date")])
    assert got == [None]


def test_string_and_decimal_pinning_is_unaffected_by_data():
    """Only temporal columns consult the data; the rest stay schema-driven."""
    cols = [
        FakeColumn("SKU", pyodbc.SQL_VARCHAR, column_size=64),
        FakeColumn("Price", pyodbc.SQL_DECIMAL, column_size=19, decimal_digits=4),
    ]
    df = pd.DataFrame({"SKU": [None], "Price": [None]})
    assert _input_sizes(FakeCursor(cols), "T", ["SKU", "Price"], df) == [
        (pyodbc.SQL_WVARCHAR, 64, 0),
        (pyodbc.SQL_DECIMAL, 19, 4),
    ]


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


# --- table-name resolution --------------------------------------------------

@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Orders", (None, None, "Orders")),
        ("dbo.Orders", (None, "dbo", "Orders")),
        ("[dbo].[Orders]", (None, "dbo", "Orders")),
        ("[dbo].Orders", (None, "dbo", "Orders")),
        ("dbo.[Order Lines]", (None, "dbo", "Order Lines")),
        ("[Order.Lines]", (None, None, "Order.Lines")),
        ("[a]]b]", (None, None, "a]b")),
        ("Reports.dbo.Orders", ("Reports", "dbo", "Orders")),
        ("Reports..Orders", ("Reports", None, "Orders")),
        (" dbo . Orders ", (None, "dbo", "Orders")),
        ("[dbo] . [Orders]", (None, "dbo", "Orders")),
        ("Order_Lines", (None, None, "Order_Lines")),
    ],
)
def test_split_table_name(name, expected):
    assert _split_table_name(name) == expected


@pytest.mark.parametrize(
    "name",
    ["", "   ", "dbo.", "[]", "[dbo.Orders", "dbo.Ord]ers", "dbo.Ord[ers", "[dbo]x.Orders",
     "Srv.Reports.dbo.Orders"],
)
def test_split_table_name_refuses_a_malformed_name(name):
    with pytest.raises(ValueError, match="(?i)table name"):
        _split_table_name(name)


@pytest.mark.parametrize(
    "name",
    ["Orders (a) VALUES (1); DROP TABLE Orders; --", "Orders;", "Or ders", "Orders--", "dbo.Orders;"],
)
def test_split_table_name_refuses_an_unbracketed_non_identifier(name):
    """The name reaches the INSERT text verbatim, so it must not be able to extend the statement."""
    with pytest.raises(ValueError, match="not a regular identifier"):
        _split_table_name(name)


@pytest.mark.parametrize(
    ("value", "escape", "expected"),
    [
        ("Orders", "\\", "Orders"),
        ("Order_Lines", "\\", "Order\\_Lines"),
        ("Pct%Off", "\\", "Pct\\%Off"),
        ("Back\\slash_x", "\\", "Back\\\\slash\\_x"),
        ("Order_Lines", None, "Order_Lines"),
    ],
)
def test_escape_pattern(value, escape, expected):
    assert _escape_pattern(value, escape) == expected


def odbc_pattern(pattern: str, escape: str | None) -> re.Pattern:
    """Compile an ODBC catalog search pattern the way the driver matches it (case-insensitive)."""
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if escape and ch == escape and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
        i += 1
    return re.compile("".join(out) + r"\Z", re.IGNORECASE)


# `_` would match any character without escaping, so each underscored name has a
# sibling it must not be confused with.
CATALOG = {
    ("dbo", "Returns"): [("SKU", 10)],
    ("dbo", "Order_Lines"): [("SKU", 20)],
    ("dbo", "OrderXLines"): [("SKU", 99), ("Extra", 50)],
    ("sales_eu", "Orders"): [("SKU", 30)],
    ("salesXeu", "Orders"): [("SKU", 88)],
}


class CatalogConnection:
    def __init__(self, escape) -> None:
        self.escape = escape

    def getinfo(self, info_type):
        assert info_type == pyodbc.SQL_SEARCH_PATTERN_ESCAPE
        if isinstance(self.escape, Exception):
            raise self.escape
        return self.escape


class CatalogCursor:
    """A cursor over `CATALOG` that honours ODBC pattern semantics, wildcards included."""

    def __init__(self, escape="\\") -> None:
        self.connection = CatalogConnection(escape)
        self.asked: dict | None = None

    def columns(self, table=None, catalog=None, schema=None, column=None):
        self.asked = {"table": table, "schema": schema, "catalog": catalog}
        esc = self.connection.escape if isinstance(self.connection.escape, str) and self.connection.escape else None
        table_re = odbc_pattern(table, esc)
        schema_re = odbc_pattern(schema, esc) if schema is not None else None
        return [
            FakeColumn(col, pyodbc.SQL_WVARCHAR, column_size=size, table_name=t, table_schem=s)
            for (s, t), cols in CATALOG.items()
            if table_re.match(t) and (schema_re is None or schema_re.match(s))
            for col, size in cols
        ]


def width(size: int) -> tuple:
    return (pyodbc.SQL_WVARCHAR, size, 0)


@pytest.mark.parametrize(
    ("table_name", "asked"),
    [
        ("Returns", {"table": "Returns", "schema": None, "catalog": None}),
        ("dbo.Returns", {"table": "Returns", "schema": "dbo", "catalog": None}),
        ("[dbo].[Returns]", {"table": "Returns", "schema": "dbo", "catalog": None}),
        ("Reports.dbo.Returns", {"table": "Returns", "schema": "dbo", "catalog": "Reports"}),
        ("dbo.returns", {"table": "returns", "schema": "dbo", "catalog": None}),
    ],
)
def test_schema_qualified_and_bracketed_names_pin_widths(table_name, asked):
    """Regression: `dbo.Returns` used to reach `cursor.columns()` as a literal table name and pin nothing."""
    cur = CatalogCursor()
    assert _input_sizes(cur, table_name, ["SKU"]) == [width(10)]
    assert cur.asked == asked


@pytest.mark.parametrize("escape", ["\\", pyodbc.Error("getinfo not supported"), "", None])
def test_underscore_table_never_takes_a_sibling_tables_widths(escape):
    """With or without an escape character, `Order_Lines` must not read `OrderXLines`."""
    cur = CatalogCursor(escape)
    assert _input_sizes(cur, "dbo.Order_Lines", ["SKU", "Extra"]) == [width(20), None]


def test_underscore_table_is_escaped_when_the_driver_reports_an_escape():
    cur = CatalogCursor("\\")
    _input_sizes(cur, "[dbo].[Order_Lines]", ["SKU"])
    assert cur.asked["table"] == "Order\\_Lines"


@pytest.mark.parametrize("escape", ["\\", pyodbc.Error("getinfo not supported")])
def test_underscore_schema_never_takes_a_sibling_schemas_widths(escape):
    cur = CatalogCursor(escape)
    assert _input_sizes(cur, "sales_eu.Orders", ["SKU"]) == [width(30)]


def test_underscore_schema_is_escaped_but_catalog_is_not():
    """SQLColumns treats the catalog as an ordinary argument, so escaping it would break the match."""
    cur = CatalogCursor("\\")
    _input_sizes(cur, "Some_Db.sales_eu.Orders", ["SKU"])
    assert cur.asked == {"table": "Orders", "schema": "sales\\_eu", "catalog": "Some_Db"}


def test_no_widths_for_a_table_in_another_schema():
    """`dbo.Orders` does not exist in CATALOG; the `sales_eu`/`salesXeu` tables must not stand in for it."""
    assert _input_sizes(CatalogCursor(), "dbo.Orders", ["SKU"]) == [None]
