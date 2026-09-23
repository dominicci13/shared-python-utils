from __future__ import annotations

import re
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
# Marked before the bulk insert so a failure can undo just that insert. A plain
# rollback() would also undo the caller's uncommitted DELETE, and the replay's
# commit would then append a second copy of the day's rows.
_SAVEPOINT = "sau_insert_dataframe"
# The table name reaches the INSERT text verbatim, so an unbracketed part must be
# a T-SQL regular identifier; anything else could rewrite the statement.
_REGULAR_IDENTIFIER = re.compile(r"[A-Za-z_#@][A-Za-z0-9_@$#]*")


def _unquote_identifier(name: str) -> str:
    """Return a column name without its T-SQL brackets.

    Strips ONE outer ``[...]`` pair when present and unescapes ``]]`` to ``]``
    inside it; any other name is returned unchanged. This is the form
    ``cursor.columns()`` reports, so schema metadata must be looked up by it.

    Args:
        name (str): Column name, plain (``SKU``) or bracketed (``[SKU]``).

    Returns:
        str: The bare column name.
    """
    if len(name) >= 2 and name.startswith("[") and name.endswith("]"):
        return name[1:-1].replace("]]", "]")
    return name


def _quote_identifier(name: str) -> str:
    """Bracket a column name for dynamic T-SQL, idempotently.

    Every name is bracketed, not only "unsafe" ones, so reserved words (``rank``)
    are covered as well as hyphens, spaces and symbols. Idempotent because some
    callers already pass bracketed names: ``SKU`` and ``[SKU]`` both give
    ``[SKU]``, and an embedded ``]`` is escaped as ``]]`` exactly once.

    Args:
        name (str): Column name, plain or already bracketed.

    Returns:
        str: The name wrapped in ``[...]`` with inner ``]`` doubled.

    Raises:
        ValueError: If the name is empty once unbracketed, which T-SQL cannot
            express as an identifier.
    """
    inner = _unquote_identifier(name)
    if not inner:
        raise ValueError(f"Empty column identifier: {name!r}")
    return "[" + inner.replace("]", "]]") + "]"


def _split_table_name(table_name: str) -> tuple[str | None, str | None, str]:
    """Split a ``[catalog.][schema.]table`` name into its unbracketed parts.

    Pure. Each part may be bracketed (``[dbo].[Orders]``), and a dot inside
    brackets belongs to the name (``[Order.Lines]`` is one table). An empty
    leading part (``Reports..Orders``) comes back None, meaning the
    connection's default, as it does in T-SQL.

    Args:
        table_name (str): Table name as the caller passes it to
            ``insert_dataframe``, plain, schema-qualified or bracketed.

    Returns:
        tuple[str | None, str | None, str]: ``(catalog, schema, table)``.

    Raises:
        ValueError: On an unbalanced bracket, an unbracketed part that is not
            a regular identifier (bracket it: ``[Order Lines]``), more than
            three parts (a linked-server name), or an empty table part.
    """
    parts: list[str] = []
    i, n = 0, len(table_name)
    while True:
        while i < n and table_name[i].isspace():
            i += 1
        if i < n and table_name[i] == "[":
            chars: list[str] = []
            i += 1
            while True:
                if i >= n:
                    raise ValueError(f"Unclosed bracket in table name: {table_name!r}")
                if table_name.startswith("]]", i):
                    chars.append("]")
                    i += 2
                elif table_name[i] == "]":
                    i += 1
                    break
                else:
                    chars.append(table_name[i])
                    i += 1
            while i < n and table_name[i].isspace():
                i += 1
            if i < n and table_name[i] != ".":
                raise ValueError(f"Unexpected text after a bracketed part in table name: {table_name!r}")
            parts.append("".join(chars))
        else:
            end = table_name.find(".", i)
            end = n if end == -1 else end
            part = table_name[i:end].strip()
            if "[" in part or "]" in part:
                raise ValueError(f"Unbalanced bracket in table name: {table_name!r}")
            if part and not _REGULAR_IDENTIFIER.fullmatch(part):
                raise ValueError(f"Unbracketed part of table name is not a regular identifier: {table_name!r}")
            parts.append(part)
            i = end
        if i >= n:
            break
        i += 1

    if len(parts) > 3:
        raise ValueError(f"Table name has more than three parts: {table_name!r}")
    if not parts[-1]:
        raise ValueError(f"Empty table name: {table_name!r}")
    catalog, schema, table = [None] * (3 - len(parts)) + parts
    return catalog or None, schema or None, table


