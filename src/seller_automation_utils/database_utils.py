from __future__ import annotations

import traceback

import pandas as pd
import pyodbc
import logging

log = logging.getLogger(__name__)
# SQL type codes that need an explicit bind width under fast_executemany.
_STRING_TYPES = frozenset({
    pyodbc.SQL_CHAR, pyodbc.SQL_VARCHAR, pyodbc.SQL_LONGVARCHAR,
    pyodbc.SQL_WCHAR, pyodbc.SQL_WVARCHAR, pyodbc.SQL_WLONGVARCHAR,
})
_DECIMAL_TYPES = frozenset({pyodbc.SQL_DECIMAL, pyodbc.SQL_NUMERIC})
# date/time columns are bound as strings by callers, so pin them WVARCHAR too —
# fast_executemany otherwise can't cast a string into a date/datetime column.
_DATETIME_NAMES = frozenset({"date", "datetime", "datetime2", "smalldatetime", "time", "datetimeoffset"})


def _input_sizes(cursor: pyodbc.Cursor, table_name: str, columns: list[str]) -> list:
    """Build a ``setinputsizes`` entry per column from the live table schema.

    fast_executemany otherwise sizes each string parameter from the FIRST row,
    so a later, longer value raises "String data, right truncation". Pinning
    string columns to their declared width (0 => (n)varchar(max)) and decimals
    to their precision/scale fixes that; other types are left to pyodbc (None).

    Args:
        cursor (pyodbc.Cursor): Active cursor on the destination database.
        table_name (str): Destination table whose columns are introspected.
        columns (list[str]): Ordered column names being inserted.

    Returns:
        list: Values aligned to ``columns`` for ``cursor.setinputsizes``.
    """
    meta = {r.column_name: r for r in cursor.columns(table=table_name)}
    sizes: list = []
    for col in columns:
        m = meta.get(col)
        if m is None:
            sizes.append(None)
        elif m.data_type in _STRING_TYPES:
            size = m.column_size or 0
            sizes.append((pyodbc.SQL_WVARCHAR, 0 if size <= 0 or size > 4000 else size, 0))
        elif m.data_type in _DECIMAL_TYPES:
            sizes.append((pyodbc.SQL_DECIMAL, m.column_size or 18, m.decimal_digits or 0))
        elif (m.type_name or "").lower() in _DATETIME_NAMES:
            sizes.append((pyodbc.SQL_WVARCHAR, 40, 0))
        else:
            sizes.append(None)
    return sizes


def insert_dataframe(cursor: pyodbc.Cursor, table_name: str, df: pd.DataFrame, columns: list[str]) -> None:
    """Bulk-insert a DataFrame into a SQL Server table via ``fast_executemany``.

    Sends every row in one ``executemany`` with ``fast_executemany`` (requires the
    "ODBC Driver 17" connection from ``sql_connection``), pinning bind widths from
    the table schema (``_input_sizes``) so long strings don't truncate. On any
    driver error it rolls back, replays row-by-row to pinpoint the offending row
    for the crash email, and re-raises. Commits once on success.

    Args:
        cursor (pyodbc.Cursor): An active pyodbc cursor connected to the target database.
        table_name (str): Name of the destination SQL table.
        df (pd.DataFrame): DataFrame whose rows will be inserted.
        columns (list[str]): Ordered list of column names to insert.

    Raises:
        RuntimeError: If a row fails to insert.
    """
    # Opt out of pandas 3.x StringDtype default so None stays None (not NaN),
    # which pyodbc can bind to nullable SQL columns.
    pd.set_option("future.infer_string", False)

    cols = ", ".join(columns)
    placeholders = ", ".join(["?"] * len(columns))
    query = f"INSERT INTO {table_name} ({cols}) VALUES ({placeholders})"

    rows = list(df[columns].itertuples(index=False, name=None))
    if not rows:
        cursor.connection.commit()
        return

    # Dedicated cursor so fast_executemany + setinputsizes never leak onto the
    # caller's cursor (whose later queries would otherwise inherit stale binds).
    # It shares cursor.connection, so a preceding uncommitted DELETE stays in the
    # same transaction — atomic delete-then-insert is preserved.
    ins = cursor.connection.cursor()
    try:
        ins.setinputsizes(_input_sizes(ins, table_name, columns))
        ins.fast_executemany = True
        ins.executemany(query, rows)
        ins.connection.commit()
        return
    except pyodbc.Error:
        ins.connection.rollback()
    finally:
        ins.close()

    # Bulk failed — replay row-by-row on a clean cursor. If a row genuinely
    # fails, name it for handle_crash's email and re-raise; otherwise the fast
    # path just couldn't bulk-bind these types, so the row-by-row inserts are
    # valid and we keep them.
    diag = cursor.connection.cursor()
    try:
        for index, row in df.iterrows():
            values = tuple(row[col] for col in columns)
            if not safe_execute(diag, query, values):
                diag.connection.rollback()
                row_dump = "\n".join(f"  {col}: {row[col]!r}" for col in columns)
                raise RuntimeError(
                    f"Insert failed at row {index + 1} in {table_name}.\n"
                    f"Row data:\n{row_dump}"
                )
        diag.connection.commit()
    finally:
        diag.close()


def safe_execute(cursor: pyodbc.Cursor, query: str, values: tuple) -> bool:
    """Execute a single parameterized SQL statement with error handling.

    Args:
        cursor (pyodbc.Cursor): An active pyodbc cursor.
        query (str): Parameterized SQL query string (use ? as placeholders).
        values (tuple): Values to bind to the query placeholders.

    Returns:
        bool: True if the statement executed successfully, False on pyodbc.Error.
    """
    try:
        cursor.execute(query, values)
        return True
    except pyodbc.Error:
        log.error("[pyodbc.Error] Failed to execute query.")
        traceback.print_exc()
        return False
