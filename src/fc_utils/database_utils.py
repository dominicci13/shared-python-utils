from __future__ import annotations

import traceback

import pandas as pd
import pyodbc
from rich import print


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
    cols = ", ".join(columns)
    placeholders = ", ".join(["?"] * len(columns))
    query = f"INSERT INTO {table_name} ({cols}) VALUES ({placeholders})"

    print(f"[cyan][INFO][/cyan] Inserting [bold]{len(df)}[/bold] rows into [cyan]{table_name}[/cyan]...")

    for index, row in df.iterrows():
        values = tuple(row[col] for col in columns)
        if not safe_execute(cursor, query, values):
            raise RuntimeError(f"Insert failed at row {index + 1}. See traceback above.")

    cursor.connection.commit()
    print(f"[green][SUCCESS][/green] Inserted [bold]{len(df)}[/bold] rows into [cyan]{table_name}[/cyan].")


def upsert_dataframe(
    cursor: pyodbc.Cursor,
    table_name: str,
    df: pd.DataFrame,
    columns: list[str],
    key_columns: list[str],
) -> None:
    """Insert or update rows from a DataFrame into a SQL Server table.

    For each row, attempts an UPDATE on the key columns first. If no rows
    were affected (i.e., the record does not exist), falls back to INSERT.
    Commits the transaction after all rows are processed.

    Args:
        cursor (pyodbc.Cursor): An active pyodbc cursor connected to the target database.
        table_name (str): Name of the destination SQL table.
        df (pd.DataFrame): DataFrame whose rows will be upserted.
        columns (list[str]): All column names to insert or update.
        key_columns (list[str]): Column names that uniquely identify a row (used in WHERE clause).

    Raises:
        RuntimeError: If any row fails to upsert.
    """
    non_key_columns = [c for c in columns if c not in key_columns]
    update_query = (
        f"UPDATE {table_name} SET "
        + ", ".join(f"{c} = ?" for c in non_key_columns)
        + " WHERE "
        + " AND ".join(f"{k} = ?" for k in key_columns)
    )
    insert_query = (
        f"INSERT INTO {table_name} ({', '.join(columns)}) "
        f"VALUES ({', '.join(['?'] * len(columns))})"
    )

    print(f"[cyan][INFO][/cyan] Upserting [bold]{len(df)}[/bold] rows into [cyan]{table_name}[/cyan]...")

    for index, row in df.iterrows():
        non_key_values = tuple(row[c] for c in non_key_columns)
        key_values = tuple(row[k] for k in key_columns)

        cursor.execute(update_query, non_key_values + key_values)
        if cursor.rowcount == 0:
            all_values = tuple(row[c] for c in columns)
            if not safe_execute(cursor, insert_query, all_values):
                raise RuntimeError(f"Upsert failed at row {index + 1}. See traceback above.")

    cursor.connection.commit()
    print(f"[green][SUCCESS][/green] Upserted [bold]{len(df)}[/bold] rows into [cyan]{table_name}[/cyan].")


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
        print("[bold red][ERROR][/bold red] [pyodbc.Error] Failed to execute query.")
        traceback.print_exc()
        return False
