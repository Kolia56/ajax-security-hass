"""Ajax data coordinator for Home Assistant.

This coordinator manages:
- Periodic polling updates from Ajax REST API
- Real-time event updates via AWS SQS (optional)
- Space, Room, Device, and Notification data
- State synchronization between Ajax and Home Assistant

Architecture:
- Hybrid Mode: SQS real-time events + REST polling fallback
- SQS events trigger immediate REST refresh for instant state updates
- REST polling continues every 30s as baseline
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AjaxRestApi, AjaxRestApiError, AjaxRestAuthError
from .const import (
    CONF_NOTIFICATION_FILTER,
    CONF_PERSISTENT_NOTIFICATION,
    DOMAIN,
    NOTIFICATION_FILTER_ALARMS_ONLY,
    NOTIFICATION_FILTER_NONE,
    NOTIFICATION_FILTER_SECURITY_EVENTS,
    UPDATE_INTERVAL,
)
from .models import (
    AjaxAccount,
    AjaxDevice,
    AjaxNotification,
    AjaxRoom,
    AjaxSpace,
    DeviceType,
    GroupState,
    NotificationType,
    SecurityState,
)

# Optional SQS support
try:
    from .sqs_manager import SQSManager
    from .sqs_client import AjaxSQSClient
    SQS_AVAILABLE = True
except ImportError:
    SQS_AVAILABLE = False
    SQSManager = None
    AjaxSQSClient = None

_LOGGER = logging.getLogger(__name__)


class AjaxDataCoordinator(DataUpdateCoordinator[AjaxAccount]):
    """Coordinator to manage Ajax data updates.

    Architecture:
        AjaxAccount (User)
        └── AjaxSpace (Hub/System)
            ├── Security State (Armed/Disarmed/etc)
            ├── Rooms (Zones/Pieces)
            │   └── Devices in room
            ├── Devices (All)
            │   ├── Sensors
            │   ├── Controls
            │   └── Cameras
            └── Notifications (Events)
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: AjaxRestApi,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        queue_name: str | None = None,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: Home Assistant instance
            api: Ajax REST API instance
            aws_access_key_id: AWS access key ID (optional, for SQS)
            aws_secret_access_key: AWS secret access key (optional, for SQS)
            queue_name: SQS queue name (optional, for SQS)
        """
        self.api = api
        self.account: AjaxAccount | None = None
        self._fast_poll_tasks: dict[str, asyncio.Task] = {}  # device_id -> fast polling task for door sensors
        self._wire_input_polling_tasks: dict[str, asyncio.Task] = {}  # space_id -> wire_input polling task
        self._initial_load_done: bool = False  # Track if initial data load is complete

        # SQS real-time events (optional)
        self.sqs_manager: SQSManager | None = None
        self._aws_access_key_id = aws_access_key_id
        self._aws_secret_access_key = aws_secret_access_key
        self._queue_name = queue_name
        self._sqs_initialized = False

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    async def _async_update_data(self) -> AjaxAccount:
        """Fetch data from Ajax REST API.

        This is called periodically for updates.
        """
        try:
            # Initialize account if needed
            if self.account is None:
                await self._async_init_account()

            # Only do full data load on first run or manual reload
            if not self._initial_load_done:
                # Update spaces - use hubs endpoint directly to get hubId
                await self._async_update_spaces_from_hubs()

                # Load devices and notifications in parallel for all spaces
                tasks = []
                for space_id in self.account.spaces.keys():
                    tasks.append(self._async_update_devices(space_id))
                    tasks.append(self._async_update_notifications(space_id, limit=20))

                # Execute all API calls in parallel for faster startup
                await asyncio.gather(*tasks, return_exceptions=True)

                # Mark initial load as complete
                self._initial_load_done = True
                _LOGGER.info("Initial data load complete")

                # Initialize SQS real-time events in background (if credentials provided)
                # This is done after initial load to not block startup with botocore's blocking I/O
                if not self._sqs_initialized and self._aws_access_key_id:
                    asyncio.create_task(self._async_init_sqs())
            else:
                # Periodic update - refresh hub state and devices
                await self._async_update_spaces_from_hubs()

                for space_id in self.account.spaces.keys():
                    space = self.account.spaces.get(space_id)
                    if space:
                        self._reset_expired_motion_detections(space)
                        await self._async_update_devices(space_id)

            return self.account

        except AjaxRestAuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except AjaxRestApiError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err

    async def _async_fast_poll_door_sensor(self, space_id: str, device_id: str) -> None:
        """Fast poll a door sensor after opening to quickly detect closure."""
        poll_interval = 3  # Poll every 3 seconds
        max_duration = 120  # Stop after 2 minutes
        start_time = asyncio.get_event_loop().time()

        try:
            while True:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > max_duration:
                    break

                try:
                    devices_data = await self.api.async_get_devices(space_id)
                    space = self.account.spaces.get(space_id)
                    if not space:
                        break

                    device_found = False
                    for device_data in devices_data:
                        if device_data.get("id") == device_id:
                            device_found = True

                            if device_id in space.devices:
                                device = space.devices[device_id]
                                # Normalize attributes from API
                                api_attrs = device_data.get("attributes", {})
                                normalized_attrs = self._normalize_device_attributes(
                                    api_attrs, device.type
                                )
                                device.attributes.update(normalized_attrs)

                                # Check door state (after normalization)
                                door_opened = device.attributes.get("door_opened", False)

                                if not door_opened:
                                    self.async_set_updated_data(self.account)
                                    if device_id in self._fast_poll_tasks:
                                        del self._fast_poll_tasks[device_id]
                                    break

                    if not device_found:
                        break

                except Exception as err:
                    _LOGGER.error("Error in fast polling for door sensor %s: %s", device_id, err)

                await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            raise
        finally:
            if device_id in self._fast_poll_tasks:
                del self._fast_poll_tasks[device_id]

    async def _async_poll_wire_inputs(self, space_id: str) -> None:
        """Poll wire_input devices every 10 seconds to get their states."""
        poll_interval = 10  # seconds

        try:
            while True:
                try:
                    devices_data = await self.api.async_get_devices(space_id)

                    if not devices_data:
                        await asyncio.sleep(poll_interval)
                        continue

                    space = self.account.spaces.get(space_id)
                    if space:
                        updated_count = 0
                        for device_data in devices_data:
                            device_id = device_data.get("id")
                            device_type = device_data.get("type")

                            if device_type != "wire_input" or not device_id:
                                continue

                            existing_device = space.devices.get(device_id)
                            if not existing_device:
                                continue

                            # Normalize attributes from API
                            api_attrs = device_data.get("attributes", {})
                            normalized_attrs = self._normalize_device_attributes(
                                api_attrs, existing_device.type
                            )

                            new_door_opened = normalized_attrs.get("door_opened", False)
                            old_door_opened = existing_device.attributes.get("door_opened", False)

                            new_external_opened = normalized_attrs.get("external_contact_opened")
                            old_external_opened = existing_device.attributes.get("external_contact_opened")

                            if new_door_opened != old_door_opened or (new_external_opened is not None and new_external_opened != old_external_opened):
                                existing_device.attributes.update(normalized_attrs)
                                updated_count += 1

                        if updated_count > 0:
                            self.async_update_listeners()

                except asyncio.CancelledError:
                    raise

                except Exception as err:
                    _LOGGER.error("Error polling wire_input devices for space %s: %s", space_id, err)

                await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            raise

    async def _async_init_account(self) -> None:
        """Initialize the account data."""
        # Use login info directly (no /user endpoint available)
        self.account = AjaxAccount(
            user_id=self.api.user_id or "",
            name=self.api.email.split("@")[0] if self.api.email else "Unknown",
            email=self.api.email or "",
        )

        _LOGGER.info("Initialized account for %s (user_id: %s)", self.account.name, self.account.user_id)

    async def _async_init_sqs(self) -> None:
        """Initialize AWS SQS for real-time events (optional).

        SQS provides real-time event notifications (<1s latency) that trigger
        immediate REST API updates. This creates a hybrid mode:
        - SQS events: Real-time triggers for instant state updates
        - REST polling: Baseline updates every 30s as fallback

        Note:
            Requires aiobotocore package and AWS credentials.
            If SQS fails to initialize, integration falls back to REST-only mode.
        """
        # Check if SQS is available
        if not SQS_AVAILABLE:
            _LOGGER.info(
                "AWS SQS not available (aiobotocore not installed). "
                "Using REST API polling only."
            )
            self._sqs_initialized = True  # Mark as "initialized" to prevent retries
            return

        # Check if credentials are provided
        if not self._aws_access_key_id or not self._aws_secret_access_key or not self._queue_name:
            _LOGGER.debug("AWS credentials not configured. Using REST API polling only.")
            self._sqs_initialized = True
            return

        try:
            _LOGGER.info("Initializing AWS SQS for real-time events...")

            # Create SQS client with HA's event loop for thread-safe callbacks
            sqs_client = AjaxSQSClient(
                aws_access_key_id=self._aws_access_key_id,
                aws_secret_access_key=self._aws_secret_access_key,
                queue_name=self._queue_name,
                hass_loop=self.hass.loop,
            )

            # Create SQS manager
            self.sqs_manager = SQSManager(
                coordinator=self,
                sqs_client=sqs_client,
            )

            # Start receiving events
            success = await self.sqs_manager.start()

            if success:
                _LOGGER.info(
                    "✓ AWS SQS initialized successfully - Real-time events enabled!"
                )
            else:
                _LOGGER.warning(
                    "Failed to start SQS - Falling back to REST API polling only"
                )
                self.sqs_manager = None

        except Exception as err:
            _LOGGER.warning(
                "Failed to initialize AWS SQS: %s - Using REST API polling only",
                err,
            )
            self.sqs_manager = None

        finally:
            self._sqs_initialized = True

    async def _async_update_spaces(self) -> None:
        """Update all spaces (hubs/systems)."""
        spaces_data = await self.api.async_get_spaces()

        # Get selected spaces from config entry (default to all if not specified)
        selected_spaces = self.config_entry.data.get("selected_spaces")
        if selected_spaces:
            _LOGGER.info("Loading selected spaces: %s", selected_spaces)

        for space_data in spaces_data:
            space_id = space_data.get("id")
            if not space_id:
                continue

            # Skip spaces not in selection (if selection is defined)
            if selected_spaces and space_id not in selected_spaces:
                continue

            # Create or update space
            if space_id not in self.account.spaces:
                space = AjaxSpace(
                    id=space_id,
                    name=space_data.get("name", "Unknown Space"),
                    hub_id=space_data.get("hub_id"),
                )
                self.account.spaces[space_id] = space
                _LOGGER.info("Added new space: %s", space.name)
            else:
                # Update existing space
                space = self.account.spaces[space_id]
                space.name = space_data.get("name", space.name)

            # Update security state if available
            if "security_state" in space_data:
                space.security_state = self._parse_security_state(
                    space_data["security_state"]
                )

            # Update notification count
            if "new_notifications_count" in space_data:
                space.unread_notifications = space_data["new_notifications_count"]

    async def _async_update_spaces_from_hubs(self) -> None:
        """Update spaces by fetching hubs directly (use hub_id as space_id)."""
        hubs_data = await self.api.async_get_hubs()

        for hub_data in hubs_data:
            hub_id = hub_data.get("hubId")
            if not hub_id:
                continue

            # Get hub details to get the name and state
            try:
                hub_details = await self.api.async_get_hub(hub_id)
                # Get rooms for this hub
                try:
                    rooms_data = await self.api.async_get_rooms(hub_id)
                    # Build room_id -> room_name mapping
                    rooms_map = {room.get("id"): room.get("roomName") for room in rooms_data if room.get("id")}
                except Exception as room_err:
                    _LOGGER.debug("Could not get rooms: %s", room_err)
                    rooms_map = {}
                # Try to get hub name from multiple possible fields
                hub_name = (
                    hub_details.get("name")
                    or hub_details.get("hubName")
                    or hub_details.get("deviceName")
                    or f"Hub {hub_id[:6]}"  # Use first 6 chars of hub_id as fallback
                )
                # Parse security state from hub details - use 'state' field, not 'securityMode'
                hub_state = hub_details.get("state", "DISARMED")
                security_state = self._parse_security_state(hub_state)
            except Exception as err:
                _LOGGER.warning("Failed to get hub details for %s: %s", hub_id, err)
                hub_name = f"Hub {hub_id}"
                security_state = SecurityState.NONE
                hub_details = {}

            # Use hub_id as space_id since we're mapping 1:1
            space_id = hub_id

            # Create or update space
            if space_id not in self.account.spaces:
                # New space - always use API state for initial value
                space = AjaxSpace(
                    id=space_id,
                    name=hub_name,
                    hub_id=hub_id,
                    security_state=security_state,  # Use API state at creation
                    hub_details=hub_details,  # Store all hub information
                )
                self.account.spaces[space_id] = space
                _LOGGER.info("Added new space from hub: %s (hub_id: %s, initial state: %s)", space.name, space.hub_id, security_state)
            else:
                # Existing space - update name, hub_id, hub_details, and potentially state
                space = self.account.spaces[space_id]
                space.name = hub_name
                space.hub_id = hub_id
                space.hub_details = hub_details  # Update hub information

            # Store rooms mapping in space for device room name lookup
            space._rooms_map = rooms_map  # type: ignore

            # Fetch users for this hub
            try:
                users_data = await self.api.async_get_users(hub_id)
                space._users = users_data  # type: ignore
            except Exception:
                space._users = []  # type: ignore

            # Update state from API unless SQS has recent activity (< 5 min)
            should_update_from_api = True
            if self.sqs_manager and self.sqs_manager.is_enabled:
                import time
                time_since_last_sqs = time.time() - self.sqs_manager.last_event_time
                if time_since_last_sqs < 300:  # 5 minutes
                    should_update_from_api = False

            if should_update_from_api:
                old_state = space.security_state
                space.security_state = security_state
                if old_state != security_state:
                    _LOGGER.info("Hub %s state updated: %s -> %s", hub_id, old_state, security_state)

    async def _async_update_devices(self, space_id: str) -> None:
        """Update devices for a specific space."""

        space = self.account.spaces.get(space_id)
        if not space:
            return

        devices_data = await self.api.async_get_devices(space_id)

        new_devices_count = 0

        for device_data in devices_data:
            device_id = device_data.get("id")
            if not device_id:
                continue

            # Parse device type - API uses camelCase (deviceType, deviceName)
            raw_device_type = device_data.get("deviceType", device_data.get("type", "unknown"))
            device_type = self._parse_device_type(raw_device_type)

            # Get room_id and room_name
            room_id = device_data.get("roomId", device_data.get("room_id"))
            rooms_map = getattr(space, "_rooms_map", {})
            room_name = rooms_map.get(room_id) if room_id else None

            # Create or update device
            if device_id not in space.devices:
                device = AjaxDevice(
                    id=device_id,
                    name=device_data.get("deviceName", device_data.get("name", "Unknown Device")),
                    type=device_type,
                    space_id=space_id,
                    hub_id=device_data.get("hub_id", space.hub_id or ""),
                    raw_type=raw_device_type,
                    room_id=room_id,
                    room_name=room_name,
                    group_id=device_data.get("groupId", device_data.get("group_id")),
                )
                space.devices[device_id] = device
                new_devices_count += 1

                # Log warning for unknown device types
                if device_type == DeviceType.UNKNOWN:
                    _LOGGER.warning(
                        "Unknown device type detected: '%s' for device '%s' (ID: %s). "
                        "Please report this to the integration developer.",
                        raw_device_type,
                        device.name,
                        device_id,
                    )
            else:
                device = space.devices[device_id]
                # Update raw_type in case it changed
                device.raw_type = raw_device_type
                # Update room info
                device.room_id = room_id
                device.room_name = room_name

            # Update basic device attributes from list endpoint
            device.online = device_data.get("online", True)
            device.bypassed = device_data.get("bypassed", False)

            # Fetch detailed device info for battery, signal, tamper, etc.
            try:
                device_details = await self.api.async_get_device(space_id, device_id)
                if device_details:
                    # malfunctions can be a list or an int - normalize to int (count)
                    malfunctions_data = device_details.get("malfunctions", 0)
                    if isinstance(malfunctions_data, list):
                        device.malfunctions = len(malfunctions_data)
                    else:
                        device.malfunctions = malfunctions_data
                    device.battery_level = device_details.get("batteryChargeLevelPercentage", device_details.get("battery_level"))
                    device.battery_state = device_details.get("batteryState", device_details.get("battery_state"))
                    # Convert signal level string to percentage
                    signal_level = device_details.get("signalLevel", device_details.get("signal_strength"))
                    if isinstance(signal_level, str):
                        signal_map = {
                            "EXCELLENT": 100,
                            "STRONG": 85,
                            "GOOD": 70,
                            "MEDIUM": 50,
                            "WEAK": 30,
                            "POOR": 15,
                        }
                        device.signal_strength = signal_map.get(signal_level.upper(), None)
                    else:
                        device.signal_strength = signal_level
                    device.firmware_version = device_details.get("firmwareVersion", device_details.get("firmware_version"))
                    device.hardware_version = device_details.get("hardwareVersion", device_details.get("hardware_version"))
                    device.states = device_details.get("states", [])

                    # Store tampered status in attributes
                    if "tampered" in device_details:
                        device.attributes["tampered"] = device_details.get("tampered", False)

                    # Store temperature if available (DoorProtect Plus)
                    if "temperature" in device_details:
                        device.attributes["temperature"] = device_details.get("temperature")

                    # Store other useful attributes
                    if "alwaysActive" in device_details:
                        device.attributes["always_active"] = device_details.get("alwaysActive", False)
                    if "nightModeArm" in device_details or "armedInNightMode" in device_details:
                        device.attributes["armed_in_night_mode"] = device_details.get("nightModeArm", device_details.get("armedInNightMode", False))

                    # DoorProtect Plus specific attributes
                    if "extraContactAware" in device_details:
                        device.attributes["extra_contact_aware"] = device_details.get("extraContactAware", False)
                    if "shockSensorAware" in device_details:
                        device.attributes["shock_sensor_aware"] = device_details.get("shockSensorAware", False)
                    if "accelerometerAware" in device_details:
                        device.attributes["accelerometer_aware"] = device_details.get("accelerometerAware", False)
                    if "shockSensorSensitivity" in device_details:
                        device.attributes["shock_sensor_sensitivity"] = device_details.get("shockSensorSensitivity", 0)
                    if "accelerometerTiltDegrees" in device_details:
                        device.attributes["accelerometer_tilt_degrees"] = device_details.get("accelerometerTiltDegrees", 5)
                    if "ignoreSimpleImpact" in device_details:
                        device.attributes["ignore_simple_impact"] = device_details.get("ignoreSimpleImpact", False)
                    if "sirenTriggers" in device_details:
                        device.attributes["siren_triggers"] = device_details.get("sirenTriggers", [])

                    # Door contact state (reedClosed -> door_opened)
                    if "reedClosed" in device_details:
                        # reedClosed=True means door is closed, so door_opened=False
                        device.attributes["door_opened"] = not device_details.get("reedClosed", True)
                    # External contact state (extraContactClosed -> external_contact_opened)
                    if "extraContactClosed" in device_details:
                        device.attributes["external_contact_opened"] = not device_details.get("extraContactClosed", True)

                    # Sensitivity (GlassProtect, MotionProtect, etc.)
                    if "sensitivity" in device_details:
                        device.attributes["sensitivity"] = device_details.get("sensitivity")

                    # Device color
                    if "color" in device_details:
                        device.attributes["color"] = device_details.get("color")

                    # Siren specific attributes
                    if "sirenVolumeLevel" in device_details:
                        device.attributes["siren_volume_level"] = device_details.get("sirenVolumeLevel")
                    if "beepVolumeLevel" in device_details:
                        device.attributes["beep_volume_level"] = device_details.get("beepVolumeLevel")
                    if "alarmDuration" in device_details:
                        device.attributes["alarm_duration"] = device_details.get("alarmDuration")
                    if "v2sirenIndicatorLightMode" in device_details:
                        device.attributes["led_indication"] = device_details.get("v2sirenIndicatorLightMode")
                    elif "blinkWhileArmed" in device_details:
                        device.attributes["led_indication"] = device_details.get("blinkWhileArmed")
                    # Siren beep/chime settings
                    if "beepOnArmDisarm" in device_details:
                        device.attributes["beep_on_arm_disarm"] = device_details.get("beepOnArmDisarm")
                    if "beepOnDelay" in device_details:
                        device.attributes["beep_on_delay"] = device_details.get("beepOnDelay")
                    if "chimesEnabled" in device_details:
                        device.attributes["chimes_enabled"] = device_details.get("chimesEnabled")
                    if "buzzerState" in device_details:
                        device.attributes["buzzer_state"] = device_details.get("buzzerState")

                    # LED indicator mode (all devices)
                    if "indicatorLightMode" in device_details:
                        device.attributes["indicatorLightMode"] = device_details.get("indicatorLightMode")

                    # Alerts by sirens setting
                    if "alertsBySirens" in device_details:
                        device.attributes["alertsBySirens"] = device_details.get("alertsBySirens", False)

                    # MotionCam specific attributes
                    if "imageResolution" in device_details:
                        device.attributes["imageResolution"] = device_details.get("imageResolution")
                    if "photosPerAlarm" in device_details:
                        device.attributes["photosPerAlarm"] = device_details.get("photosPerAlarm")
            except Exception:
                pass  # Device details are optional

            # Update device metadata (API uses "color" not "device_color")
            device.device_color = device_data.get("color") or device_details.get("color")
            device.device_label = device_data.get("device_label")
            device.device_marketing_id = device_data.get("device_marketing_id")

            # Update device attributes dict
            if "attributes" in device_data:
                # Normalize API attributes to internal format
                normalized_attrs = self._normalize_device_attributes(
                    device_data["attributes"], device.type
                )
                device.attributes.update(normalized_attrs)
            # Update room association
            if device.room_id and device.room_id in space.rooms:
                room = space.rooms[device.room_id]
                if device_id not in room.device_ids:
                    room.device_ids.append(device_id)

        # Log summary of devices loaded
        if new_devices_count > 0:
            _LOGGER.info("Discovered %d new device(s) in space %s", new_devices_count, space_id)

    def _normalize_device_attributes(
        self, api_attributes: dict[str, Any], device_type: DeviceType
    ) -> dict[str, Any]:
        """Normalize Ajax API attributes to internal format.

        The Ajax API uses specific attribute names (e.g., reedClosed) that we
        normalize to more intuitive names (e.g., door_opened) for internal use.

        Args:
            api_attributes: Raw attributes from Ajax API
            device_type: Type of device

        Returns:
            Normalized attributes dict
        """
        normalized = dict(api_attributes)  # Start with original attributes

        # Door contacts: Support both API formats
        if device_type in [DeviceType.DOOR_CONTACT, DeviceType.WIRE_INPUT]:
            # If API already provides door_opened, use it directly
            if "door_opened" not in api_attributes and "reedClosed" in api_attributes:
                # Convert reedClosed (False=open) to door_opened (True=open)
                # Invert the logic: reedClosed=False means door is open
                normalized["door_opened"] = not api_attributes["reedClosed"]

            # External contact: Support both formats
            if "external_contact_opened" not in api_attributes and "extraContactClosed" in api_attributes:
                # Convert extraContactClosed to external_contact_opened
                # extraContactClosed=False means contact is open (alarm state)
                normalized["external_contact_opened"] = not api_attributes["extraContactClosed"]

        # Motion detectors: Support both camelCase and snake_case
        if device_type == DeviceType.MOTION_DETECTOR:
            if "motion_detected" not in api_attributes and "motionDetected" in api_attributes:
                normalized["motion_detected"] = api_attributes["motionDetected"]
            if "motion_detected_at" not in api_attributes and "motionDetectedAt" in api_attributes:
                normalized["motion_detected_at"] = api_attributes["motionDetectedAt"]

        # Smoke detectors: Support both formats
        if device_type == DeviceType.SMOKE_DETECTOR:
            if "smoke_detected" not in api_attributes and "smokeDetected" in api_attributes:
                normalized["smoke_detected"] = api_attributes["smokeDetected"]

        # Flood detectors: Support both formats
        if device_type == DeviceType.FLOOD_DETECTOR:
            if "leak_detected" not in api_attributes and "leakDetected" in api_attributes:
                normalized["leak_detected"] = api_attributes["leakDetected"]

        # Glass break detectors: Support both formats
        if device_type == DeviceType.GLASS_BREAK:
            if "glass_break_detected" not in api_attributes and "glassBreakDetected" in api_attributes:
                normalized["glass_break_detected"] = api_attributes["glassBreakDetected"]

        return normalized

    def _reset_expired_motion_detections(self, space: AjaxSpace) -> None:
        """Reset motion_detected to False for motion detectors if no recent detection.

        Motion detection events are impulse-based (not persistent state),
        so we reset them after 30 seconds of no new detection.

        Args:
            space: The AjaxSpace to process
        """
        now = datetime.now(timezone.utc)
        expiry_seconds = 30  # Reset motion detection after 30 seconds

        for device in space.devices.values():
            # Only process motion detectors
            if device.type != DeviceType.MOTION_DETECTOR:
                continue

            # Check if motion_detected is currently True
            if not device.attributes.get("motion_detected"):
                continue

            # Get last detection time
            last_detected_at = device.attributes.get("motion_detected_at")
            if not last_detected_at:
                device.attributes["motion_detected"] = False
                continue

            # Parse timestamp
            try:
                last_detected = datetime.fromisoformat(last_detected_at)
                if last_detected.tzinfo is None:
                    last_detected = last_detected.replace(tzinfo=timezone.utc)

                if (now - last_detected).total_seconds() > expiry_seconds:
                    device.attributes["motion_detected"] = False
            except (ValueError, TypeError) as err:
                _LOGGER.warning(
                    "Failed to parse motion_detected_at timestamp for %s: %s",
                    device.name,
                    err
                )

    async def _async_update_notifications(
        self, space_id: str, limit: int = 50
    ) -> None:
        """Update notifications for a specific space."""
        space = self.account.spaces.get(space_id)
        if not space:
            return

        try:
            # TODO: Fetch notifications from API when endpoint is available
            # For now, notifications come from SQS real-time events
            # notifications_data = await self.api.async_find_notifications(space_id, limit)
            notifications_data = []

            # Clear existing notifications and add new ones
            space.notifications.clear()

            for notif_data in notifications_data:
                # Parse notification data
                from .models import AjaxNotification

                # Determine notification type based on event_type
                event_type = notif_data.get("event_type", "")
                notif_type = self._parse_notification_type(event_type)

                # Create notification object
                notification = AjaxNotification(
                    id=notif_data.get("id", ""),
                    space_id=notif_data.get("space_id", space_id),
                    type=notif_type,
                    title=event_type.replace("_", " ").title() if event_type else "Event",
                    message=f"{notif_data.get('device_name', 'Device')}: {event_type.replace('_', ' ')}" if event_type else "",
                    timestamp=notif_data.get("timestamp") or datetime.now(),
                    device_id=notif_data.get("device_id"),
                    device_name=notif_data.get("device_name"),
                    read=notif_data.get("read", False),
                    user_name=notif_data.get("user_name"),  # User who triggered the event
                )

                space.notifications.append(notification)

            # Update unread count
            space.unread_notifications = sum(1 for n in space.notifications if not n.read)

        except Exception as err:
            _LOGGER.error("Error updating notifications for space %s: %s", space_id, err)

    def _parse_notification_type(self, event_type: str | None) -> NotificationType:
        """Parse notification type from event type string."""
        from .models import NotificationType

        if not event_type:
            return NotificationType.INFO

        event_lower = event_type.lower()

        if any(keyword in event_lower for keyword in ["alarm", "intrusion", "panic", "fire", "smoke", "leak", "gas"]):
            return NotificationType.ALARM
        elif any(keyword in event_lower for keyword in ["malfunction", "low", "tamper", "loss", "error", "fault"]):
            return NotificationType.WARNING
        elif any(keyword in event_lower for keyword in ["arm", "disarm", "motion", "door", "opened"]):
            return NotificationType.SECURITY_EVENT
        elif any(keyword in event_lower for keyword in ["update", "added", "changed", "test"]):
            return NotificationType.SYSTEM_EVENT
        else:
            return NotificationType.INFO

    async def _create_persistent_notification(
        self,
        notification: AjaxNotification,
        formatted_message: str,
        notif_type: NotificationType,
    ) -> None:
        """Create a persistent notification in Home Assistant if enabled and passes filter.

        Args:
            notification: The Ajax notification object
            formatted_message: The formatted notification message
            notif_type: The notification type (ALARM, WARNING, etc.)
        """
        # Get config options
        config_entry = self.config_entry
        options = config_entry.options if config_entry else {}

        # Check if persistent notifications are enabled
        if not options.get(CONF_PERSISTENT_NOTIFICATION, False):
            return

        # Get notification filter
        notification_filter = options.get(CONF_NOTIFICATION_FILTER, NOTIFICATION_FILTER_NONE)

        # Check if notification passes filter
        if notification_filter == NOTIFICATION_FILTER_NONE:
            return
        elif notification_filter == NOTIFICATION_FILTER_ALARMS_ONLY:
            # Only show alarms
            if notif_type != NotificationType.ALARM:
                return
        elif notification_filter == NOTIFICATION_FILTER_SECURITY_EVENTS:
            # Show alarms and security events (arming/disarming)
            if notif_type not in [NotificationType.ALARM, NotificationType.SECURITY_EVENT]:
                return
        # NOTIFICATION_FILTER_ALL: show all notifications (no filter)

        # Ensure we have a valid message
        if not formatted_message:
            return

        # Create persistent notification in Home Assistant
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Ajax Security",
                "message": formatted_message,
                "notification_id": f"ajax_{notification.id}",
            },
        )

    async def _fire_ajax_event(
        self,
        space: AjaxSpace,
        notification: AjaxNotification,
        event_type: str,
        device_id: str | None,
        device_name: str,
        room_name: str | None,
        timestamp,
    ) -> None:
        """Fire a Home Assistant event for Ajax notifications.

        This allows users to create automations based on Ajax events.

        Args:
            space: The AjaxSpace object
            notification: The AjaxNotification object
            event_type: The raw event type from Ajax
            device_id: Device ID that triggered the event
            device_name: Device name
            room_name: Room name if available
            timestamp: Event timestamp
        """
        if not event_type:
            return

        event_type_lower = event_type.lower()

        # Map Ajax event types to HA event names
        event_mapping = {
            "motion_detected": "ajax_motion_detected",
            "door_opened": "ajax_door_opened",
            "door_closed": "ajax_door_closed",
            "window_opened": "ajax_window_opened",
            "window_closed": "ajax_window_closed",
            "alarm_triggered": "ajax_alarm_triggered",
            "tamper": "ajax_device_tampered",
            "tampered": "ajax_device_tampered",
            "armed": "ajax_armed",
            "disarmed": "ajax_disarmed",
            "smoke_detected": "ajax_smoke_detected",
            "leak_detected": "ajax_leak_detected",
            "gas_detected": "ajax_gas_detected",
            "glass_break_detected": "ajax_glass_break_detected",
        }

        # Find matching event
        ha_event_name = None
        for key, event_name in event_mapping.items():
            if key in event_type_lower:
                ha_event_name = event_name
                break

        # If no specific mapping, use generic event
        if not ha_event_name:
            ha_event_name = "ajax_event"

        # Get device type if available
        device_type = None
        if device_id and device_id in space.devices:
            device = space.devices[device_id]
            device_type = device.type.value if device.type else None

        # Prepare event data
        event_data = {
            "space_id": space.id,
            "space_name": space.name,
            "event_type": event_type,
            "notification_type": notification.type.value,
            "message": notification.message,
            "timestamp": timestamp.isoformat(),
            "device_name": device_name,
        }

        # Add optional fields if available
        if device_id:
            event_data["device_id"] = device_id
        if device_type:
            event_data["device_type"] = device_type
        if room_name:
            event_data["room_name"] = room_name

        # Fire the event
        self.hass.bus.async_fire(ha_event_name, event_data)

    def _fire_security_state_event(
        self,
        space: AjaxSpace,
        old_state: SecurityState,
        new_state: SecurityState,
    ) -> None:
        """Fire a Home Assistant event when security state changes.

        Args:
            space: The AjaxSpace object
            old_state: Previous security state
            new_state: New security state
        """

        # Determine event type based on new state
        if new_state == SecurityState.ARMED:
            event_name = "ajax_armed"
        elif new_state == SecurityState.DISARMED:
            event_name = "ajax_disarmed"
        elif new_state == SecurityState.NIGHT_MODE:
            event_name = "ajax_armed_night"
        elif new_state == SecurityState.PARTIALLY_ARMED:
            event_name = "ajax_armed_home"
        else:
            event_name = "ajax_security_state_changed"

        # Prepare event data
        event_data = {
            "space_id": space.id,
            "space_name": space.name,
            "old_state": old_state.value,
            "new_state": new_state.value,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Add group information if in group mode
        if space.group_mode_enabled and space.groups:
            armed_groups = [g.name for g in space.groups.values() if g.state == GroupState.ARMED]
            disarmed_groups = [g.name for g in space.groups.values() if g.state == GroupState.DISARMED]
            event_data["armed_groups"] = armed_groups
            event_data["disarmed_groups"] = disarmed_groups
            event_data["group_mode"] = True
        else:
            event_data["group_mode"] = False

        # Fire the event
        self.hass.bus.async_fire(event_name, event_data)

    def _update_device_from_notification(self, space: AjaxSpace, notification: AjaxNotification) -> None:
        """Update device state based on notification event."""

        device = space.devices.get(notification.device_id)
        if not device:
            return

        if not notification.title:
            return

        event_type = notification.title.lower()

        # Update device attributes based on event type
        if "motion" in event_type or "mouvement" in event_type:
            # Motion detected event
            if "detected" in event_type or "détecté" in event_type:
                device.attributes["motion_detected"] = True
                device.attributes["motion_detected_at"] = notification.timestamp.isoformat()
                device.last_trigger_time = notification.timestamp.isoformat()
            elif "cleared" in event_type or "norm" in event_type:
                device.attributes["motion_detected"] = False

        elif "door" in event_type or "porte" in event_type:
            # Door sensor event
            if "opened" in event_type or "open" in event_type or "ouvert" in event_type:
                device.attributes["door_opened"] = True

                # Start fast polling when door opens to quickly detect closure
                device_id = notification.device_id
                if device_id in self._fast_poll_tasks and self._fast_poll_tasks[device_id].done():
                    del self._fast_poll_tasks[device_id]

                if device_id not in self._fast_poll_tasks:
                    task = asyncio.create_task(self._async_fast_poll_door_sensor(space.id, device_id))
                    self._fast_poll_tasks[device_id] = task

            elif "closed" in event_type or "close" in event_type or "fermé" in event_type:
                device.attributes["door_opened"] = False

                # Cancel fast polling if door closed
                device_id = notification.device_id
                if device_id in self._fast_poll_tasks:
                    task = self._fast_poll_tasks[device_id]
                    task.cancel()
                    del self._fast_poll_tasks[device_id]

        elif "window" in event_type or "fenêtre" in event_type:
            # Window sensor event (use same attribute as door)
            if "opened" in event_type or "open" in event_type:
                device.attributes["door_opened"] = True
            elif "closed" in event_type or "close" in event_type:
                device.attributes["door_opened"] = False

        elif "glass" in event_type or "verre" in event_type or "vitre" in event_type:
            # Glass break event
            if "detected" in event_type or "bris" in event_type or "cassé" in event_type:
                device.attributes["glass_break_detected"] = True
                device.attributes["glass_break_detected_at"] = notification.timestamp.isoformat()
                device.last_trigger_time = notification.timestamp.isoformat()
            elif "cleared" in event_type or "norm" in event_type:
                device.attributes["glass_break_detected"] = False

        elif "smoke" in event_type or "fire" in event_type or "fumée" in event_type or "incendie" in event_type:
            # Smoke/fire event
            if "detected" in event_type or "détecté" in event_type:
                device.attributes["smoke_detected"] = True
                device.attributes["smoke_detected_at"] = notification.timestamp.isoformat()
                device.last_trigger_time = notification.timestamp.isoformat()
            elif "cleared" in event_type or "norm" in event_type:
                device.attributes["smoke_detected"] = False

        elif "leak" in event_type or "water" in event_type or "flood" in event_type or "fuite" in event_type or "inondation" in event_type:
            # Water leak event
            if "detected" in event_type or "détecté" in event_type:
                device.attributes["leak_detected"] = True
                device.attributes["leak_detected_at"] = notification.timestamp.isoformat()
                device.last_trigger_time = notification.timestamp.isoformat()
            elif "cleared" in event_type or "norm" in event_type:
                device.attributes["leak_detected"] = False

        elif "external_contact" in event_type or "external contact" in event_type:
            # External contact event (for EOL/wire_input sensors)
            if "lost" in event_type or "open" in event_type:
                device.attributes["door_opened"] = True
                device.attributes["external_contact_state"] = "lost"
            elif "ok" in event_type or "closed" in event_type:
                device.attributes["door_opened"] = False
                device.attributes["external_contact_state"] = "ok"
            elif "short" in event_type or "circuit" in event_type:
                device.attributes["external_contact_state"] = "short_circuit"
                device.attributes["problem"] = True
                _LOGGER.warning("Device '%s': External contact short circuit", device.name)
            elif "fault" in event_type or "resistance" in event_type:
                device.attributes["external_contact_state"] = "fault"
                device.attributes["problem"] = True
                _LOGGER.warning("Device '%s': External contact fault", device.name)

        # Hub-specific events (battery, power, signal, antenna, noise, system faults)
        elif device.type == DeviceType.HUB:
            # Battery disconnected event
            if "battery" in event_type and ("disconnect" in event_type or "déconnect" in event_type or "batterie" in event_type):
                device.attributes["battery_disconnected"] = True
                device.attributes["battery_disconnected_at"] = notification.timestamp.isoformat()
                _LOGGER.warning("Hub '%s': Battery disconnected", device.name)
            elif "battery" in event_type and ("connect" in event_type or "reconnect" in event_type or "ok" in event_type):
                device.attributes["battery_disconnected"] = False

            # External power loss event
            elif "power" in event_type or "alimentation" in event_type:
                if "loss" in event_type or "lost" in event_type or "perte" in event_type:
                    device.attributes["external_power_loss"] = True
                    device.attributes["external_power_loss_at"] = notification.timestamp.isoformat()
                    device.attributes["externally_powered"] = False
                    _LOGGER.warning("Hub '%s': External power loss", device.name)
                elif "restored" in event_type or "ok" in event_type or "rétabli" in event_type:
                    device.attributes["external_power_loss"] = False
                    device.attributes["externally_powered"] = True

            # Cellular/GSM signal events
            elif "cellular" in event_type or "gsm" in event_type or "signal" in event_type or "cellulaire" in event_type:
                if "low" in event_type or "weak" in event_type or "faible" in event_type:
                    device.attributes["cellular_signal_low"] = True
                    device.attributes["cellular_signal_low_at"] = notification.timestamp.isoformat()
                    _LOGGER.warning("Hub '%s': Cellular signal low", device.name)
                elif "ok" in event_type or "normal" in event_type or "restored" in event_type:
                    device.attributes["cellular_signal_low"] = False

            # GSM antenna events
            elif "antenna" in event_type or "antenne" in event_type:
                if "damaged" in event_type or "endommagé" in event_type or "fault" in event_type:
                    device.attributes["gsm_antenna_damaged"] = True
                    device.attributes["gsm_antenna_damaged_at"] = notification.timestamp.isoformat()
                    device.attributes["problem"] = True
                    _LOGGER.error("Hub '%s': GSM antenna damaged", device.name)
                elif "disconnect" in event_type or "déconnect" in event_type:
                    device.attributes["gsm_antenna_disconnected"] = True
                    device.attributes["gsm_antenna_disconnected_at"] = notification.timestamp.isoformat()
                    _LOGGER.warning("Hub '%s': GSM antenna disconnected", device.name)
                elif "connect" in event_type or "ok" in event_type:
                    device.attributes["gsm_antenna_damaged"] = False
                    device.attributes["gsm_antenna_disconnected"] = False

            # Jeweller/Wings noise events
            elif "noise" in event_type or "bruit" in event_type:
                if "high" in event_type or "élevé" in event_type:
                    if "jeweller" in event_type:
                        device.attributes["jeweller_noise_high"] = True
                        device.attributes["jeweller_noise_high_at"] = notification.timestamp.isoformat()
                        _LOGGER.warning("Hub '%s': Jeweller noise high", device.name)
                    elif "wings" in event_type:
                        device.attributes["wings_noise_high"] = True
                        device.attributes["wings_noise_high_at"] = notification.timestamp.isoformat()
                        _LOGGER.warning("Hub '%s': Wings noise high", device.name)
                elif "ok" in event_type or "normal" in event_type:
                    if "jeweller" in event_type:
                        device.attributes["jeweller_noise_high"] = False
                    elif "wings" in event_type:
                        device.attributes["wings_noise_high"] = False

            # Software/System fault events
            elif "software" in event_type or "system" in event_type or "logiciel" in event_type or "système" in event_type:
                if "fault" in event_type or "error" in event_type or "défaut" in event_type or "erreur" in event_type:
                    device.attributes["system_fault"] = True
                    device.attributes["system_fault_at"] = notification.timestamp.isoformat()
                    device.attributes["problem"] = True
                    _LOGGER.error("Hub '%s': System fault detected", device.name)
                elif "ok" in event_type or "cleared" in event_type or "résolu" in event_type:
                    device.attributes["system_fault"] = False

            # CMS connection loss events
            elif "cms" in event_type or "monitoring" in event_type or "télésurveillance" in event_type:
                if "loss" in event_type or "lost" in event_type or "perte" in event_type:
                    device.attributes["cms_connection_loss"] = True
                    device.attributes["cms_connection_loss_at"] = notification.timestamp.isoformat()
                    _LOGGER.warning("Hub '%s': CMS connection loss", device.name)
                elif "restored" in event_type or "ok" in event_type or "rétabli" in event_type:
                    device.attributes["cms_connection_loss"] = False

    def _parse_security_state(self, state_value: Any) -> SecurityState:
        """Parse security state from API response."""
        if isinstance(state_value, str):
            state_str = state_value.upper()
            # Check DISARMED first, before ARMED, since "DISARMED" contains "ARMED"
            if "DISARMED" in state_str:
                return SecurityState.DISARMED
            elif "PARTIALLY" in state_str:
                return SecurityState.PARTIALLY_ARMED
            elif "NIGHT" in state_str:
                return SecurityState.NIGHT_MODE
            elif "ARMED" in state_str:
                return SecurityState.ARMED

        return SecurityState.NONE

    def _parse_device_type(self, type_str: str) -> DeviceType:
        """Parse device type from API response."""
        type_map = {
            # Motion detectors
            "motion_protect": DeviceType.MOTION_DETECTOR,
            "motion": DeviceType.MOTION_DETECTOR,
            "pir": DeviceType.MOTION_DETECTOR,
            "motionprotect": DeviceType.MOTION_DETECTOR,

            # Combi detectors (motion + glass break)
            "combi_protect": DeviceType.COMBI_PROTECT,
            "combiprotect": DeviceType.COMBI_PROTECT,
            "combi": DeviceType.COMBI_PROTECT,

            # Door/Window contacts
            "door_protect": DeviceType.DOOR_CONTACT,
            "doorprotect": DeviceType.DOOR_CONTACT,
            "door": DeviceType.DOOR_CONTACT,
            "window": DeviceType.DOOR_CONTACT,
            "opening": DeviceType.DOOR_CONTACT,
            "magnet": DeviceType.DOOR_CONTACT,

            # Glass break
            "glass_protect": DeviceType.GLASS_BREAK,
            "glassprotect": DeviceType.GLASS_BREAK,
            "glass": DeviceType.GLASS_BREAK,

            # Smoke detectors
            "fire_protect": DeviceType.SMOKE_DETECTOR,
            "fireprotect": DeviceType.SMOKE_DETECTOR,
            "smoke": DeviceType.SMOKE_DETECTOR,
            "fire": DeviceType.SMOKE_DETECTOR,

            # Flood detectors
            "leak_protect": DeviceType.FLOOD_DETECTOR,
            "leakprotect": DeviceType.FLOOD_DETECTOR,
            "leak": DeviceType.FLOOD_DETECTOR,
            "water": DeviceType.FLOOD_DETECTOR,
            "flood": DeviceType.FLOOD_DETECTOR,

            # Temperature
            "temperature": DeviceType.TEMPERATURE_SENSOR,
            "temp": DeviceType.TEMPERATURE_SENSOR,

            # Controls - Keypads and Keyboards
            "keypad": DeviceType.KEYPAD,
            "keyboard": DeviceType.KEYPAD,
            "keypadplus": DeviceType.KEYPAD,
            "keypad_plus": DeviceType.KEYPAD,
            "keypadsplus": DeviceType.KEYPAD,
            "keypad_s_plus": DeviceType.KEYPAD,
            "keypadplusg3": DeviceType.KEYPAD,
            "keypad_plus_g3": DeviceType.KEYPAD,
            "keypadcombi": DeviceType.KEYPAD,
            "keypad_combi": DeviceType.KEYPAD,
            "keyboardfibra": DeviceType.KEYPAD,
            "keyboard_fibra": DeviceType.KEYPAD,
            "keypadtouchscreen": DeviceType.KEYPAD,
            "keypad_touchscreen": DeviceType.KEYPAD,
            "keypadtouchscreeng3": DeviceType.KEYPAD,
            "keypad_touchscreen_g3": DeviceType.KEYPAD,
            "keypadbeep": DeviceType.KEYPAD,
            "keypad_beep": DeviceType.KEYPAD,
            "keypadbase": DeviceType.KEYPAD,
            "keypad_base": DeviceType.KEYPAD,
            "keypadtouchscreenfibra": DeviceType.KEYPAD,
            "keypad_touchscreen_fibra": DeviceType.KEYPAD,
            "keypadoutdoorbase": DeviceType.KEYPAD,
            "keypad_outdoor_base": DeviceType.KEYPAD,
            "keypadoutdoor": DeviceType.KEYPAD,
            "keypad_outdoor": DeviceType.KEYPAD,
            "keypadoutdoorfibra": DeviceType.KEYPAD,
            "keypad_outdoor_fibra": DeviceType.KEYPAD,
            # Remote controls
            "space_control": DeviceType.REMOTE_CONTROL,
            "spacecontrol": DeviceType.REMOTE_CONTROL,
            "remote": DeviceType.REMOTE_CONTROL,

            # Buttons
            "button": DeviceType.BUTTON,
            "double_button": DeviceType.BUTTON,
            "doublebutton": DeviceType.BUTTON,

            # Sirens
            "siren": DeviceType.SIREN,
            "alarm": DeviceType.SIREN,

            # Transmitter
            "transmitter": DeviceType.TRANSMITTER,
            "integration": DeviceType.TRANSMITTER,

            # Repeater / Range Extender
            "repeater": DeviceType.REPEATER,
            "rex": DeviceType.REPEATER,
            "range_extender": DeviceType.REPEATER,
            "extender": DeviceType.REPEATER,

            # Wired Input Modules
            "wire_input_mt": DeviceType.WIRE_INPUT,
            "wireinputmt": DeviceType.WIRE_INPUT,
            "wire_input_rs": DeviceType.WIRE_INPUT,
            "wireinputrs": DeviceType.WIRE_INPUT,

            # Line Splitter
            "line_split_fibra": DeviceType.LINE_SPLITTER,
            "linesplitfibra": DeviceType.LINE_SPLITTER,
            "line_splitter": DeviceType.LINE_SPLITTER,
            "linesplitter": DeviceType.LINE_SPLITTER,

            # Smart devices
            "socket": DeviceType.SOCKET,
            "relay": DeviceType.RELAY,
            "thermostat": DeviceType.THERMOSTAT,
            "life_quality": DeviceType.LIFE_QUALITY,
            "lifequality": DeviceType.LIFE_QUALITY,

            # Cameras
            "camera": DeviceType.CAMERA,
            "cam": DeviceType.CAMERA,

            # Hub
            "hub": DeviceType.HUB,
        }

        # Clean up the type string (remove formatting artifacts)
        # Example: "wire_input_mt {\n}\n" -> "wire_input_mt"
        type_cleaned = type_str.strip().split()[0].lower() if type_str else ""

        # Try exact match (case insensitive)
        type_lower = type_str.lower()
        if type_cleaned in type_map:
            return type_map[type_cleaned]
        if type_lower in type_map:
            return type_map[type_lower]

        # Try partial match
        for key, device_type in type_map.items():
            if key in type_lower or type_lower in key:
                return device_type

        # Log unknown types to help improve mapping
        _LOGGER.warning(
            "Unknown device type '%s' - please report this to help improve the integration. "
            "Device will be marked as UNKNOWN.",
            type_str,
        )
        return DeviceType.UNKNOWN

    # ============================================================================
    # Control methods
    # ============================================================================

    async def async_arm_space(self, space_id: str, force: bool = True) -> None:
        """Arm a space.

        Args:
            space_id: The space ID to arm
            force: If True, ignore problems and force arm even with open sensors
        """
        _LOGGER.info("Arming space %s (force=%s)", space_id, force)

        try:
            await self.api.async_arm(space_id, ignore_problems=force)
            # State will be updated via real-time stream to avoid race conditions

        except AjaxRestApiError as err:
            _LOGGER.error("Failed to arm space %s: %s", space_id, err)
            raise

    async def async_disarm_space(self, space_id: str) -> None:
        """Disarm a space."""
        _LOGGER.info("Disarming space %s", space_id)

        try:
            await self.api.async_disarm(space_id)
            # State will be updated via real-time stream to avoid race conditions

        except AjaxRestApiError as err:
            _LOGGER.error("Failed to disarm space %s: %s", space_id, err)
            raise

    async def async_arm_night_mode(self, space_id: str, force: bool = False) -> None:
        """Activate night mode for a space.

        Args:
            space_id: The space ID to arm in night mode
            force: If True, ignore alarms and force arm even with open sensors or problems
        """
        _LOGGER.info("Activating night mode for space %s (force=%s)", space_id, force)

        try:
            await self.api.async_night_mode(space_id, enabled=True)
            # State will be updated via real-time stream to avoid race conditions

        except AjaxRestApiError as err:
            _LOGGER.error("Failed to activate night mode for space %s: %s", space_id, err)
            raise

    async def async_arm_group(self, space_id: str, group_id: str, force: bool = False) -> None:
        """Arm a specific group/zone.

        Args:
            space_id: The space ID
            group_id: The group ID to arm
            force: If True, ignore alarms and force arm even with open sensors or problems
        """
        _LOGGER.info("Arming group %s in space %s (force=%s)", group_id, space_id, force)

        try:
            await self.api.async_arm_group(space_id, group_id, force=force)
            # State will be updated via real-time stream to avoid race conditions

        except AjaxRestApiError as err:
            _LOGGER.error("Failed to arm group %s in space %s: %s", group_id, space_id, err)
            raise

    async def async_disarm_group(self, space_id: str, group_id: str) -> None:
        """Disarm a specific group/zone.

        Args:
            space_id: The space ID
            group_id: The group ID to disarm
        """
        _LOGGER.info("Disarming group %s in space %s", group_id, space_id)

        try:
            await self.api.async_disarm_group(space_id, group_id)
            # State will be updated via real-time stream to avoid race conditions

        except AjaxRestApiError as err:
            _LOGGER.error("Failed to disarm group %s in space %s: %s", group_id, space_id, err)
            raise

    async def async_press_panic_button(self, space_id: str) -> None:
        """Press panic button (trigger panic alarm) for a space."""
        _LOGGER.warning("PANIC BUTTON pressed for space %s", space_id)

        try:
            await self.api.async_press_panic_button(space_id)
            # No state update needed, panic is instantaneous

        except AjaxRestApiError as err:
            _LOGGER.error("Failed to trigger panic for space %s: %s", space_id, err)
            raise

    # ============================================================================
    # Helper methods
    # ============================================================================

    def get_space(self, space_id: str) -> AjaxSpace | None:
        """Get a space by ID."""
        return self.account.spaces.get(space_id) if self.account else None

    def get_device(self, space_id: str, device_id: str) -> AjaxDevice | None:
        """Get a device by space and device ID."""
        space = self.get_space(space_id)
        return space.devices.get(device_id) if space else None

    def get_room(self, space_id: str, room_id: str) -> AjaxRoom | None:
        """Get a room by space and room ID."""
        space = self.get_space(space_id)
        return space.rooms.get(room_id) if space else None

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator and cleanup resources."""
        _LOGGER.info("Shutting down Ajax coordinator")

        # Stop SQS Manager (real-time events)
        if self.sqs_manager:
            try:
                _LOGGER.debug("Stopping AWS SQS Manager...")
                await self.sqs_manager.stop()
            except Exception as err:
                _LOGGER.error("Error stopping SQS Manager: %s", err)

        # Stop all wire_input polling tasks
        for space_id, task in self._wire_input_polling_tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._wire_input_polling_tasks.clear()

        # Stop all fast poll tasks
        for device_id, task in self._fast_poll_tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._fast_poll_tasks.clear()

        # Close API connection
        await self.api.close()
