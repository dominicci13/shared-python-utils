from __future__ import annotations

import traceback

import pandas as pd
import pyodbc
import logging

log = logging.getLogger(__name__)
def insert_dataframe(cursor: pyodbc.Cursor, table_name: str, df: pd.DataFrame, columns: list[str]) -> None:
    """Insert all rows of a DataFrame into a SQL Server table.

    Executes a parameterized INSERT for each row and commits the transaction
    after all rows are processed.

    Args:
        cursor (pyodbc.Cursor): An active pyodbc cursor connected to the target database.
        table_name (str): Name of the destination SQL table.
        df (pd.DataFrame): DataFrame whose rows will be inserted.
        columns (list[str]): Ordered list of column names to insert.

    Raises:
        RuntimeError: If any row fails to insert.
    """
    # Opt out of pandas 3.x StringDtype default. df.iterrows() under StringDtype
    # rebuilds each row as a Series that converts None back to NaN, which pyodbc
    # cannot bind to nullable SQL columns.
    pd.set_option("future.infer_string", False)

    cols = ", ".join(columns)
    placeholders = ", ".join(["?"] * len(columns))
    query = f"INSERT INTO {table_name} ({cols}) VALUES ({placeholders})"

    for index, row in df.iterrows():
        values = tuple(row[col] for col in columns)
        if not safe_execute(cursor, query, values):
            # Embed the failing row in the RuntimeError so handle_crash's email
            # body carries full row context — not just the row index.
            row_dump = "\n".join(f"  {col}: {row[col]!r}" for col in columns)
            raise RuntimeError(
                f"Insert failed at row {index + 1} in {table_name}.\n"
                f"Row data:\n{row_dump}"
            )

    cursor.connection.commit()


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
