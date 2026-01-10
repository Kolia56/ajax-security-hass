"""The Ajax Security System integration."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .api import AjaxRestApi, AjaxRestApiError, AjaxRestAuthError
from .const import (
    AUTH_MODE_DIRECT,
    AUTH_MODE_PROXY_HYBRID,
    AUTH_MODE_PROXY_SECURE,
    CONF_API_KEY,
    CONF_AUTH_MODE,
    CONF_AWS_ACCESS_KEY_ID,
    CONF_AWS_SECRET_ACCESS_KEY,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_PROXY_URL,
    CONF_QUEUE_NAME,
    DOMAIN,
)
from .coordinator import AjaxDataCoordinator

if TYPE_CHECKING:
    from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

# Service names
SERVICE_FORCE_ARM = "force_arm"
SERVICE_FORCE_ARM_NIGHT = "force_arm_night"
SERVICE_GET_RAW_DEVICES = "get_raw_devices"

PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.DEVICE_TRACKER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Ajax Security System component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Ajax Security System from a config entry."""
    _LOGGER.info("Ajax integration v0.7.6 starting...")
    hass.data.setdefault(DOMAIN, {})

    # Get authentication mode (default to direct for backwards compatibility)
    auth_mode = entry.data.get(CONF_AUTH_MODE, AUTH_MODE_DIRECT)

    # Get common credentials
    email = entry.data[CONF_EMAIL]
    password_hash = entry.data[CONF_PASSWORD]  # Already hashed in config_flow

    # Variables for different modes
    api_key = None
    proxy_url = None
    proxy_mode = None
    sse_url = None
    aws_access_key_id = None
    aws_secret_access_key = None
    queue_name = None

    if auth_mode == AUTH_MODE_DIRECT:
        # Direct mode: API key provided, optional SQS for real-time events
        api_key = entry.data[CONF_API_KEY]
        aws_access_key_id = entry.data.get(CONF_AWS_ACCESS_KEY_ID)
        aws_secret_access_key = entry.data.get(CONF_AWS_SECRET_ACCESS_KEY)
        queue_name = entry.data.get(CONF_QUEUE_NAME)
        _LOGGER.info("Using direct authentication mode")

    elif auth_mode == AUTH_MODE_PROXY_SECURE:
        # Proxy Secure: All requests via proxy, SSE for real-time events
        proxy_url = entry.data[CONF_PROXY_URL]
        proxy_mode = AUTH_MODE_PROXY_SECURE
        _LOGGER.info("Using proxy secure authentication mode")

    elif auth_mode == AUTH_MODE_PROXY_HYBRID:
        # Proxy Hybrid: Login via proxy to get API key, then direct requests, SSE for events
        proxy_url = entry.data[CONF_PROXY_URL]
        proxy_mode = AUTH_MODE_PROXY_HYBRID
        _LOGGER.info("Using proxy hybrid authentication mode")

    # Create REST API instance
    # password_is_hashed=True because we store only SHA256 hash, never plain password
    api = AjaxRestApi(
        api_key=api_key,
        email=email,
        password=password_hash,
        password_is_hashed=True,  # Password is already SHA256 hash
        proxy_url=proxy_url,
        proxy_mode=proxy_mode,
    )

    try:
        # Login to get temporary token (and API key + SSE URL if using proxy)
        await api.async_login()
        _LOGGER.info("Successfully logged in to Ajax REST API")

        # Get SSE URL if using proxy mode
        if auth_mode in (AUTH_MODE_PROXY_SECURE, AUTH_MODE_PROXY_HYBRID):
            sse_url = api.sse_url
            if sse_url:
                _LOGGER.info("SSE URL obtained from proxy: %s", sse_url)
            else:
                _LOGGER.warning("No SSE URL received from proxy")

        # Test API connection by getting hubs
        await api.async_get_hubs()
        _LOGGER.info("Successfully connected to Ajax REST API")

    except AjaxRestAuthError as err:
        _LOGGER.error("Authentication failed: %s", err)
        await api.close()
        return False
    except AjaxRestApiError as err:
        _LOGGER.error("API error during setup: %s", err)
        await api.close()
        raise ConfigEntryNotReady from err

    # Create coordinator
    # - REST polling: Baseline updates every 30s
    # - AWS SQS: Optional real-time events (direct mode only)
    # - SSE: Real-time events via proxy (proxy modes only)
    coordinator = AjaxDataCoordinator(
        hass,
        api,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        queue_name=queue_name,
        sse_url=sse_url,
    )

    # Store coordinator
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    await _async_setup_services(hass, coordinator)

    # Create HA Areas from Ajax rooms and assign devices
    await _async_setup_areas(hass, coordinator)

    return True


