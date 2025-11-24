"""Ajax sensor platform.

This module creates sensors for:
- Space-level statistics (device counts, notifications, etc.)
- Device-level measurements (battery, signal, temperature, etc.)
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, get_event_message
from .coordinator import AjaxDataCoordinator
from .models import AjaxDevice, AjaxSpace, DeviceType

_LOGGER = logging.getLogger(__name__)


# ==============================================================================
# Helper Functions
# ==============================================================================


def get_device_color_name(color_code: int | None) -> str | None:
    """Convert Ajax device color code to human-readable name."""
    if color_code is None:
        return None

    color_map = {
        1: "White",
        2: "Black",
    }

    return color_map.get(color_code, f"Unknown ({color_code})")


def format_timezone(tz_string: str | None) -> str | None:
    """Format timezone string to be more readable.

    Converts formats like:
    - EUROPE_PARIS -> Europe/Paris
    - AMERICA_NEW_YORK -> America/New_York
    """
    if not tz_string:
        return None

    # Replace underscores with slashes and title case each part
    parts = tz_string.split("_")
    if len(parts) >= 2:
        # First part is region (Europe, America, etc.)
        region = parts[0].title()
        # Rest is city (may contain underscores)
        city = "_".join(parts[1:]).replace("_", " ").title().replace(" ", "_")
        return f"{region}/{city}"

    return tz_string


def format_hub_type(hub_type: str | None) -> str | None:
    """Format hub type string to be more readable.

    Converts formats like:
    - HUB_2_PLUS -> Hub 2 Plus
    - HUB_HYBRID_2G -> Hub Hybrid 2G
    """
    if not hub_type:
        return None

    # Replace underscores with spaces and title case
    return hub_type.replace("_", " ").title()


def format_signal_level(signal: str | None) -> str | None:
    """Format signal level string to be more readable.

    Converts formats like:
    - STRONG -> Fort
    - WEAK -> Faible
    - MEDIUM -> Moyen
    """
    if not signal:
        return None

    signal_map = {
        "STRONG": "Fort",
        "WEAK": "Faible",
        "MEDIUM": "Moyen",
        "EXCELLENT": "Excellent",
        "GOOD": "Bon",
        "POOR": "Mauvais",
    }

    return signal_map.get(signal.upper(), signal.title())


# ==============================================================================
# Sensor Descriptions
# ==============================================================================


@dataclass
class AjaxSpaceSensorDescription(SensorEntityDescription):
    """Description for Ajax space-level sensors."""

    value_fn: Callable[[AjaxSpace], Any] | None = None
    should_create: Callable[[AjaxSpace], bool] | None = None
    entity_category: EntityCategory | None = None


@dataclass
class AjaxDeviceSensorDescription(SensorEntityDescription):
    """Description for Ajax device-level sensors."""

    value_fn: Callable[[AjaxDevice], Any] | None = None
    should_create: Callable[[AjaxDevice], bool] | None = None
    enabled_by_default: bool = True
    extra_attributes_fn: Callable[[AjaxDevice], dict[str, Any]] | None = None


# Space-level sensor descriptions
SPACE_SENSORS: tuple[AjaxSpaceSensorDescription, ...] = (
    AjaxSpaceSensorDescription(
        key="total_devices",
        translation_key="total_devices",
        icon="mdi:devices",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: len(space.devices),
    ),
    AjaxSpaceSensorDescription(
        key="online_devices",
        translation_key="online_devices",
        icon="mdi:check-network",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: len(space.get_online_devices()),
    ),
    AjaxSpaceSensorDescription(
        key="devices_with_malfunctions",
        translation_key="devices_with_malfunctions",
        icon="mdi:alert-circle",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: len(space.get_devices_with_malfunctions()),
    ),
    AjaxSpaceSensorDescription(
        key="unread_notifications",
        translation_key="unread_notifications",
        icon="mdi:bell-badge",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: space.unread_notifications,
    ),
    AjaxSpaceSensorDescription(
        key="bypassed_devices",
        translation_key="bypassed_devices",
        icon="mdi:shield-off",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: len(space.get_bypassed_devices()),
    ),
    # Hub hardware information
    AjaxSpaceSensorDescription(
        key="hub_battery",
        translation_key="hub_battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda space: space.hub_details.get("battery", {}).get("chargeLevelPercentage") if space.hub_details else None,
    ),
    # Network - Ethernet
    AjaxSpaceSensorDescription(
        key="hub_ethernet_ip",
        translation_key="hub_ethernet_ip",
        icon="mdi:ethernet",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: space.hub_details.get("ethernet", {}).get("ip") if space.hub_details and space.hub_details.get("ethernet", {}).get("enabled") else None,
    ),
    # Network - WiFi
    AjaxSpaceSensorDescription(
        key="hub_wifi_ssid",
        translation_key="hub_wifi_ssid",
        icon="mdi:wifi",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: space.hub_details.get("wifi", {}).get("ssid") if space.hub_details and space.hub_details.get("wifi", {}).get("enabled") else None,
        should_create=lambda space: space.hub_details and space.hub_details.get("wifi", {}).get("enabled", False),
    ),
    AjaxSpaceSensorDescription(
        key="hub_wifi_signal",
        translation_key="hub_wifi_signal",
        icon="mdi:wifi-strength-3",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: format_signal_level(space.hub_details.get("wifi", {}).get("signalLevel")) if space.hub_details and space.hub_details.get("wifi", {}).get("enabled") else None,
        should_create=lambda space: space.hub_details and space.hub_details.get("wifi", {}).get("enabled", False),
    ),
    AjaxSpaceSensorDescription(
        key="hub_wifi_ip",
        translation_key="hub_wifi_ip",
        icon="mdi:wifi",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: space.hub_details.get("wifi", {}).get("ip") if space.hub_details and space.hub_details.get("wifi", {}).get("enabled") else None,
        should_create=lambda space: space.hub_details and space.hub_details.get("wifi", {}).get("enabled", False),
    ),
    # Network - GSM
    AjaxSpaceSensorDescription(
        key="hub_gsm_signal",
        translation_key="hub_gsm_signal",
        icon="mdi:signal",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: format_signal_level(space.hub_details.get("gsm", {}).get("signalLevel")) if space.hub_details else None,
        should_create=lambda space: space.hub_details and space.hub_details.get("gsm") is not None,
    ),
    AjaxSpaceSensorDescription(
        key="hub_gsm_network",
        translation_key="hub_gsm_network",
        icon="mdi:signal-cellular-3",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: space.hub_details.get("gsm", {}).get("networkStatus") if space.hub_details else None,
        should_create=lambda space: space.hub_details and space.hub_details.get("gsm") is not None,
    ),
    # System info
    AjaxSpaceSensorDescription(
        key="hub_led_brightness",
        translation_key="hub_led_brightness",
        icon="mdi:brightness-6",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: space.hub_details.get("ledBrightnessLevel") if space.hub_details else None,
    ),
    AjaxSpaceSensorDescription(
        key="hub_timezone",
        translation_key="hub_timezone",
        icon="mdi:clock-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: format_timezone(space.hub_details.get("timeZone")) if space.hub_details else None,
    ),
    # SIM Cards
    AjaxSpaceSensorDescription(
        key="hub_active_sim",
        translation_key="hub_active_sim",
        icon="mdi:sim",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: (
            f"SIM {space.hub_details.get('gsm', {}).get('activeSimCard', 0) + 1}/{len(space.hub_details.get('gsm', {}).get('simCards', []))}"
            if space.hub_details and space.hub_details.get('gsm') and space.hub_details.get('gsm', {}).get('simCards')
            else None
        ),
        should_create=lambda space: space.hub_details and space.hub_details.get('gsm') and space.hub_details.get('gsm', {}).get('simCards'),
    ),
    AjaxSpaceSensorDescription(
        key="hub_sim1_apn",
        translation_key="hub_sim1_apn",
        icon="mdi:sim",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: (
            space.hub_details.get('gsm', {}).get('simCards', [{}])[0].get('apn') or "Non configuré"
            if space.hub_details
            and space.hub_details.get('gsm', {}).get('simCards')
            and len(space.hub_details.get('gsm', {}).get('simCards', [])) > 0
            else None
        ),
        should_create=lambda space: (
            space.hub_details
            and space.hub_details.get('gsm', {}).get('simCards')
            and len(space.hub_details.get('gsm', {}).get('simCards', [])) > 0
        ),
    ),
    AjaxSpaceSensorDescription(
        key="hub_sim2_apn",
        translation_key="hub_sim2_apn",
        icon="mdi:sim",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: (
            space.hub_details.get('gsm', {}).get('simCards', [{}, {}])[1].get('apn') or "Non configuré"
            if space.hub_details
            and space.hub_details.get('gsm', {}).get('simCards')
            and len(space.hub_details.get('gsm', {}).get('simCards', [])) > 1
            else None
        ),
        should_create=lambda space: (
            space.hub_details
            and space.hub_details.get('gsm', {}).get('simCards')
            and len(space.hub_details.get('gsm', {}).get('simCards', [])) > 1
            and space.hub_details.get('gsm', {}).get('simCards', [{}, {}])[1].get('lastTrafficResetTimestamp', 0) > 0  # Only if SIM card has been used
        ),
    ),
    AjaxSpaceSensorDescription(
        key="hub_sim1_traffic",
        translation_key="hub_sim1_traffic",
        icon="mdi:swap-vertical",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: (
            f"↑{space.hub_details.get('gsm', {}).get('simCards', [{}])[0].get('trafficTxKb', 0)} Ko / ↓{space.hub_details.get('gsm', {}).get('simCards', [{}])[0].get('trafficRxKb', 0)} Ko"
            if space.hub_details
            and space.hub_details.get('gsm', {}).get('simCards')
            and len(space.hub_details.get('gsm', {}).get('simCards', [])) > 0
            else None
        ),
        should_create=lambda space: (
            space.hub_details
            and space.hub_details.get('gsm', {}).get('simCards')
            and len(space.hub_details.get('gsm', {}).get('simCards', [])) > 0
        ),
    ),
    AjaxSpaceSensorDescription(
        key="hub_sim2_traffic",
        translation_key="hub_sim2_traffic",
        icon="mdi:swap-vertical",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda space: (
            f"↑{space.hub_details.get('gsm', {}).get('simCards', [{}, {}])[1].get('trafficTxKb', 0)} Ko / ↓{space.hub_details.get('gsm', {}).get('simCards', [{}, {}])[1].get('trafficRxKb', 0)} Ko"
            if space.hub_details
            and space.hub_details.get('gsm', {}).get('simCards')
            and len(space.hub_details.get('gsm', {}).get('simCards', [])) > 1
            else None
        ),
        should_create=lambda space: (
            space.hub_details
            and space.hub_details.get('gsm', {}).get('simCards')
            and len(space.hub_details.get('gsm', {}).get('simCards', [])) > 1
            and space.hub_details.get('gsm', {}).get('simCards', [{}, {}])[1].get('lastTrafficResetTimestamp', 0) > 0  # Only if SIM card has been used
        ),
    ),
)

# Device-level sensor descriptions
DEVICE_SENSORS: tuple[AjaxDeviceSensorDescription, ...] = (
    AjaxDeviceSensorDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.battery_level,
        should_create=lambda device: device.has_battery,
        enabled_by_default=True,
    ),
    AjaxDeviceSensorDescription(
        key="signal_strength",
        translation_key="signal_strength",
        icon="mdi:signal",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.signal_strength,
        should_create=lambda device: device.signal_strength is not None,
        enabled_by_default=True,
    ),
    AjaxDeviceSensorDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.attributes.get("temperature"),
        should_create=lambda device: device.type == DeviceType.TEMPERATURE_SENSOR
        or "temperature" in device.attributes,
        enabled_by_default=True,
    ),
    AjaxDeviceSensorDescription(
        key="humidity",
        translation_key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.attributes.get("humidity"),
        should_create=lambda device: "humidity" in device.attributes,
        enabled_by_default=True,
    ),
    AjaxDeviceSensorDescription(
        key="co2",
        translation_key="co2",
        device_class=SensorDeviceClass.CO2,
        native_unit_of_measurement="ppm",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.attributes.get("co2"),
        should_create=lambda device: "co2" in device.attributes,
        enabled_by_default=True,
    ),
    AjaxDeviceSensorDescription(
        key="firmware_version",
        translation_key="firmware_version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.firmware_version,
        should_create=lambda device: device.firmware_version is not None,
        enabled_by_default=True,
    ),
    AjaxDeviceSensorDescription(
        key="hardware_version",
        translation_key="hardware_version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.hardware_version,
        should_create=lambda device: device.hardware_version is not None,
        enabled_by_default=True,
    ),
)

# Hub-specific sensor descriptions
HUB_SENSORS: tuple[AjaxDeviceSensorDescription, ...] = (
    AjaxDeviceSensorDescription(
        key="sim_status",
        translation_key="sim_status",
        icon="mdi:sim",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.attributes.get("sim_status"),
        should_create=lambda device: (
            "sim_status" in device.attributes
            and device.attributes.get("sim_slots_used", 0) > 0
        ),
        enabled_by_default=True,
        extra_attributes_fn=lambda device: {
            "sim_slots_total": device.attributes.get("sim_slots_total", 0),
            "sim_slots_used": device.attributes.get("sim_slots_used", 0),
            "sim_cards": device.attributes.get("sim_cards", []),
        } if "sim_cards" in device.attributes else {},
    ),
    AjaxDeviceSensorDescription(
        key="gsm_type",
        translation_key="gsm_type",
        icon="mdi:signal",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.attributes.get("gsm_type"),
        should_create=lambda device: (
            "gsm_type" in device.attributes
            and device.attributes.get("sim_slots_used", 0) > 0
        ),
        enabled_by_default=True,
    ),
    AjaxDeviceSensorDescription(
        key="gsm_connection_status",
        translation_key="gsm_connection_status",
        icon="mdi:access-point-network",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.attributes.get("gsm_connection_status"),
        should_create=lambda device: (
            "gsm_connection_status" in device.attributes
            and device.attributes.get("sim_slots_used", 0) > 0
        ),
        enabled_by_default=True,
        extra_attributes_fn=lambda device: device.attributes.get("gsm_info", {}),
    ),
)

# Additional device sensor descriptions (metadata, etc.)
DEVICE_METADATA_SENSORS: tuple[AjaxDeviceSensorDescription, ...] = (
    AjaxDeviceSensorDescription(
        key="device_color",
        translation_key="device_color",
        icon="mdi:palette",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: get_device_color_name(device.device_color),
        should_create=lambda device: device.device_color is not None,
        enabled_by_default=False,
    ),
    AjaxDeviceSensorDescription(
        key="device_label",
        translation_key="device_label",
        icon="mdi:label",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.device_label,
        should_create=lambda device: device.device_label is not None,
        enabled_by_default=False,
    ),
    AjaxDeviceSensorDescription(
        key="device_marketing_id",
        translation_key="device_marketing_id",
        icon="mdi:identifier",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.device_marketing_id,
        should_create=lambda device: device.device_marketing_id is not None,
        enabled_by_default=False,
    ),
    AjaxDeviceSensorDescription(
        key="device_state",
        translation_key="device_state",
        icon="mdi:state-machine",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda device: device.states[0] if device.states else "PASSIVE",
        should_create=lambda device: True,
        enabled_by_default=False,
    ),
    AjaxDeviceSensorDescription(
        key="malfunctions",
        translation_key="malfunctions",
        icon="mdi:alert-circle",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.malfunctions,
        should_create=lambda device: True,
        enabled_by_default=True,
    ),
)


# ==============================================================================
# Setup
# ==============================================================================


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax sensors from a config entry."""
    coordinator: AjaxDataCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []

    if not coordinator.account:
        _LOGGER.warning("No Ajax account found, no sensors created")
        return

    # Create space-level sensors for each space (hub)
    for space_id, space in coordinator.account.spaces.items():
        space_sensor_count = 0
        for description in SPACE_SENSORS:
            # Check if sensor should be created
            if description.should_create and not description.should_create(space):
                continue
            entities.append(
                AjaxSpaceSensor(coordinator, entry, space_id, description)
            )
            space_sensor_count += 1
        _LOGGER.debug(
            "Created %d space-level sensors for space '%s'",
            space_sensor_count,
            space.name,
        )

    # Create device-level sensors for each device
    for space_id, space in coordinator.account.spaces.items():
        for device_id, device in space.devices.items():
            # Regular device sensors
            for description in DEVICE_SENSORS:
                if description.should_create and description.should_create(device):
                    entities.append(
                        AjaxDeviceSensor(
                            coordinator, entry, space_id, device_id, description
                        )
                    )

            # Hub-specific sensors
            if device.type == DeviceType.HUB:
                for description in HUB_SENSORS:
                    if description.should_create and description.should_create(device):
                        entities.append(
                            AjaxDeviceSensor(
                                coordinator, entry, space_id, device_id, description
                            )
                        )

            # Device metadata sensors (color, label, marketing ID)
            for description in DEVICE_METADATA_SENSORS:
                if description.should_create and description.should_create(device):
                    entities.append(
                        AjaxDeviceSensor(
                            coordinator, entry, space_id, device_id, description
                        )
                    )

            _LOGGER.debug(
                "Created sensors for device '%s' (type: %s)",
                device.name,
                device.type.value,
            )

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ajax sensor(s)", len(entities))
    else:
        _LOGGER.warning("No Ajax sensors created")


