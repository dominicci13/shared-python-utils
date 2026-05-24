"""Unit tests for `seller_automation_utils.config_utils.get_env`."""
from __future__ import annotations

import pytest

from seller_automation_utils.config_utils import get_env


def test_returns_set_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SAU_TEST_VAR", "expected")
    assert get_env("SAU_TEST_VAR") == "expected"


def test_returns_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAU_TEST_VAR", raising=False)
    assert get_env("SAU_TEST_VAR", default="fallback") == "fallback"


def test_returns_none_when_unset_without_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAU_TEST_VAR", raising=False)
    assert get_env("SAU_TEST_VAR") is None


def test_required_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SAU_TEST_VAR", raising=False)
    with pytest.raises(ValueError, match="SAU_TEST_VAR"):
        get_env("SAU_TEST_VAR", required=True)


def test_required_set_returns_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """`required=True` must not raise when the variable is set."""
    monkeypatch.setenv("SAU_TEST_VAR", "present")
    assert get_env("SAU_TEST_VAR", required=True) == "present"
