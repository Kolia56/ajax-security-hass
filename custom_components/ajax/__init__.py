"""The Ajax Security System integration."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .api import AjaxApi, AjaxApiError, AjaxAuthError
from .const import DOMAIN
from .coordinator import AjaxDataCoordinator

if TYPE_CHECKING:
    from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Ajax Security System component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Ajax Security System from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    email = entry.data[CONF_EMAIL]
    password = entry.data[CONF_PASSWORD]
    device_id = entry.entry_id  # Use entry ID as unique device ID

    # Create API instance
    api = AjaxApi(email=email, password=password, device_id=device_id)

    try:
        # Authenticate
        await api.async_login()
        _LOGGER.info("Successfully authenticated with Ajax API")

    except AjaxAuthError as err:
        _LOGGER.error("Authentication failed: %s", err)
        return False
    except AjaxApiError as err:
        _LOGGER.error("API error during setup: %s", err)
        raise ConfigEntryNotReady from err

    # Create coordinator
    coordinator = AjaxDataCoordinator(hass, api)

    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()

    # Store coordinator
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: AjaxDataCoordinator = hass.data[DOMAIN].pop(entry.entry_id)

        # Close API connection
        await coordinator.api.close()

    return unload_ok
