"""Local pytest fixtures for Ajax tests."""

from __future__ import annotations

from collections.abc import Generator

import pytest


@pytest.fixture(autouse=True, scope="session")
def mock_zeroconf_resolver() -> Generator[None]:
    """Override HA's pycares resolver fixture, which hangs during teardown here."""
    yield


@pytest.fixture(autouse=True)
def enable_event_loop_debug() -> None:
    """Override HA fixture; these tests do not need an event loop."""


@pytest.fixture(autouse=True)
def verify_cleanup() -> Generator[None]:
    """Override HA cleanup fixture for non-HA translation tests."""
    yield