def _search_escape(cursor: pyodbc.Cursor) -> str | None:
    """Return the driver's escape character for catalog search patterns.

    Args:
        cursor (pyodbc.Cursor): Cursor whose connection is asked.

    Returns:
        str | None: The character (``\\`` for SQL Server's drivers), or None
        when the driver does not report one or cannot be asked.
    """
    try:
        escape = cursor.connection.getinfo(pyodbc.SQL_SEARCH_PATTERN_ESCAPE)
    except (AttributeError, pyodbc.Error):
        return None
    return escape if isinstance(escape, str) and escape else None


def _escape_pattern(value: str, escape: str | None) -> str:
    """Make ``_`` and ``%`` literal in an ODBC catalog pattern argument.

    Args:
        value (str): A schema or table name.
        escape (str | None): The driver's escape character, or None.

    Returns:
        str: The escaped name, or ``value`` unchanged when there is no escape
        character.
    """
    if escape is None:
        return value
    # The escape character first, or the escapes added below would be doubled.
    return (value.replace(escape, escape * 2)
            .replace("_", escape + "_")
            .replace("%", escape + "%"))


def _binds_as_text(values) -> bool:
    """Whether a column's first non-null value is a string.

    Temporal columns are only pinned WVARCHAR when the caller is actually passing
    text. Callers differ: some stringify dates (``date.isoformat()``), others hand
    over real ``datetime.date`` / ``pandas.Timestamp`` objects, and those must be
    left for pyodbc to bind natively.
    """
    for value in values:
        if value is None:
            continue
        if isinstance(value, float) and value != value:  # NaN
            continue
        return isinstance(value, str)
    return False


def _input_sizes(
    cursor: pyodbc.Cursor,
    table_name: str,
    columns: list[str],
    df: pd.DataFrame | None = None,
) -> list:
    """Build a ``setinputsizes`` entry per column from the live table schema.

    fast_executemany otherwise sizes each string parameter from the FIRST row,
    so a later, longer value raises "String data, right truncation". Pinning
    string columns to their declared width (0 => (n)varchar(max)) and decimals
    to their precision/scale fixes that; other types are left to pyodbc (None).

    Args:
        cursor (pyodbc.Cursor): Active cursor on the destination database.
        table_name (str): Destination table whose columns are introspected:
            ``Orders``, ``dbo.Orders``, ``[dbo].[Orders]`` or
            ``Reports.dbo.Orders``. The schema is passed to ``cursor.columns()``
            separately, and ``_``/``%`` are escaped so they match literally.
            Returned metadata is also filtered to the exact table (and schema,
            if given), so a driver without an escape character still cannot mix
            in a sibling table's columns. A bare name is not narrowed to a
            schema, as before.
        columns (list[str]): Ordered column names being inserted, plain or
            already bracketed. Metadata is matched on the unbracketed form;
            ``df`` is indexed by the name exactly as passed.
        df (pd.DataFrame | None): Rows about to be inserted. Used only to decide
            whether a temporal column is being fed strings or real date objects;
            without it, temporal columns are left to pyodbc.

    Returns:
        list: Values aligned to ``columns`` for ``cursor.setinputsizes``.

    Raises:
        ValueError: If ``table_name`` is malformed (see ``_split_table_name``).
    """
    catalog, schema, table = _split_table_name(table_name)
    escape = _search_escape(cursor)
    found = cursor.columns(
        table=_escape_pattern(table, escape),
        catalog=catalog,
        schema=None if schema is None else _escape_pattern(schema, escape),
    )
    meta = {
        r.column_name: r
        for r in found
        if (r.table_name or "").casefold() == table.casefold()
        and (schema is None or (r.table_schem or "").casefold() == schema.casefold())
    }
    sizes: list = []
    for col in columns:
        m = meta.get(_unquote_identifier(col))
        if m is None:
            sizes.append(None)
        elif m.data_type in _STRING_TYPES:
            size = m.column_size or 0
            sizes.append((pyodbc.SQL_WVARCHAR, 0 if size <= 0 or size > 4000 else size, 0))
        elif m.data_type in _DECIMAL_TYPES:
            sizes.append((pyodbc.SQL_DECIMAL, m.column_size or 18, m.decimal_digits or 0))
        elif (m.type_name or "").lower() in _DATETIME_NAMES:
            # Pinning WVARCHAR is what lets a "YYYY-MM-DD" string reach a date
            # column, but it breaks a real date object, so decide from the data.
            stringy = df is not None and col in df.columns and _binds_as_text(df[col])
            sizes.append((pyodbc.SQL_WVARCHAR, 40, 0) if stringy else None)
        else:
            sizes.append(None)
    return sizes


