"""Shared logging setup for fc-utils automations."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from rich.logging import RichHandler

__all__ = ["setup_logging"]


def setup_logging(
    name: str = "automation",
    log_file: str | None = None,
    level: int = logging.INFO,
    max_bytes: int = 1_000_000,
    backup_count: int = 5,
) -> logging.Logger:
    """Configure the root logger with a Rich console handler and a rotating file.

    Calls ``logging.basicConfig`` once with two handlers — a ``RichHandler``
    for colorized terminal output (with Rich markup like ``[cyan]X[/cyan]``
    rendered, not printed literally), and a ``RotatingFileHandler`` writing
    plain-text logs to ``log_file``. Safe to call multiple times in the same
    process; subsequent calls return the existing logger without re-adding
    handlers.

    Args:
        name (str): Logger name. Also used to derive the default log filename
            (``logs/{name}.log``).
        log_file (str | None): Path to the rotating log file. Defaults to
            ``logs/{name}.log`` and creates the parent directory if missing.
        level (int): Minimum level for both handlers. Defaults to ``INFO``.
        max_bytes (int): Rotate the file once it grows past this size.
        backup_count (int): Number of rotated files to keep.

    Returns:
        logging.Logger: A logger named ``name`` ready for ``.info()`` /
            ``.warning()`` / ``.error()`` calls.
    """
    if log_file is None:
        log_file = f"logs/{name}.log"

    parent = os.path.dirname(log_file)
    if parent:
        os.makedirs(parent, exist_ok=True)

    root = logging.getLogger()
    if not getattr(root, "_fc_logging_configured", False):
        file_handler = RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        ))
        logging.basicConfig(
            level=level,
            format="%(message)s",
            datefmt="%X",
            handlers=[
                RichHandler(rich_tracebacks=True, show_path=False, markup=True),
                file_handler,
            ],
        )
        root._fc_logging_configured = True

    return logging.getLogger(name)
