"""Compose a time-aware email greeting like ``Good morning,``.

Centralizes the four sibling repos that hand-rolled the same
"if morning / afternoon / evening" block. Keep this module
behavior-only (no I/O, no global state) so it stays trivially
unit-testable in a future pass.
"""
from __future__ import annotations

from datetime import datetime


def greeting_for(hour: int | None = None) -> str:
    """Return the time-of-day greeting matching the local hour.

    Args:
        hour (int | None): 0-23 hour to evaluate against. Defaults to the
            current local hour when ``None``.

    Returns:
        str: ``"Good morning"`` (05-11), ``"Good afternoon"`` (12-17),
            or ``"Good evening"`` (everything else).

    Raises:
        ValueError: If ``hour`` is supplied and outside 0-23.
    """
    if hour is None:
        hour = datetime.now().hour
    if not 0 <= hour <= 23:
        raise ValueError(f"hour must be 0-23, got {hour!r}")
    if 5 <= hour <= 11:
        return "Good morning"
    if 12 <= hour <= 17:
        return "Good afternoon"
    return "Good evening"
