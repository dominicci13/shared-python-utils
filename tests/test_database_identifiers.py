"""Unit tests for column-identifier bracketing in `database_utils.insert_dataframe`.

`insert_dataframe` builds its INSERT text from caller-supplied column names, so a
name with a hyphen, a space, a symbol or a reserved word is invalid T-SQL unless
bracketed. Some callers already pass bracketed names (and key their DataFrame by
them), so the bracketing must be idempotent: doubling a bracket breaks working code.
"""
from __future__ import annotations

import pandas as pd
import pyodbc
import pytest

from seller_automation_utils.database_utils import (
    _input_sizes,
    _quote_identifier,
    _unquote_identifier,
    insert_dataframe,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("SKU", "[SKU]"),
        ("[SKU]", "[SKU]"),
        ("P&L (30 days)", "[P&L (30 days)]"),
        ("[P&L (30 days)]", "[P&L (30 days)]"),
        ("rank", "[rank]"),
        ("a]b", "[a]]b]"),
        ("[a]]b]", "[a]]b]"),
        ("product-name", "[product-name]"),
        ("[product-name]", "[product-name]"),
        ("your price", "[your price]"),
        ("[", "[[]"),
        ("]", "[]]]"),
    ],
)
def test_quote_identifier(name, expected):
    assert _quote_identifier(name) == expected


@pytest.mark.parametrize(
    "name",
    ["SKU", "[SKU]", "a]b", "[a]]b]", "P&L (30 days)", "rank", "[product-name]"],
)
def test_quote_identifier_is_idempotent(name):
    once = _quote_identifier(name)
    assert _quote_identifier(once) == once


@pytest.mark.parametrize("name", ["", "[]"])
def test_quote_identifier_refuses_empty_name(name):
    with pytest.raises(ValueError, match="Empty column identifier"):
        _quote_identifier(name)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("SKU", "SKU"),
        ("[SKU]", "SKU"),
        ("[product-name]", "product-name"),
        ("[a]]b]", "a]b"),
        ("a]b", "a]b"),
        ("[unclosed", "[unclosed"),
        ("unopened]", "unopened]"),
        ("[", "["),
    ],
)
def test_unquote_identifier(name, expected):
    assert _unquote_identifier(name) == expected


class FakeColumn:
    """Mimics one row of `pyodbc.Cursor.columns()`, which reports bare names."""

    def __init__(self, column_name, data_type, column_size=None, decimal_digits=None, type_name=""):
        self.column_name = column_name
        self.data_type = data_type
        self.column_size = column_size
        self.decimal_digits = decimal_digits
        self.type_name = type_name


class FakeConnection:
    def __init__(self, schema: list[FakeColumn], fail_bulk: bool = False) -> None:
        self.schema = schema
        self.fail_bulk = fail_bulk
        self.cursors: list[FakeCursor] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> FakeCursor:
        cur = FakeCursor(self)
        self.cursors.append(cur)
        return cur

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeCursor:
    """Records every statement so the INSERT text can be asserted."""

    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.fast_executemany = False
        self.input_sizes: list | None = None
        self.executemany_calls: list[tuple[str, list]] = []
        self.execute_calls: list[tuple[str, tuple]] = []

    def columns(self, table: str):
        # `table` is ignored, so the real driver's failure to match a
        # schema-qualified name (no widths pinned) is not exercised here.
        return self.connection.schema

    def setinputsizes(self, sizes) -> None:
        self.input_sizes = sizes

    def executemany(self, query: str, rows) -> None:
        if self.connection.fail_bulk:
            raise pyodbc.Error("bulk bind refused")
        self.executemany_calls.append((query, list(rows)))

    def execute(self, query: str, values: tuple) -> None:
        self.execute_calls.append((query, values))

    def close(self) -> None:
        pass


def run_insert(columns: list[str], df: pd.DataFrame, schema=None, fail_bulk=False):
    conn = FakeConnection(schema or [], fail_bulk=fail_bulk)
    caller = conn.cursor()
    insert_dataframe(caller, "dbo.Orders", df, columns)
    return conn


@pytest.mark.parametrize(
    ("columns", "expected_cols"),
    [
        (["SKU", "Qty"], "[SKU], [Qty]"),
        (["P&L (30 days)", "your price"], "[P&L (30 days)], [your price]"),
        (["product-name", "afn-total-quantity"], "[product-name], [afn-total-quantity]"),
        (["rank", "order"], "[rank], [order]"),
        (["[product-name]", "[SKU]"], "[product-name], [SKU]"),
        (["[product-name]", "SKU"], "[product-name], [SKU]"),
    ],
)
def test_insert_text_brackets_every_column_once(columns, expected_cols):
    df = pd.DataFrame({c: ["x"] for c in columns})
    conn = run_insert(columns, df)
    ins = conn.cursors[1]
    [(query, rows)] = ins.executemany_calls
    assert query == f"INSERT INTO dbo.Orders ({expected_cols}) VALUES (?, ?)"
    assert "[[" not in query
    assert rows == [("x", "x")]