# ==============================================================================
# Space-level Sensors
# ==============================================================================


class AjaxSpaceSensor(CoordinatorEntity[AjaxDataCoordinator], SensorEntity):
    """Representation of an Ajax space-level sensor (statistics about the space/hub)."""

    entity_description: AjaxSpaceSensorDescription

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        entry: ConfigEntry,
        space_id: str,
        description: AjaxSpaceSensorDescription,
    ) -> None:
        """Initialize the space sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._space_id = space_id
        self._entry = entry

        # Get initial space data
        space = coordinator.get_space(space_id)
        space_name = space.name if space else "Unknown"

        # Set entity attributes - use translation_key instead of hardcoded name
        self._attr_has_entity_name = True
        self._attr_translation_key = description.translation_key
        self._attr_unique_id = f"{entry.entry_id}_{space_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        space = self.coordinator.get_space(self._space_id)
        if not space or not self.entity_description.value_fn:
            return None

        return self.entity_description.value_fn(space)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return {}

        device_info = {
            "identifiers": {(DOMAIN, self._space_id)},
            "name": f"Ajax Hub - {space.name}",
            "manufacturer": "Ajax Systems",
            "model": format_hub_type(space.hub_details.get("hubSubtype")) if space.hub_details else "Security Hub",
        }

        # Add firmware version if available
        if space.hub_details and space.hub_details.get("firmware"):
            firmware = space.hub_details["firmware"]
            if firmware.get("version"):
                device_info["sw_version"] = firmware["version"]

        return device_info

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        space = self.coordinator.get_space(self._space_id)
        if not space:
            return {}

        attributes = {
            "space_id": space.id,
            "space_name": space.name,
            "hub_id": space.hub_id,
        }

        # Add detailed info for last_alert sensor
        if self.entity_description.key == "last_alert" and space.notifications:
            # Get the latest alert/security event
            for notification in space.notifications:
                if notification.type.value in ["alarm", "security_event"]:
                    # Get room name if available
                    room_name = None
                    if notification.device_id and notification.device_id in space.devices:
                        device = space.devices[notification.device_id]
                        if device.room_id and device.room_id in space.rooms:
                            room = space.rooms[device.room_id]
                            room_name = room.name
                            attributes["room_name"] = room.name
                            attributes["room_id"] = room.id

                    # Format message like the Ajax app
                    # Get language from Home Assistant (default to English)
                    language = self.hass.config.language if self.hass.config.language in ["en", "fr"] else "en"

                    # Create human-readable message
                    formatted_message = get_event_message(
                        notification.title,
                        language=language,
                        device_name=notification.device_name,
                        room_name=room_name,
                    )

                    attributes["event_type"] = notification.title  # Keep raw event type
                    attributes["device_name"] = notification.device_name
                    attributes["device_id"] = notification.device_id
                    attributes["notification_type"] = notification.type.value
                    attributes["message"] = formatted_message  # Use formatted message
                    attributes["raw_message"] = notification.message  # Keep original
                    if notification.user_name:
                        attributes["user_name"] = notification.user_name
                    break

        # Add room breakdown for device-related sensors
        if "device" in self.entity_description.key and space.rooms:
            attributes["rooms"] = {
                room_id: {
                    "name": room.name,
                    "device_count": len(room.device_ids),
                }
                for room_id, room in space.rooms.items()
            }

        return attributes


# ==============================================================================
# Device-level Sensors
# ==============================================================================


class AjaxDeviceSensor(CoordinatorEntity[AjaxDataCoordinator], SensorEntity):
    """Representation of an Ajax device-level sensor (measurements from a specific device)."""

    entity_description: AjaxDeviceSensorDescription

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        entry: ConfigEntry,
        space_id: str,
        device_id: str,
        description: AjaxDeviceSensorDescription,
    ) -> None:
        """Initialize the device sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._space_id = space_id
        self._device_id = device_id
        self._entry = entry

        # Get initial device data
        device = coordinator.get_device(space_id, device_id)
        device_name = device.name if device else "Unknown"

        # Set entity attributes - use translation_key instead of hardcoded name
        self._attr_has_entity_name = True
        self._attr_translation_key = description.translation_key
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_{description.key}"
        self._attr_entity_registry_enabled_default = description.enabled_by_default

    @property
    def native_value(self) -> Any:
        """Return the state of the sensor."""
        device = self.coordinator.get_device(self._space_id, self._device_id)
        if not device or not self.entity_description.value_fn:
            return None

        return self.entity_description.value_fn(device)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if not self.coordinator.last_update_success:
            return False

        device = self.coordinator.get_device(self._space_id, self._device_id)
        if not device:
            return False

        # Device-level sensors are unavailable if device is offline
        return device.online

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        device = self.coordinator.get_device(self._space_id, self._device_id)
        if not device:
            return {}

        # For hub devices, use the space identifier to merge with space-level sensors
        # This prevents duplicate hub devices in Home Assistant
        if device.type == DeviceType.HUB:
            # Avoid redundant name if device.name is already "Hub"
            hub_name = "Ajax Hub" if device.name == "Hub" else f"Ajax {device.name}"
            return {
                "identifiers": {(DOMAIN, self._space_id)},
                "name": hub_name,
                "manufacturer": "Ajax Systems",
                "model": device.type.value.replace("_", " ").title(),
                "sw_version": device.firmware_version,
                "hw_version": device.hardware_version,
            }

        # For non-hub devices, use device identifier
        # Get room name if available
        room_name = None
        if device.room_id:
            space = self.coordinator.get_space(self._space_id)
            if space and device.room_id in space.rooms:
                room_name = space.rooms[device.room_id].name

        # Include room name in device name if available
        device_display_name = f"{room_name} - {device.name}" if room_name else device.name

        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"Ajax {device_display_name}",
            "manufacturer": "Ajax Systems",
            "model": device.type.value.replace("_", " ").title(),
            "via_device": (DOMAIN, self._space_id),
            "sw_version": device.firmware_version,
            "hw_version": device.hardware_version,
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        device = self.coordinator.get_device(self._space_id, self._device_id)
        if not device:
            return {}

        attributes = {
            "device_id": device.id,
            "device_type": device.type.value,
            "space_id": device.space_id,
            "hub_id": device.hub_id,
            "online": device.online,
            "bypassed": device.bypassed,
        }

        # Add room information if available
        if device.room_id:
            room = self.coordinator.get_room(self._space_id, device.room_id)
            if room:
                attributes["room_id"] = room.id
                attributes["room_name"] = room.name

        # Add battery state for battery sensors
        if self.entity_description.key == "battery" and device.battery_state:
            attributes["battery_state"] = device.battery_state
            attributes["is_low_battery"] = device.is_low_battery

        # Add custom attributes from description if available
        if hasattr(self.entity_description, "extra_attributes_fn") and self.entity_description.extra_attributes_fn:
            custom_attrs = self.entity_description.extra_attributes_fn(device)
            if custom_attrs:
                attributes.update(custom_attrs)

        return attributes
