"""Unit tests for the crash-report DOM capture in `seller_automation_utils.alert_utils`.

Driven by a fake WebDriver rather than a real browser: the behaviour under test is
the frame walk and its failure handling, not Chrome.
"""
from __future__ import annotations

import pytest

from seller_automation_utils import alert_utils
from seller_automation_utils.alert_utils import _collect_dom


class FakeElement:
    def __init__(self, attrs: dict[str, str], child: "FakeFrame | None" = None) -> None:
        self._attrs = attrs
        self.child = child

    def get_attribute(self, name: str) -> str | None:
        return self._attrs.get(name)


class FakeFrame:
    """One browsing context: its own source plus the iframes it contains."""

    def __init__(self, source: str, frames: list["FakeFrame"] | None = None) -> None:
        self.source = source
        self.frames = frames or []


class FakeSwitchTo:
    def __init__(self, driver: "FakeDriver") -> None:
        self._driver = driver

    def frame(self, element: FakeElement) -> None:
        if getattr(element, "explode", False):
            raise RuntimeError("cannot enter frame")
        self._driver.stack.append(element.child)

    def parent_frame(self) -> None:
        if len(self._driver.stack) > 1:
            self._driver.stack.pop()

    def default_content(self) -> None:
        del self._driver.stack[1:]


class FakeDriver:
    def __init__(self, root: FakeFrame, current_url: str = "https://example.test/page") -> None:
        self.root = root
        self.stack: list[FakeFrame] = [root]
        self.current_url = current_url
        self.switch_to = FakeSwitchTo(self)

    @property
    def _here(self) -> FakeFrame:
        return self.stack[-1]

    @property
    def page_source(self) -> str:
        if self._here.source is None:
            raise RuntimeError("page_source unavailable")
        return self._here.source

    def find_elements(self, by: str, value: str) -> list[FakeElement]:
        assert (by, value) == ("tag name", "iframe")
        return [
            FakeElement({"class": f"widget-{i}", "src": f"https://example.test/frame{i}"}, child)
            for i, child in enumerate(self._here.frames)
        ]


def test_captures_main_document() -> None:
    driver = FakeDriver(FakeFrame("<html>top level</html>"))
    dom = _collect_dom(driver)
    assert "MAIN DOCUMENT" in dom
    assert "<html>top level</html>" in dom


def test_descends_into_iframes() -> None:
    """The whole point: content that page_source alone would miss."""
    driver = FakeDriver(FakeFrame(
        "<html>shell</html>",
        [FakeFrame("<html>PSO widget</html>"), FakeFrame("<html>SFP widget</html>")],
    ))
    dom = _collect_dom(driver)
    assert "PSO widget" in dom
    assert "SFP widget" in dom
    assert "iframe[0]" in dom and "iframe[1]" in dom


def test_labels_frames_with_class_and_src() -> None:
    driver = FakeDriver(FakeFrame("<html>shell</html>", [FakeFrame("<html>inner</html>")]))
    dom = _collect_dom(driver)
    assert "widget-0" in dom
    assert "https://example.test/frame0" in dom


def test_descends_into_nested_frames() -> None:
    driver = FakeDriver(FakeFrame(
        "<html>a</html>",
        [FakeFrame("<html>b</html>", [FakeFrame("<html>c</html>")])],
    ))
    dom = _collect_dom(driver)
    assert "<html>c</html>" in dom


def test_respects_max_depth() -> None:
    driver = FakeDriver(FakeFrame(
        "<html>a</html>",
        [FakeFrame("<html>b</html>", [FakeFrame("<html>c</html>")])],
    ))
    dom = _collect_dom(driver, max_depth=1)
    assert "<html>b</html>" in dom
    assert "<html>c</html>" not in dom


def test_unreadable_frame_is_noted_not_fatal() -> None:
    driver = FakeDriver(FakeFrame(
        "<html>shell</html>",
        [FakeFrame(None), FakeFrame("<html>still captured</html>")],
    ))
    dom = _collect_dom(driver)
    assert "unreadable" in dom
    assert "still captured" in dom


def test_frame_that_cannot_be_entered_is_skipped() -> None:
    driver = FakeDriver(FakeFrame("<html>shell</html>", [FakeFrame("<html>inner</html>")]))
    original = driver.find_elements

    def exploding(by: str, value: str) -> list[FakeElement]:
        elements = original(by, value)
        for element in elements:
            element.explode = True
        return elements

    driver.find_elements = exploding
    dom = _collect_dom(driver)
    assert "could not enter" in dom
    assert "<html>shell</html>" in dom


def test_returns_driver_to_default_content() -> None:
    driver = FakeDriver(FakeFrame(
        "<html>a</html>",
        [FakeFrame("<html>b</html>", [FakeFrame("<html>c</html>")])],
    ))
    _collect_dom(driver)
    assert driver.stack == [driver.root]


def test_size_cap_truncates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alert_utils, "MAX_DOM_CHARS", 50)
    driver = FakeDriver(FakeFrame("x" * 200, [FakeFrame("y" * 200)]))
    dom = _collect_dom(driver)
    assert "truncated" in dom
    assert "y" * 200 not in dom


def test_no_frames_still_produces_output() -> None:
    driver = FakeDriver(FakeFrame("<html>alone</html>"))
    assert "<html>alone</html>" in _collect_dom(driver)


def test_write_dom_file_creates_and_headers(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alert_utils.tempfile, "gettempdir", lambda: str(tmp_path))
    driver = FakeDriver(FakeFrame("<html>body</html>", [FakeFrame("<html>framed</html>")]))

    path = alert_utils._write_dom_file(driver, "Account Health", "2026-07-30 11:00:59")

    assert path is not None
    assert path.endswith("Account_Health_crash_dom.txt")
    content = (tmp_path / "Account_Health_crash_dom.txt").read_text(encoding="utf-8")
    assert "Automation: Account Health" in content
    assert "2026-07-30 11:00:59" in content
    assert "https://example.test/page" in content
    assert "<html>framed</html>" in content


def test_write_dom_file_survives_a_broken_driver(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A capture failure must never take down the crash alert itself."""
    monkeypatch.setattr(alert_utils.tempfile, "gettempdir", lambda: str(tmp_path))

    class BrokenDriver:
        @property
        def current_url(self) -> str:
            raise RuntimeError("dead session")

        @property
        def page_source(self) -> str:
            raise RuntimeError("dead session")

        def find_elements(self, by: str, value: str) -> list:
            raise RuntimeError("dead session")

        class switch_to:  # noqa: N801 - mirrors the Selenium attribute name
            @staticmethod
            def default_content() -> None:
                raise RuntimeError("dead session")

    assert alert_utils._write_dom_file(BrokenDriver(), "Broken", "2026-07-30 11:00:59") is None
