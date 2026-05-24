"""Unit tests for `seller_automation_utils.file_utils` pure helpers."""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from seller_automation_utils.file_utils import (
    create_dir_structure,
    latest_modified_date,
)


def test_create_dir_structure_creates_nested(tmp_path: Path) -> None:
    create_dir_structure(str(tmp_path), ["logs", "output/reports"])
    assert (tmp_path / "logs").is_dir()
    assert (tmp_path / "output" / "reports").is_dir()


def test_create_dir_structure_idempotent(tmp_path: Path) -> None:
    """Re-running over a pre-existing folder must not raise."""
    (tmp_path / "existing").mkdir()
    create_dir_structure(str(tmp_path), ["existing", "fresh"])
    assert (tmp_path / "existing").is_dir()
    assert (tmp_path / "fresh").is_dir()


def test_latest_modified_date_empty_tree_returns_none(tmp_path: Path) -> None:
    assert latest_modified_date(str(tmp_path)) is None


def test_latest_modified_date_finds_most_recent(tmp_path: Path) -> None:
    older = tmp_path / "older.txt"
    newer = tmp_path / "newer.txt"
    older.write_text("first")
    time.sleep(0.05)  # ensure mtime ordering on filesystems with low resolution
    newer.write_text("second")
    result = latest_modified_date(str(tmp_path))
    assert isinstance(result, datetime)
    # Result should be at-or-after newer's mtime
    assert result.timestamp() >= newer.stat().st_mtime - 0.001