def _open_savepoint(cursor: pyodbc.Cursor) -> bool:
    """Mark ``_SAVEPOINT`` if the connection has a transaction open.

    With nothing open there is no uncommitted caller work to protect, so no
    savepoint is needed, and none is forced. ``SAVE TRANSACTION`` needs an open
    transaction, but opening one with ``BEGIN TRANSACTION`` is the riskier fix:
    the SQL Server ODBC driver runs manual-commit mode as
    ``IMPLICIT_TRANSACTIONS ON``, where an explicit ``BEGIN TRANSACTION`` on an
    idle connection nests ``@@TRANCOUNT`` to 2, and a single ``commit()`` could
    then leave the outer level open, to be rolled back when the connection
    closes. ``SELECT @@TRANCOUNT`` names no table, so it does not open a
    transaction itself.

    Args:
        cursor (pyodbc.Cursor): Cursor on the caller's connection.

    Returns:
        bool: True if the savepoint was marked.

    Raises:
        pyodbc.Error: If either statement fails.
    """
    if not cursor.execute("SELECT @@TRANCOUNT").fetchone()[0]:
        return False
    cursor.execute(f"SAVE TRANSACTION {_SAVEPOINT}")
    return True


def _rewind_to_savepoint(connection: pyodbc.Connection, table_name: str, bulk_error: Exception) -> None:
    """Undo a failed bulk attempt back to ``_SAVEPOINT``, keeping the caller's earlier work.

    The replay that follows commits, so it may only run on a transaction that
    is intact and committable. If the savepoint cannot be restored, or
    ``XACT_STATE()`` is anything but 1 afterwards (the error doomed the
    transaction, or already rolled it back whole), the whole transaction is
    rolled back and this raises instead. Replaying then would append to a table
    whose caller-side DELETE is gone: duplicates.

    Runs on a fresh cursor: the savepoint belongs to the connection's
    transaction, and the bulk cursor still carries fast_executemany bind state.

    Args:
        connection (pyodbc.Connection): The connection the bulk attempt ran on.
        table_name (str): Destination table, for the error message.
        bulk_error (Exception): Why the bulk attempt failed; chained as the
            cause.

    Raises:
        RuntimeError: The savepoint could not be restored to a committable
            transaction. Nothing from this call was written, and every
            uncommitted statement before it on the connection was rolled back.
    """
    rewind = connection.cursor()
    try:
        rewind.execute(f"ROLLBACK TRANSACTION {_SAVEPOINT}")
        state = rewind.execute("SELECT XACT_STATE()").fetchone()[0]
    except pyodbc.Error as exc:
        problem = f"rolling back to its savepoint failed ({exc})"
    else:
        if state == 1:
            return
        problem = f"the transaction was not committable after rolling back to its savepoint (XACT_STATE() = {state})"
    finally:
        rewind.close()
    connection.rollback()
    raise RuntimeError(
        f"Bulk insert into {table_name} failed and {problem}. Rolled back the whole "
        "transaction instead: nothing from this call was written, and any uncommitted "
        "statement before it on this connection (such as the caller's DELETE) was undone too."
    ) from bulk_error


