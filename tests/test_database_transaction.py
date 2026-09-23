"""Unit tests for the transaction handling in `database_utils.insert_dataframe`.

Callers run `DELETE ... WHERE ReportDate = ?` and then `insert_dataframe` on the
same connection without committing in between, so the delete and the insert
land together. Before 1.8.4 a failed bulk attempt ran a full `rollback()`, which
also undid that DELETE, and the row-by-row replay then committed: a same-day
rerun appended a second copy of the day's rows. These tests pin the fix against
a fake connection that models SQL Server's transaction, savepoint and
`XACT_STATE()` behaviour closely enough to show where the rows end up.

A fake cannot prove the real driver behaves this way; a live SQL Server check
(ODBC Driver 17, 2026-09-22) confirmed it: the forced bulk failure left 2 rows, not 4.
"""
from __future__ import annotations

import pandas as pd
import pyodbc
import pytest

from seller_automation_utils.database_utils import _SAVEPOINT, insert_dataframe

TODAY = "2026-09-22"
YESTERDAY = "2026-09-21"
SAVE = f"SAVE TRANSACTION {_SAVEPOINT}"
REWIND = f"ROLLBACK TRANSACTION {_SAVEPOINT}"


class FakeServer:
    """One connection's view of a single table, with SQL Server's transaction rules.

    `committed` is what another session would read. `pending` holds this
    connection's uncommitted work, oldest first; a savepoint is an index into it.
    Every statement, commit and rollback is appended to `log` in order.

    Args:
        rows: The committed table, as ``(report_date, sku)`` tuples.
        fail_bulk: Make `executemany` insert its first row and then fail, as a
            fast_executemany batch can.
        bulk_effect: What the bulk failure does to the transaction:
            ``"statement"`` (only the statement fails; the usual case),
            ``"doom"`` (XACT_STATE -1, e.g. under XACT_ABORT for some errors) or
            ``"abort"`` (the whole transaction is rolled back, e.g. a deadlock).
        bad_skus: Skus whose single-row INSERT fails during the replay.
        fail_trancount: Make the `@@TRANCOUNT` probe itself fail.
    """

    def __init__(self, rows, *, fail_bulk=False, bulk_effect="statement", bad_skus=(), fail_trancount=False):
        self.committed: list[tuple] = list(rows)
        self.pending: list[tuple] = []
        self.in_tran = False
        self.doomed = False
        self.savepoints: dict[str, int] = {}
        self.fail_bulk = fail_bulk
        self.bulk_effect = bulk_effect
        self.bad_skus = set(bad_skus)
        self.fail_trancount = fail_trancount
        self.log: list[str] = []

    # --- connection API -------------------------------------------------

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.log.append("COMMIT")
        assert not self.doomed, "commit on a doomed transaction"
        self.committed = self.table()
        self._end()

    def rollback(self) -> None:
        self.log.append("ROLLBACK")
        self._end()

    def getinfo(self, info_type):
        return "\\"

    # --- helpers --------------------------------------------------------

    def _end(self) -> None:
        self.pending.clear()
        self.savepoints.clear()
        self.in_tran = False
        self.doomed = False

    def _write(self, op: tuple) -> None:
        # Manual-commit mode: the first write opens a transaction implicitly.
        self.in_tran = True
        self.pending.append(op)

    def table(self) -> list[tuple]:
        """The table as this connection sees it, pending work applied."""
        rows = list(self.committed)
        for op in self.pending:
            if op[0] == "delete":
                rows = [r for r in rows if r[0] != op[1]]
            else:
                rows.append(op[1])
        return rows

    def caller_deletes(self, report_date: str) -> None:
        """What `amzn-ca-fba-inventory` does before calling insert_dataframe, uncommitted."""
        self.log.append(f"DELETE {report_date}")
        self._write(("delete", report_date))


