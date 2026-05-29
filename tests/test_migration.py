"""Tests for async_migrate_entry (ConfigEntry schema migrations).

Lifecycle rule: every schema version must have migration coverage so an
upgrade can never silently lose or mis-key a user's config. The v1.1 -> v1.2
step populates the entry unique_id from the e-mail; it must match the
config flow's ``async_set_unique_id(email.lower())`` (lower-cased) so
duplicate detection works, and it must be idempotent.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.ajax import async_migrate_entry
from custom_components.ajax.const import CONF_EMAIL


def _entry(*, version: int, minor_version: int, email: str | None) -> SimpleNamespace:
    data: dict = {}
    if email is not None:
        data[CONF_EMAIL] = email
    return SimpleNamespace(version=version, minor_version=minor_version, data=data)


def _hass() -> MagicMock:
    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    return hass


async def test_migrate_v1_1_to_v1_2_sets_lowercased_unique_id() -> None:
    """A mixed-case e-mail must be stored lower-cased to match the config flow."""
    hass = _hass()
    entry = _entry(version=1, minor_version=1, email="Foo@Bar.COM")

    assert await async_migrate_entry(hass, entry) is True

    hass.config_entries.async_update_entry.assert_called_once()
    _, kwargs = hass.config_entries.async_update_entry.call_args
    assert kwargs["unique_id"] == "foo@bar.com"
    assert kwargs["minor_version"] == 2


async def test_migrate_is_idempotent_when_already_current() -> None:
    """Re-running migration on an already-migrated entry must be a no-op."""
    hass = _hass()
    entry = _entry(version=1, minor_version=2, email="user@example.com")

    assert await async_migrate_entry(hass, entry) is True
    hass.config_entries.async_update_entry.assert_not_called()


async def test_migrate_without_email_yields_none_unique_id() -> None:
    """A missing e-mail must not produce an empty-string unique_id."""
    hass = _hass()
    entry = _entry(version=1, minor_version=1, email=None)

    assert await async_migrate_entry(hass, entry) is True

    _, kwargs = hass.config_entries.async_update_entry.call_args
    assert kwargs["unique_id"] is None