async def _async_setup_services(
    hass: HomeAssistant, coordinator: AjaxDataCoordinator
) -> None:
    """Set up Ajax services."""

    async def handle_force_arm(call: ServiceCall) -> None:
        """Handle force arm service call."""
        entity_id = call.data.get("entity_id")
        _LOGGER.info("Force arming via service call (entity: %s)", entity_id)

        # Get the first hub from coordinator
        if coordinator.account and coordinator.account.spaces:
            for hub_id in coordinator.account.spaces:
                try:
                    await coordinator.api.async_arm(hub_id, ignore_problems=True)
                    await coordinator.async_request_refresh()
                    _LOGGER.info("Force armed hub %s", hub_id)
                except Exception as err:
                    _LOGGER.error("Failed to force arm hub %s: %s", hub_id, err)

    async def handle_force_arm_night(call: ServiceCall) -> None:
        """Handle force arm night mode service call."""
        entity_id = call.data.get("entity_id")
        _LOGGER.info("Force arming night mode via service call (entity: %s)", entity_id)

        if coordinator.account and coordinator.account.spaces:
            for hub_id in coordinator.account.spaces:
                try:
                    await coordinator.api.async_night_mode(hub_id, enabled=True)
                    await coordinator.async_request_refresh()
                    _LOGGER.info("Force armed night mode hub %s", hub_id)
                except Exception as err:
                    _LOGGER.error(
                        "Failed to force arm night mode hub %s: %s", hub_id, err
                    )

    async def handle_get_raw_devices(call: ServiceCall) -> None:
        """Handle get raw devices service call - get full raw API data for all devices."""
        _LOGGER.info("Getting full raw data for all devices and cameras")

        all_devices = []
        all_cameras = []
        hub_count = 0

        if coordinator.account:
            for _space_id, space in coordinator.account.spaces.items():
                hub_id = space.hub_id
                if hub_id:
                    hub_count += 1
                    try:
                        # First get device list (light)
                        devices_list = await coordinator.api.async_get_devices(hub_id)
                        # Then get full details for each device
                        for device_summary in devices_list:
                            device_id = device_summary.get("id")
                            if device_id:
                                try:
                                    full_device = (
                                        await coordinator.api.async_get_device(
                                            hub_id, device_id
                                        )
                                    )
                                    all_devices.append(full_device)
                                except Exception as dev_err:
                                    _LOGGER.warning(
                                        "Failed to get device %s: %s",
                                        device_id,
                                        dev_err,
                                    )
                                    all_devices.append(device_summary)
                    except Exception as err:
                        _LOGGER.error(
                            "Failed to get devices for hub %s: %s", hub_id, err
                        )

            # Fetch cameras for each hub (same pattern as devices)
            for _space_id, space in coordinator.account.spaces.items():
                hub_id = space.hub_id
                if hub_id:
                    try:
                        cameras_list = await coordinator.api.async_get_cameras(hub_id)
                        for camera_summary in cameras_list:
                            camera_id = camera_summary.get("id")
                            if camera_id:
                                try:
                                    full_camera = (
                                        await coordinator.api.async_get_camera(
                                            hub_id, camera_id
                                        )
                                    )
                                    all_cameras.append(full_camera)
                                except Exception as cam_err:
                                    _LOGGER.warning(
                                        "Failed to get camera %s: %s",
                                        camera_id,
                                        cam_err,
                                    )
                                    all_cameras.append(camera_summary)
                    except Exception as err:
                        _LOGGER.warning(
                            "Failed to get cameras for hub %s: %s", hub_id, err
                        )

        # Write to file (include both devices and cameras)
        output_path = Path(hass.config.path("ajax_raw_devices.json"))
        output_data = {
            "devices": all_devices,
            "cameras": all_cameras,
        }

        def write_json():
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(output_data, f, indent=2, default=str, ensure_ascii=False)

        await hass.async_add_executor_job(write_json)

        _LOGGER.info(
            "Raw data written to %s (%d devices, %d cameras)",
            output_path,
            len(all_devices),
            len(all_cameras),
        )

        # Create notification with summary
        from homeassistant.components.persistent_notification import async_create

        # Count device types
        type_counts: dict[str, int] = {}
        for device in all_devices:
            dtype = device.get("deviceType", "unknown")
            type_counts[dtype] = type_counts.get(dtype, 0) + 1

        type_list = "\n".join(f"- {t}: {c}" for t, c in sorted(type_counts.items()))

        message = (
            f"**Hubs:** {hub_count}\n"
            f"**Devices:** {len(all_devices)}\n"
            f"**Cameras:** {len(all_cameras)}\n\n"
            f"**Device types:**\n{type_list}\n\n"
            f"Saved to: {output_path}"
        )

        async_create(
            hass,
            message,
            title="Ajax Raw Devices",
            notification_id="ajax_get_raw_devices",
        )

    # Register services if not already registered
    if not hass.services.has_service(DOMAIN, SERVICE_FORCE_ARM):
        hass.services.async_register(
            DOMAIN,
            SERVICE_FORCE_ARM,
            handle_force_arm,
            schema=vol.Schema({vol.Optional("entity_id"): cv.entity_ids}),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_FORCE_ARM_NIGHT):
        hass.services.async_register(
            DOMAIN,
            SERVICE_FORCE_ARM_NIGHT,
            handle_force_arm_night,
            schema=vol.Schema({vol.Optional("entity_id"): cv.entity_ids}),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_GET_RAW_DEVICES):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_RAW_DEVICES,
            handle_get_raw_devices,
        )


