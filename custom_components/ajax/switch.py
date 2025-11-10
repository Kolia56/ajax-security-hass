"""Ajax switch platform.

This module creates switch entities for:
- Ajax Smart Sockets
- Ajax Relays
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import (
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AjaxDataCoordinator
from .models import AjaxDevice, DeviceType

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax switch platform."""
    coordinator: AjaxDataCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []

    # Create switches for all spaces
    for space_id, space in coordinator.account.spaces.items():
        # Create switch entities for sockets and relays
        for device_id, device in space.devices.items():
            if device.type in [DeviceType.SOCKET, DeviceType.RELAY]:
                # Check if device has channel data
                if "channel" in device.attributes:
                    entities.append(
                        AjaxSwitch(
                            coordinator=coordinator,
                            space_id=space_id,
                            device_id=device_id,
                        )
                    )
                    _LOGGER.debug(
                        "Created switch for device: %s (type: %s)",
                        device.name,
                        device.type.value,
                    )

    async_add_entities(entities)


class AjaxSwitch(CoordinatorEntity[AjaxDataCoordinator], SwitchEntity):
    """Representation of an Ajax Switch."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        space_id: str,
        device_id: str,
    ) -> None:
        """Initialize the Ajax switch."""
        super().__init__(coordinator)
        self._space_id = space_id
        self._device_id = device_id

        # Get device info
        device = self.coordinator.account.spaces[space_id].devices[device_id]

        # Set unique ID
        self._attr_unique_id = f"{device_id}_switch"

        # Set device info
        self._attr_device_info = coordinator.get_device_info(space_id, device_id)

        # Set name (will use device name as base due to has_entity_name=True)
        self._attr_translation_key = "socket"

    @property
    def is_on(self) -> bool:
        """Return true if switch is on."""
        device = self._get_device()
        if device and "channel" in device.attributes:
            channel = device.attributes["channel"]
            return channel.get("is_on", False)
        return False

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        device = self._get_device()
        if not device:
            return False

        # Check if channel is enabled
        if "channel" in device.attributes:
            channel = device.attributes["channel"]
            return channel.get("is_enabled", True)

        return True

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        device = self._get_device()
        if not device:
            _LOGGER.error("Device %s not found", self._device_id)
            return

        try:
            # Get channel ID from attributes
            channel_id = 1
            if "channel" in device.attributes:
                channel_id = device.attributes["channel"].get("channel_id", 1)

            # Get hub ID
            hub_id = device.hub_id

            # Call API to turn on
            await self.coordinator.api_client.async_turn_on_device(
                space_id=self._space_id,
                device_id=self._device_id,
                hub_id=hub_id,
                channel_id=channel_id,
            )

            # Request immediate refresh
            await self.coordinator.async_request_refresh()

        except Exception as err:
            _LOGGER.error("Error turning on switch %s: %s", self._device_id, err)
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        device = self._get_device()
        if not device:
            _LOGGER.error("Device %s not found", self._device_id)
            return

        try:
            # Get channel ID from attributes
            channel_id = 1
            if "channel" in device.attributes:
                channel_id = device.attributes["channel"].get("channel_id", 1)

            # Get hub ID
            hub_id = device.hub_id

            # Call API to turn off
            await self.coordinator.api_client.async_turn_off_device(
                space_id=self._space_id,
                device_id=self._device_id,
                hub_id=hub_id,
                channel_id=channel_id,
            )

            # Request immediate refresh
            await self.coordinator.async_request_refresh()

        except Exception as err:
            _LOGGER.error("Error turning off switch %s: %s", self._device_id, err)
            raise

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    def _get_device(self) -> AjaxDevice | None:
        """Get the device from coordinator data."""
        space = self.coordinator.account.spaces.get(self._space_id)
        if not space:
            return None
        return space.devices.get(self._device_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional state attributes."""
        device = self._get_device()
        if not device or "channel" not in device.attributes:
            return {}

        channel = device.attributes["channel"]
        attributes = {
            "channel_id": channel.get("channel_id"),
            "output_mode": channel.get("output_mode"),
            "operating_mode": channel.get("operating_mode"),
        }

        return {k: v for k, v in attributes.items() if v is not None}