def insert_dataframe(cursor: pyodbc.Cursor, table_name: str, df: pd.DataFrame, columns: list[str]) -> None:
    """Bulk-insert a DataFrame into a SQL Server table via ``fast_executemany``.

    Sends every row in one ``executemany`` with ``fast_executemany`` (requires the
    "ODBC Driver 17" connection from ``sql_connection``), pinning bind widths from
    the table schema (``_input_sizes``) so long strings don't truncate. Commits
    once on success, together with any uncommitted statement the caller ran
    before it on the same connection (the DELETE of a delete-then-insert).

    On a driver error the bulk attempt alone is undone, back to a savepoint
    marked just before it, so the caller's DELETE survives; the rows are then
    replayed one by one to find the offending row. If every row inserts, the
    replay commits once, exactly as the bulk path would have. If a row fails,
    the whole transaction is rolled back (the caller's DELETE too, so the table
    is as it was before the caller began) and the row is named in the raised
    error. If the savepoint cannot be restored, the whole transaction is rolled
    back and it raises without replaying. Any other exception during the bulk
    attempt is undone the same way and re-raised unchanged, with no replay.
    With no transaction open on entry there is nothing earlier to protect, and
    a failed bulk attempt is undone with a full rollback.

    Args:
        cursor (pyodbc.Cursor): An active pyodbc cursor connected to the target database.
        table_name (str): Name of the destination SQL table, used verbatim in
            the INSERT text: ``Orders``, ``dbo.Orders`` and ``[dbo].[Orders]``
            all work, and bind widths are pinned for each of them.
        df (pd.DataFrame): DataFrame whose rows will be inserted.
        columns (list[str]): Ordered list of column names to insert. Each name is
            used as-is to select from ``df`` and is bracketed for the INSERT
            text, so plain names (``your-price``, ``P&L (30 days)``, ``rank``)
            and already-bracketed ones (``[your-price]``) both work; brackets
            are never doubled.

    Raises:
        RuntimeError: If a row fails to insert, or a failed bulk attempt could
            not be rolled back to its savepoint.
        ValueError: If a column name is empty or ``table_name`` is malformed;
            raised before anything is sent to the database.
        pyodbc.Error: If the savepoint cannot be marked. The whole transaction
            is rolled back first.
    """
    # Opt out of pandas 3.x StringDtype default so None stays None (not NaN),
    # which pyodbc can bind to nullable SQL columns.
    pd.set_option("future.infer_string", False)

    # Refuse a malformed name before anything reaches the database.
    _split_table_name(table_name)
    cols = ", ".join(_quote_identifier(c) for c in columns)
    placeholders = ", ".join(["?"] * len(columns))
    query = f"INSERT INTO {table_name} ({cols}) VALUES ({placeholders})"

    rows = list(df[columns].itertuples(index=False, name=None))
    if not rows:
        cursor.connection.commit()
        return

    # Dedicated cursor so fast_executemany + setinputsizes never leak onto the
    # caller's cursor (whose later queries would otherwise inherit stale binds).
    # It shares cursor.connection, so it runs inside the caller's transaction.
    ins = cursor.connection.cursor()
    try:
        savepoint = _open_savepoint(ins)
    except pyodbc.Error:
        ins.close()
        cursor.connection.rollback()
        raise
    try:
        ins.setinputsizes(_input_sizes(ins, table_name, columns, df))
        ins.fast_executemany = True
        ins.executemany(query, rows)
        ins.connection.commit()
        return
    except pyodbc.Error as exc:
        if savepoint:
            _rewind_to_savepoint(ins.connection, table_name, exc)
        else:
            ins.connection.rollback()
    except Exception as exc:
        # Not a driver error, so the replay would not help; still undo the
        # partial bulk attempt, or the caller's next commit would keep it.
        if savepoint:
            _rewind_to_savepoint(ins.connection, table_name, exc)
        else:
            ins.connection.rollback()
        raise
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
