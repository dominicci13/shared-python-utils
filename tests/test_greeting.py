"""Unit tests for `seller_automation_utils.greeting`."""
from __future__ import annotations

import pytest

from seller_automation_utils.greeting import greeting_for


@pytest.mark.parametrize("hour", [5, 8, 11])
def test_morning_window(hour: int) -> None:
    assert greeting_for(hour) == "Good morning"


@pytest.mark.parametrize("hour", [12, 14, 17])
def test_afternoon_window(hour: int) -> None:
    assert greeting_for(hour) == "Good afternoon"


@pytest.mark.parametrize("hour", [0, 4, 18, 23])
def test_evening_window(hour: int) -> None:
    assert greeting_for(hour) == "Good evening"


@pytest.mark.parametrize("hour", [-1, 24, 100])
def test_invalid_hour_raises(hour: int) -> None:
    with pytest.raises(ValueError, match="hour must be 0-23"):
        greeting_for(hour)


def test_default_hour_returns_valid_greeting() -> None:
    """When no hour is supplied, the function reads the local clock and
    must return one of the three known greetings."""
    assert greeting_for() in {"Good morning", "Good afternoon", "Good evening"}