def test_table_name_is_not_bracketed():
    """A schema-qualified name must survive unbracketed; `[dbo.Orders]` would name one table called that."""
    conn = run_insert(["SKU"], pd.DataFrame({"SKU": ["x"]}))
    [(query, _)] = conn.cursors[1].executemany_calls
    assert query.startswith("INSERT INTO dbo.Orders (")


def test_df_keyed_by_bracketed_names_inserts_without_keyerror():
    """The two repos with a private `_sql_identifier` rename their df to bracketed keys."""
    columns = ["SKU", "[product-name]", "[afn-total-quantity]"]
    df = pd.DataFrame({
        "SKU": ["ACM-AM16", "ACM-AM17"],
        "[product-name]": ["Acme AM-16", "Acme AM-17"],
        "[afn-total-quantity]": [3, 0],
    })
    conn = run_insert(columns, df)
    [(query, rows)] = conn.cursors[1].executemany_calls
    assert query == (
        "INSERT INTO dbo.Orders ([SKU], [product-name], [afn-total-quantity]) VALUES (?, ?, ?)"
    )
    assert rows == [("ACM-AM16", "Acme AM-16", 3), ("ACM-AM17", "Acme AM-17", 0)]


def test_row_by_row_replay_uses_the_same_bracketed_text():
    """The fallback path indexes each row by the name as passed and reuses the query."""
    columns = ["[product-name]", "rank"]
    df = pd.DataFrame({"[product-name]": ["Acme AM-16"], "rank": [1]})
    conn = run_insert(columns, df, fail_bulk=True)
    diag = conn.cursors[2]
    [(query, values)] = diag.execute_calls
    assert query == "INSERT INTO dbo.Orders ([product-name], [rank]) VALUES (?, ?)"
    assert values == ("Acme AM-16", 1)
    assert conn.rollbacks == 1


def test_empty_column_name_refused_before_any_write():
    conn = FakeConnection([])
    with pytest.raises(ValueError, match="Empty column identifier"):
        insert_dataframe(conn.cursor(), "dbo.Orders", pd.DataFrame({"": ["x"]}), [""])
    assert conn.commits == 0
    assert len(conn.cursors) == 1


def test_bracketed_insert_pins_widths_from_bare_schema_names():
    """End to end: the bulk cursor gets real widths, not all-None, for bracketed names."""
    schema = [
        FakeColumn("product-name", pyodbc.SQL_WVARCHAR, column_size=500),
        FakeColumn("your-price", pyodbc.SQL_DECIMAL, column_size=19, decimal_digits=4),
    ]
    columns = ["[product-name]", "[your-price]"]
    df = pd.DataFrame({"[product-name]": ["Acme AM-16"], "[your-price]": [999.0]})
    conn = run_insert(columns, df, schema=schema)
    assert conn.cursors[1].input_sizes == [
        (pyodbc.SQL_WVARCHAR, 500, 0),
        (pyodbc.SQL_DECIMAL, 19, 4),
    ]


class SchemaOnlyCursor:
    def __init__(self, columns: list[FakeColumn]) -> None:
        self._columns = columns

    def columns(self, table: str):
        return self._columns


@pytest.mark.parametrize("name", ["product-name", "[product-name]"])
def test_input_sizes_pins_plain_and_bracketed_names_alike(name):
    """Regression: before 1.8.3 a bracketed name never matched `cursor.columns()` and got None."""
    cur = SchemaOnlyCursor([FakeColumn("product-name", pyodbc.SQL_WVARCHAR, column_size=500)])
    assert _input_sizes(cur, "T", [name]) == [(pyodbc.SQL_WVARCHAR, 500, 0)]


def test_input_sizes_sniffs_temporal_df_column_by_name_as_passed():
    """The schema is matched unbracketed, but the df is still indexed by the caller's key."""
    cur = SchemaOnlyCursor(
        [FakeColumn("report-date", pyodbc.SQL_TYPE_TIMESTAMP, type_name="date")]
    )
    df = pd.DataFrame({"[report-date]": ["2026-09-22"]})
    assert _input_sizes(cur, "T", ["[report-date]"], df) == [(pyodbc.SQL_WVARCHAR, 40, 0)]


def test_input_sizes_still_none_for_a_column_absent_from_the_schema():
    cur = SchemaOnlyCursor([FakeColumn("SKU", pyodbc.SQL_VARCHAR, column_size=64)])
    assert _input_sizes(cur, "T", ["[NotAColumn]"]) == [None]
