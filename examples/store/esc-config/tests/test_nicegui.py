"""Tests for the nicegui example.

The settings contract is the same idea as the worker's: the app reads the env
vars compose.yaml declares. There is also a smoke test that the pages build
without starting a server.
"""

from __future__ import annotations

import pytest

from esc_config import nicegui_app
from esc_config.nicegui_app import Settings


def test_defaults() -> None:
    assert Settings().greeting


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESC_CONFIG_GREETING", "hi there")
    assert Settings().greeting == "hi there"


def test_pages_build_without_a_server() -> None:
    # build() registers the routes; it must not need ui.run() (and a real port)
    # to be importable and constructible, which is what lets us test it at all.
    nicegui_app.build(Settings())