class FakeCursor:
    def __init__(self, server: FakeServer) -> None:
        self.connection = server
        self.fast_executemany = False
        self._result = None

    def execute(self, query: str, values: tuple = ()) -> FakeCursor:
        s = self.connection
        if query == "SELECT @@TRANCOUNT":
            s.log.append(query)
            if s.fail_trancount:
                raise pyodbc.Error("08S01", "Communication link failure")
            self._result = (1 if s.in_tran else 0,)
        elif query == "SELECT XACT_STATE()":
            s.log.append(query)
            self._result = (-1 if s.doomed else 1 if s.in_tran else 0,)
        elif query.startswith("SAVE TRANSACTION "):
            s.log.append(query)
            if not s.in_tran:
                raise pyodbc.Error("25000", "[628] Cannot issue SAVE TRANSACTION when there is no active transaction.")
            s.savepoints[query.split()[-1]] = len(s.pending)
        elif query.startswith("ROLLBACK TRANSACTION "):
            s.log.append(query)
            name = query.split()[-1]
            if not s.in_tran or name not in s.savepoints:
                raise pyodbc.Error("25000", "[3903] The ROLLBACK TRANSACTION request has no corresponding BEGIN TRANSACTION.")
            if s.doomed:
                raise pyodbc.Error("25000", "[3931] The current transaction cannot be committed and cannot "
                                            "support operations that write to the log file.")
            del s.pending[s.savepoints[name]:]
        elif query.startswith("INSERT INTO"):
            s.log.append(f"INSERT {values[1]}")
            if values[1] in s.bad_skus:
                raise pyodbc.Error("23000", "[2627] Violation of PRIMARY KEY constraint.")
            s._write(("insert", tuple(values)))
        else:
            raise AssertionError(f"unexpected SQL: {query}")
        return self

    def fetchone(self):
        return self._result

    def executemany(self, query: str, rows) -> None:
        s = self.connection
        rows = list(rows)
        s.log.append(f"EXECUTEMANY {len(rows)}")
        if not s.fail_bulk:
            for row in rows:
                s._write(("insert", tuple(row)))
            return
        s._write(("insert", tuple(rows[0])))  # a partial batch, which must not survive
        if s.bulk_effect == "doom":
            s.doomed = True
        elif s.bulk_effect == "abort":
            s._end()
        raise pyodbc.Error("HY000", "bulk bind refused")

    def setinputsizes(self, sizes) -> None:
        pass

    def columns(self, table=None, catalog=None, schema=None, column=None):
        return []

    def close(self) -> None:
        pass


ALREADY_THERE = [(YESTERDAY, "OLD-1"), (TODAY, "ACM-001"), (TODAY, "ACM-002")]


def todays_frame() -> pd.DataFrame:
    return pd.DataFrame({"ReportDate": [TODAY, TODAY], "SKU": ["ACM-001", "ACM-002"]})


def rerun(server: FakeServer) -> None:
    """A same-day rerun: the caller's uncommitted DELETE, then the insert."""
    server.caller_deletes(TODAY)
    insert_dataframe(server.cursor(), "dbo.CaInventory", todays_frame(), ["ReportDate", "SKU"])


def test_happy_path_bulk_inserts_and_commits_once():
    server = FakeServer(ALREADY_THERE)
    rerun(server)
    assert server.log == [f"DELETE {TODAY}", "SELECT @@TRANCOUNT", SAVE, "EXECUTEMANY 2", "COMMIT"]
    assert sorted(server.committed) == sorted(ALREADY_THERE)


def test_happy_path_with_no_open_transaction_marks_no_savepoint():
    server = FakeServer([])
    insert_dataframe(server.cursor(), "dbo.CaInventory", todays_frame(), ["ReportDate", "SKU"])
    assert server.log == ["SELECT @@TRANCOUNT", "EXECUTEMANY 2", "COMMIT"]
    assert sorted(server.committed) == [(TODAY, "ACM-001"), (TODAY, "ACM-002")]


def test_empty_frame_only_commits():
    server = FakeServer(ALREADY_THERE)
    server.caller_deletes(TODAY)
    insert_dataframe(server.cursor(), "dbo.CaInventory", todays_frame().iloc[0:0], ["ReportDate", "SKU"])
    assert server.log == [f"DELETE {TODAY}", "COMMIT"]


def test_bulk_failure_rewinds_to_the_savepoint_and_keeps_the_callers_delete():
    """The regression: a same-day rerun whose bulk path fails must not duplicate today's rows."""
    server = FakeServer(ALREADY_THERE, fail_bulk=True)
    rerun(server)
    assert server.log == [
        f"DELETE {TODAY}", "SELECT @@TRANCOUNT", SAVE, "EXECUTEMANY 2",
        REWIND, "SELECT XACT_STATE()",
        "INSERT ACM-001", "INSERT ACM-002", "COMMIT",
    ]
    assert "ROLLBACK" not in server.log
    assert server.log.count("COMMIT") == 1
    assert sorted(server.committed) == sorted(ALREADY_THERE)


def test_bulk_failure_with_no_open_transaction_rolls_back_fully_then_replays():
    """Nothing uncommitted preceded the call, so a full rollback undoes only the partial batch."""
    server = FakeServer([(YESTERDAY, "OLD-1")], fail_bulk=True)
    insert_dataframe(server.cursor(), "dbo.CaInventory", todays_frame(), ["ReportDate", "SKU"])
    assert server.log == [
        "SELECT @@TRANCOUNT", "EXECUTEMANY 2", "ROLLBACK",
        "INSERT ACM-001", "INSERT ACM-002", "COMMIT",
    ]
    assert sorted(server.committed) == [(YESTERDAY, "OLD-1"), (TODAY, "ACM-001"), (TODAY, "ACM-002")]


def test_bad_row_in_the_replay_rolls_everything_back_and_names_the_row():
    """The table is left exactly as it was before the caller's DELETE."""
    server = FakeServer(ALREADY_THERE, fail_bulk=True, bad_skus={"ACM-002"})
    with pytest.raises(RuntimeError, match=r"Insert failed at row 2 in dbo\.CaInventory") as excinfo:
        rerun(server)
    assert "SKU: 'ACM-002'" in str(excinfo.value)
    assert server.log[-3:] == ["INSERT ACM-001", "INSERT ACM-002", "ROLLBACK"]
    assert "COMMIT" not in server.log
    assert server.committed == ALREADY_THERE


