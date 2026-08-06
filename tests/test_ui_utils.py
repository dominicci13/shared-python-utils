"""Tests for the ``FC_NO_PROMPT`` escape hatch on ``ask_user``.

Every entry point in the fleet blocks on ``ask_user`` before reaching its
scheduler. Without this guard an automation started by the fleet-control
supervisor would hang on a Windows dialog nobody is looking at, while the
dashboard reported it as running.
"""
from __future__ import annotations

import pytest

from seller_automation_utils import ui_utils


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv(ui_utils.NO_PROMPT_ENV, raising=False)


def test_no_prompt_skips_dialog_entirely(monkeypatch):
    """The MessageBoxW call must not happen at all, not merely be answered."""
    def fail_if_called(*args, **kwargs):
        raise AssertionError("MessageBoxW was called despite FC_NO_PROMPT")

    monkeypatch.setenv(ui_utils.NO_PROMPT_ENV, "1")
    monkeypatch.setattr(ui_utils.ctypes, "windll", _FakeWindll(fail_if_called), raising=False)

    assert ui_utils.ask_user("Run now?", "Demo") is False


def test_prompt_shown_when_env_absent(monkeypatch):
    calls = []

    monkeypatch.setattr(
        ui_utils.ctypes, "windll",
        _FakeWindll(lambda *a, **k: (calls.append(a), 6)[1]),
        raising=False,
    )

    assert ui_utils.ask_user("Run now?", "Demo") is True
    assert len(calls) == 1


def test_user_declining_returns_false(monkeypatch):
    monkeypatch.setattr(ui_utils.ctypes, "windll", _FakeWindll(lambda *a, **k: 7), raising=False)
    assert ui_utils.ask_user("Run now?", "Demo") is False


def test_empty_env_value_still_prompts(monkeypatch):
    """FC_NO_PROMPT="" is an unset variable, not an opt-in."""
    monkeypatch.setenv(ui_utils.NO_PROMPT_ENV, "")
    monkeypatch.setattr(ui_utils.ctypes, "windll", _FakeWindll(lambda *a, **k: 6), raising=False)
    assert ui_utils.ask_user("Run now?", "Demo") is True


class _FakeWindll:
    """Stands in for ``ctypes.windll`` so these run without a real message pump."""

    def __init__(self, message_box):
        self.user32 = type("_User32", (), {"MessageBoxW": staticmethod(message_box)})()