async def _async_setup_areas(
    hass: HomeAssistant, coordinator: AjaxDataCoordinator
) -> None:
    """Create HA Areas from Ajax rooms and assign devices to them."""
    from homeassistant.helpers import area_registry as ar, device_registry as dr

    area_reg = ar.async_get(hass)
    device_reg = dr.async_get(hass)

    # Collect all rooms from all spaces
    rooms_created = 0
    devices_assigned = 0

    for _space_id, space in coordinator.account.spaces.items():
        # Get rooms map from space
        rooms_map = getattr(space, "_rooms_map", {})

        for room_id, room_name in rooms_map.items():
            if not room_name:
                continue

            # Create area if it doesn't exist
            area = area_reg.async_get_area_by_name(room_name)
            if not area:
                area = area_reg.async_create(name=room_name)
                rooms_created += 1
                _LOGGER.info("Created HA Area: %s", room_name)

            # Assign devices in this room to the area (only if not already assigned)
            for device_id, device in space.devices.items():
                if device.room_id == room_id:
                    # Find the HA device by identifiers
                    ha_device = device_reg.async_get_device(
                        identifiers={(DOMAIN, device_id)}
                    )
                    # Only assign area if device has no area yet (respect user changes)
                    if ha_device and ha_device.area_id is None:
                        device_reg.async_update_device(ha_device.id, area_id=area.id)
                        devices_assigned += 1
                        _LOGGER.debug(
                            "Assigned device %s to area %s", device.name, room_name
                        )

    if rooms_created > 0 or devices_assigned > 0:
        _LOGGER.info(
            "Areas setup: %d created, %d devices assigned",
            rooms_created,
            devices_assigned,
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        coordinator: AjaxDataCoordinator = hass.data[DOMAIN].pop(entry.entry_id)

        # Shutdown coordinator (closes SQS manager, API connection, and all tasks)
        await coordinator.async_shutdown()

    return unload_ok