@pytest.mark.parametrize(
    ("bulk_effect", "why"),
    [
        pytest.param("doom", "rolling back to its savepoint failed", id="doomed-xact-state-minus-1"),
        pytest.param("abort", "rolling back to its savepoint failed", id="whole-transaction-already-gone"),
    ],
)
def test_savepoint_rewind_failure_rolls_back_fully_and_never_replays(bulk_effect, why):
    server = FakeServer(ALREADY_THERE, fail_bulk=True, bulk_effect=bulk_effect)
    with pytest.raises(RuntimeError, match="Bulk insert into dbo.CaInventory failed") as excinfo:
        rerun(server)
    assert why in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, pyodbc.Error)
    assert "bulk bind refused" in str(excinfo.value.__cause__)
    assert server.log[-2:] == [REWIND, "ROLLBACK"]
    assert not [entry for entry in server.log if entry.startswith("INSERT")]
    assert "COMMIT" not in server.log
    assert server.committed == ALREADY_THERE


def test_uncommittable_state_after_the_rewind_rolls_back_fully_and_never_replays(monkeypatch):
    """Belt and braces: a rewind that "succeeds" onto a transaction that cannot commit must not replay."""
    server = FakeServer(ALREADY_THERE, fail_bulk=True)
    original = FakeCursor.execute

    def execute(self, query, values=()):
        result = original(self, query, values)
        if query == "SELECT XACT_STATE()":
            self._result = (-1,)
        return result

    monkeypatch.setattr(FakeCursor, "execute", execute)
    with pytest.raises(RuntimeError, match=r"XACT_STATE\(\) = -1"):
        rerun(server)
    assert server.log[-3:] == [REWIND, "SELECT XACT_STATE()", "ROLLBACK"]
    assert "COMMIT" not in server.log
    assert server.committed == ALREADY_THERE


def _bulk_raises(monkeypatch, error: Exception) -> None:
    """Make `executemany` write its first row and then raise a non-driver error."""

    def executemany(self, query, rows):
        rows = list(rows)
        self.connection.log.append(f"EXECUTEMANY {len(rows)}")
        self.connection._write(("insert", tuple(rows[0])))
        raise error

    monkeypatch.setattr(FakeCursor, "executemany", executemany)


def test_non_driver_error_mid_bulk_rewinds_to_the_savepoint_and_propagates_unchanged(monkeypatch):
    error = TypeError("cannot bind this value")
    _bulk_raises(monkeypatch, error)
    server = FakeServer(ALREADY_THERE)
    with pytest.raises(TypeError) as excinfo:
        rerun(server)
    assert excinfo.value is error
    assert server.log == [f"DELETE {TODAY}", "SELECT @@TRANCOUNT", SAVE, "EXECUTEMANY 2", REWIND, "SELECT XACT_STATE()"]
    assert server.pending == [("delete", TODAY)]
    assert "COMMIT" not in server.log
    assert server.committed == ALREADY_THERE


def test_non_driver_error_mid_bulk_with_no_open_transaction_rolls_back_and_propagates(monkeypatch):
    error = TypeError("cannot bind this value")
    _bulk_raises(monkeypatch, error)
    server = FakeServer([(YESTERDAY, "OLD-1")])
    with pytest.raises(TypeError) as excinfo:
        insert_dataframe(server.cursor(), "dbo.CaInventory", todays_frame(), ["ReportDate", "SKU"])
    assert excinfo.value is error
    assert server.log == ["SELECT @@TRANCOUNT", "EXECUTEMANY 2", "ROLLBACK"]
    assert server.committed == [(YESTERDAY, "OLD-1")]
    assert server.pending == []


def test_savepoint_probe_failure_rolls_back_and_raises_before_any_insert():
    server = FakeServer(ALREADY_THERE, fail_trancount=True)
    with pytest.raises(pyodbc.Error, match="Communication link failure"):
        rerun(server)
    assert server.log == [f"DELETE {TODAY}", "SELECT @@TRANCOUNT", "ROLLBACK"]
    assert server.committed == ALREADY_THERE


@pytest.mark.parametrize(
    "table_name",
    ["dbo.", "[dbo.CaInventory", "Srv.Reports.dbo.CaInventory",
     "CaInventory (a) VALUES (1); DROP TABLE CaInventory; --", "CaInventory;", "Ca Inventory", "CaInventory--"],
)
def test_malformed_table_name_refused_before_any_sql(table_name):
    server = FakeServer(ALREADY_THERE)
    with pytest.raises(ValueError):
        insert_dataframe(server.cursor(), table_name, todays_frame(), ["ReportDate", "SKU"])
    assert server.log == []
