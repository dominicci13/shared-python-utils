"""Shared logging setup for seller-automation-utils."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from rich.logging import RichHandler

__all__ = ["setup_logging", "SUCCESS"]


# Custom log level between INFO (20) and WARNING (30), used by automations
# to call out "this worked" milestones with visual emphasis.
SUCCESS: int = 25
logging.addLevelName(SUCCESS, "SUCCESS")


def _success(self: logging.Logger, message: str, *args, **kwargs) -> None:
    """Log ``message`` at the custom :data:`SUCCESS` level.

    Mirrors :meth:`logging.Logger.info` so callers can write
    ``log.success("done")`` without thinking about the underlying level
    number.

    Args:
        self (logging.Logger): The logger receiving the call (bound method).
        message (str): The log message; may include ``printf``-style
            substitutions or Rich markup.
        *args: Positional substitution arguments forwarded to
            :meth:`logging.Logger._log`.
        **kwargs: Keyword arguments forwarded to :meth:`logging.Logger._log`
            (e.g. ``exc_info``, ``stack_info``).
    """
    if self.isEnabledFor(SUCCESS):
        self._log(SUCCESS, message, args, **kwargs)


logging.Logger.success = _success  # type: ignore[attr-defined]


# Rich-markup color per level. Used by ``RichLevelFormatter`` to colorize
# the ``[LEVELNAME]`` prefix it prepends to every console line.
_LEVEL_COLORS: dict[str, str] = {
    "DEBUG": "dim",
    "INFO": "cyan",
    "SUCCESS": "green",
    "WARNING": "yellow",
    "ERROR": "red",
    "CRITICAL": "bold red",
}


class RichLevelFormatter(logging.Formatter):
    """Prepend a colored ``[LEVEL]`` tag to every log message.

    Produces output like ``[cyan][INFO][/cyan] message`` so the level is
    immediately visible in the terminal regardless of whether the
    underlying handler shows its own level column. Intended for handlers
    that render Rich markup (e.g. :class:`rich.logging.RichHandler` with
    ``markup=True``); the file handler attached by :func:`setup_logging`
    keeps a plain-text formatter so on-disk logs remain grep-friendly.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` with a ``[color][LEVELNAME][/color] `` prefix.

        Args:
            record (logging.LogRecord): The record being formatted.

        Returns:
            str: The message with a Rich-markup level tag prepended.
        """
        message = super().format(record)
        color = _LEVEL_COLORS.get(record.levelname, "white")
        return f"[{color}][{record.levelname}][/{color}] {message}"


def setup_logging(
    name: str = "automation",
    log_file: str | None = None,
    level: int = logging.INFO,
    max_bytes: int = 1_000_000,
    backup_count: int = 5,
) -> logging.Logger:
    """Configure the root logger with a Rich console handler and a rotating file.

    Calls ``logging.basicConfig`` once with two handlers:

    - A :class:`rich.logging.RichHandler` for the console, with its
      built-in level column suppressed (``show_level=False``) and a
      :class:`RichLevelFormatter` attached so each line begins with a
      colored ``[LEVELNAME]`` tag and any inline Rich markup in the
      message (e.g. ``[cyan]X[/cyan]``) renders normally.
    - A :class:`~logging.handlers.RotatingFileHandler` writing plain-text
      logs to ``log_file`` (``timestamp + levelname + message``) so the
      on-disk log stays easy to search.

    A custom :data:`SUCCESS` level (numeric value ``25``, between ``INFO``
    and ``WARNING``) and a matching ``Logger.success`` method are
    registered at import time, so callers can write
    ``log.success("milestone")`` once they hold a logger.

    Safe to call multiple times in the same process; subsequent calls
    return the existing logger without re-adding handlers.

    Args:
        name (str): Logger name. Also used to derive the default log
            filename (``logs/{name}.log``).
        log_file (str | None): Path to the rotating log file. Defaults to
            ``logs/{name}.log`` and creates the parent directory if missing.
        level (int): Minimum level for both handlers. Defaults to
            ``logging.INFO``.
        max_bytes (int): Rotate the file once it grows past this size.
        backup_count (int): Number of rotated files to keep.

    Returns:
        logging.Logger: A logger named ``name`` ready for ``.info()`` /
            ``.success()`` / ``.warning()`` / ``.error()`` calls.
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

        rich_handler = RichHandler(
            rich_tracebacks=True,
            show_path=False,
            show_level=False,
            markup=True,
        )
        rich_handler.setFormatter(RichLevelFormatter("%(message)s"))

        logging.basicConfig(
            level=level,
            format="%(message)s",
            datefmt="%X",
            handlers=[rich_handler, file_handler],
        )
        root._fc_logging_configured = True

    return logging.getLogger(name)
