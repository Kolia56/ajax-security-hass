"""Ajax alarm control panel platform."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AjaxDataCoordinator
from .models import SecurityState, GroupState

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax alarm control panels from a config entry."""
    coordinator: AjaxDataCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []

    if coordinator.account:
        for space_id, space in coordinator.account.spaces.items():
            # Create main alarm control panel for the space (hub)
            entities.append(AjaxAlarmControlPanel(coordinator, entry, space_id))

            # Create alarm control panel for each group/zone
            if space.groups:
                for group_id, group in space.groups.items():
                    entities.append(
                        AjaxGroupAlarmControlPanel(coordinator, entry, space_id, group_id)
                    )
                _LOGGER.info(
                    "Created %d group alarm panel(s) for space %s",
                    len(space.groups),
                    space.name,
                )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax alarm control panel(s)", len(entities))
    else:
        _LOGGER.info("No Ajax spaces found, no alarm panels created (yet)")


class AjaxAlarmControlPanel(CoordinatorEntity[AjaxDataCoordinator], AlarmControlPanelEntity):
    """Representation of an Ajax alarm control panel (one per space/hub)."""

    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_NIGHT
    )
    _attr_code_arm_required = False

    def __init__(
        self, coordinator: AjaxDataCoordinator, entry: ConfigEntry, space_id: str
    ) -> None:
        """Initialize the alarm control panel."""
        super().__init__(coordinator)
        self._entry = entry
        self._space_id = space_id

        # Get initial space data
        space = coordinator.get_space(space_id)
        space_name = space.name if space else "Unknown"

        self._attr_name = f"Ajax Alarm - {space_name}"
        self._attr_unique_id = f"{entry.entry_id}_alarm_{space_id}"

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return {}

        return {
            "identifiers": {(DOMAIN, self._space_id)},
            "name": f"Ajax Hub - {space.name}",
            "manufacturer": "Ajax Systems",
            "model": "Security Hub",
        }

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        """Return the state of the alarm."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return None

        # Map Ajax SecurityState to Home Assistant AlarmControlPanelState
        state_map = {
            SecurityState.DISARMED: AlarmControlPanelState.DISARMED,
            SecurityState.ARMED: AlarmControlPanelState.ARMED_AWAY,
            SecurityState.NIGHT_MODE: AlarmControlPanelState.ARMED_NIGHT,
            SecurityState.PARTIALLY_ARMED: AlarmControlPanelState.ARMED_HOME,
            SecurityState.AWAITING_EXIT_TIMER: AlarmControlPanelState.ARMING,
            SecurityState.AWAITING_CONFIRMATION: AlarmControlPanelState.PENDING,
            SecurityState.ARMING_INCOMPLETE: AlarmControlPanelState.ARMING,
        }

        return state_map.get(space.security_state, AlarmControlPanelState.DISARMED)

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Send disarm command."""
        _LOGGER.info("Disarming Ajax alarm for space %s", self._space_id)

        try:
            await self.coordinator.async_disarm_space(self._space_id)
        except Exception as err:
            _LOGGER.error("Failed to disarm: %s", err)
            raise

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Send arm away command."""
        _LOGGER.info("Arming Ajax alarm (away) for space %s", self._space_id)

        try:
            await self.coordinator.async_arm_space(self._space_id)
        except Exception as err:
            _LOGGER.error("Failed to arm: %s", err)
            raise

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        """Send arm night command."""
        _LOGGER.info("Arming Ajax alarm (night) for space %s", self._space_id)

        try:
            await self.coordinator.async_arm_night_mode(self._space_id)
        except Exception as err:
            _LOGGER.error("Failed to arm night mode: %s", err)
            raise

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return {}

        attributes = {
            "space_id": space.id,
            "space_name": space.name,
            "hub_id": space.hub_id,
            "unread_notifications": space.unread_notifications,
            "total_devices": len(space.devices),
            "online_devices": len(space.get_online_devices()),
            "devices_with_malfunctions": len(space.get_devices_with_malfunctions()),
            "bypassed_devices": len(space.get_bypassed_devices()),
        }

        # Add room information
        if space.rooms:
            attributes["rooms"] = {
                room_id: {
                    "name": room.name,
                    "device_count": len(room.device_ids),
                }
                for room_id, room in space.rooms.items()
            }

        return attributes

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return {}

        return {
            "identifiers": {(DOMAIN, self._space_id)},
            "name": f"Ajax Hub - {space.name}",
            "manufacturer": "Ajax Systems",
            "model": "Security Hub",
            "sw_version": None,  # TODO: Add hub firmware version when available
        }


class AjaxGroupAlarmControlPanel(CoordinatorEntity[AjaxDataCoordinator], AlarmControlPanelEntity):
    """Representation of an Ajax group/zone alarm control panel."""

    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_NIGHT
    )
    _attr_code_arm_required = False

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        entry: ConfigEntry,
        space_id: str,
        group_id: str,
    ) -> None:
        """Initialize the group alarm control panel."""
        super().__init__(coordinator)
        self._entry = entry
        self._space_id = space_id
        self._group_id = group_id

        # Get initial group data
        space = coordinator.get_space(space_id)
        if space and group_id in space.groups:
            group = space.groups[group_id]
            self._attr_name = f"Ajax Alarm - {group.name}"
            self._attr_unique_id = f"{entry.entry_id}_alarm_group_{group_id}"
        else:
            self._attr_name = f"Ajax Alarm - Group {group_id}"
            self._attr_unique_id = f"{entry.entry_id}_alarm_group_{group_id}"

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return {}

        return {
            "identifiers": {(DOMAIN, self._space_id)},
            "name": f"Ajax Hub - {space.name}",
            "manufacturer": "Ajax Systems",
            "model": "Security Hub",
        }

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        """Return the state of the alarm for this group."""
        space = self.coordinator.get_space(self._space_id)
        if not space or self._group_id not in space.groups:
            return None

        group = space.groups[self._group_id]

        # Map GroupState to AlarmControlPanelState
        if group.state == GroupState.ARMED:
            # Check if night mode is enabled for this group
            if group.night_mode_enabled:
                return AlarmControlPanelState.ARMED_NIGHT
            else:
                return AlarmControlPanelState.ARMED_AWAY
        elif group.state == GroupState.DISARMED:
            return AlarmControlPanelState.DISARMED
        else:
            return AlarmControlPanelState.DISARMED

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Send disarm command for this group."""
        _LOGGER.info("Disarming Ajax group %s in space %s", self._group_id, self._space_id)

        try:
            await self.coordinator.async_disarm_group(self._space_id, self._group_id)
        except Exception as err:
            _LOGGER.error("Failed to disarm group: %s", err)
            raise

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Send arm away command for this group."""
        _LOGGER.info("Arming Ajax group %s (away) in space %s", self._group_id, self._space_id)

        try:
            await self.coordinator.async_arm_group(self._space_id, self._group_id)
        except Exception as err:
            _LOGGER.error("Failed to arm group: %s", err)
            raise

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        """Send arm night command for this group."""
        _LOGGER.info("Arming Ajax group %s (night) in space %s", self._group_id, self._space_id)

        try:
            # Arm the group (night mode is handled by the group's night_mode_enabled property)
            await self.coordinator.async_arm_group(self._space_id, self._group_id)
        except Exception as err:
            _LOGGER.error("Failed to arm group in night mode: %s", err)
            raise

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        space = self.coordinator.get_space(self._space_id)
        if not space or self._group_id not in space.groups:
            return {}

        group = space.groups[self._group_id]

        attributes = {
            "group_id": group.id,
            "group_name": group.name,
            "space_id": space.id,
            "space_name": space.name,
            "night_mode_enabled": group.night_mode_enabled,
            "bulk_arm_involved": group.bulk_arm_involved,
            "bulk_disarm_involved": group.bulk_disarm_involved,
            "device_count": len(group.device_ids),
        }

        return attributes
