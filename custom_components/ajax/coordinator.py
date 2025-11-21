"""Ajax data coordinator for Home Assistant.

This coordinator manages:
- Real-time updates from AWS SQS (or polling fallback)
- Space, Room, Device, and Notification data
- State synchronization between Ajax and Home Assistant
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
    NOTIFICATION_FILTER_ALL,
    NOTIFICATION_FILTER_ALARMS_ONLY,
    NOTIFICATION_FILTER_NONE,
    NOTIFICATION_FILTER_SECURITY_EVENTS,
    UPDATE_INTERVAL,
    get_event_message,
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

    def __init__(self, hass: HomeAssistant, api: AjaxRestApi) -> None:
        """Initialize the coordinator."""
        self.api = api
        self.account: AjaxAccount | None = None
        self.sqs_client = None  # Will be set by __init__.py if AWS SQS is configured
        self._fast_poll_tasks: dict[str, asyncio.Task] = {}  # device_id -> fast polling task for door sensors
        self._wire_input_polling_tasks: dict[str, asyncio.Task] = {}  # space_id -> wire_input polling task
        self._initial_load_done: bool = False  # Track if initial data load is complete

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    async def _async_update_data(self) -> AjaxAccount:
        """Fetch data from Ajax REST API.

        This is called periodically. If AWS SQS is configured, it provides real-time events.
        Without SQS, we rely on this polling for updates (60s interval).
        """
        try:
            # Initialize account if needed
            if self.account is None:
                await self._async_init_account()

            # Only do full data load on first run or manual reload
            # After that, rely on SQS events (or periodic polling if no SQS)
            if not self._initial_load_done:
                _LOGGER.debug("Performing initial/full data load")

                # Update spaces
                await self._async_update_spaces()

                # Load devices and notifications in parallel for all spaces
                tasks = []
                for space_id in self.account.spaces.keys():
                    tasks.append(self._async_update_devices(space_id))
                    tasks.append(self._async_update_notifications(space_id, limit=20))

                # Execute all API calls in parallel for faster startup
                await asyncio.gather(*tasks, return_exceptions=True)

                # Start AWS SQS listener if configured
                if self.sqs_client:
                    _LOGGER.info("Starting AWS SQS listener for real-time events")
                    self.sqs_client.set_event_callback(self._handle_sqs_event)
                    await self.sqs_client.start()
                else:
                    _LOGGER.info("AWS SQS not configured, relying on polling (60s interval)")

                # Mark initial load as complete
                self._initial_load_done = True
                _LOGGER.debug("Initial data load complete")
            else:
                # Periodic update - reset expired motion detections and refresh devices
                _LOGGER.debug("Periodic update - refreshing device states")
                for space_id in self.account.spaces.keys():
                    space = self.account.spaces.get(space_id)
                    if space:
                        self._reset_expired_motion_detections(space)
                        # Refresh devices periodically (important if no SQS)
                        await self._async_update_devices(space_id)

            return self.account

        except AjaxRestAuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except AjaxRestApiError as err:
            raise UpdateFailed(f"Error communicating with API: {err}") from err

    async def handle_event(self, event_data: dict[str, Any]) -> None:
        """Public method to handle events from AWS SQS.

        Args:
            event_data: The event data from SQS
        """
        await self._handle_sqs_event(event_data)

    async def _handle_sqs_event(self, event_data: dict[str, Any]) -> None:
        """Handle an event received from AWS SQS.

        Args:
            event_data: The event data from SQS
        """
        try:
            _LOGGER.debug("Received SQS event: %s", event_data.get("type", "unknown"))

            # Extract event information
            event_type = event_data.get("type")
            space_id = event_data.get("space_id")

            if not event_type or not space_id:
                _LOGGER.warning("Invalid SQS event (missing type or space_id): %s", event_data)
                return

            # Handle different event types
            if event_type == "device_status":
                # Device state changed (door opened, motion detected, etc.)
                await self._async_update_devices(space_id)
            elif event_type == "security_mode":
                # Security mode changed (armed, disarmed, etc.)
                await self._async_update_spaces()
            elif event_type == "notification":
                # New notification/alarm
                await self._async_update_notifications(space_id, limit=10)
            else:
                _LOGGER.debug("Unknown SQS event type: %s", event_type)

            # Trigger coordinator update to notify all entities
            self.async_set_updated_data(self.account)

        except Exception as err:
            _LOGGER.error("Error handling SQS event: %s", err)

    async def _async_fast_poll_door_sensor(self, space_id: str, device_id: str) -> None:
        """Fast poll a door sensor after opening to quickly detect closure.

        When a door opens (detected via stream), we start polling it every 3 seconds
        for up to 2 minutes to quickly detect when it closes.
        """
        poll_interval = 3  # Poll every 3 seconds
        max_duration = 120  # Stop after 2 minutes
        start_time = asyncio.get_event_loop().time()

        _LOGGER.info("Starting fast polling for door sensor %s", device_id)

        try:
            while True:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > max_duration:
                    _LOGGER.info("Fast polling timeout for door sensor %s after %d seconds", device_id, int(elapsed))
                    break

                try:
                    # Refresh device data from API
                    devices_data = await self.api.async_get_devices(space_id)

                    # Update account with fresh data (similar to _async_update_data)
                    space = self.account.spaces.get(space_id)
                    if not space:
                        break

                    # Find our device in the updated data
                    device_found = False
                    for device_data in devices_data:
                        if device_data.get("id") == device_id:
                            device_found = True
                            door_opened = device_data.get("attributes", {}).get("door_opened", False)
                            _LOGGER.debug(
                                "Fast polling device %s: door_opened=%s, attributes=%s",
                                device_id,
                                door_opened,
                                device_data.get("attributes", {})
                            )

                            # Update the device in our account
                            if device_id in space.devices:
                                device = space.devices[device_id]
                                device.attributes.update(device_data.get("attributes", {}))

                            # If door is now closed, stop polling
                            if not door_opened:
                                _LOGGER.info("🔄 POLLING: 🚪⬅️ Door '%s' CLOSED (detected via fast polling @ %dms) ✅",
                                           device.name if device_id in space.devices else device_id,
                                           poll_interval * 1000)
                                # Notify HA of the change
                                self.async_set_updated_data(self.account)
                                # Remove from fast poll tasks
                                if device_id in self._fast_poll_tasks:
                                    del self._fast_poll_tasks[device_id]
                                break
                            else:
                                _LOGGER.debug("🔄 POLLING: Door '%s' still OPENED (polling @ %dms)",
                                            device.name if device_id in space.devices else device_id,
                                            poll_interval * 1000)

                    if not device_found:
                        _LOGGER.warning("Device %s not found in API response", device_id)
                        break

                except Exception as err:
                    _LOGGER.error("Error in fast polling for door sensor %s: %s", device_id, err)

                # Wait before next poll
                await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            _LOGGER.info("Fast polling cancelled for door sensor %s", device_id)
            raise
        finally:
            # Clean up task from dictionary
            if device_id in self._fast_poll_tasks:
                del self._fast_poll_tasks[device_id]

    async def _async_poll_wire_inputs(self, space_id: str) -> None:
        """Poll wire_input devices every 10 seconds to get their states.

        Wire_input (EOL sensors) states are not transmitted via streaming API,
        so we need to poll them periodically.
        """
        poll_interval = 10  # seconds
        _LOGGER.info("Starting wire_input polling for space %s (interval: %ds)", space_id, poll_interval)

        try:
            while True:
                try:
                    # Get fresh device data from API
                    devices_data = await self.api.async_get_devices(space_id)

                    if not devices_data:
                        _LOGGER.debug("No devices returned from polling for space %s", space_id)
                        await asyncio.sleep(poll_interval)
                        continue

                    # Update only wire_input devices
                    space = self.account.spaces.get(space_id)
                    if space:
                        updated_count = 0
                        for device_data in devices_data:
                            device_id = device_data.get("id")
                            device_type = device_data.get("type")

                            # Only process wire_input devices
                            if device_type != "wire_input" or not device_id:
                                continue

                            # Check if device exists in our data
                            existing_device = space.devices.get(device_id)
                            if not existing_device:
                                continue

                            # Log all attributes for debugging wire_input
                            _LOGGER.debug(
                                "Wire_input '%s' polling data: %s",
                                device_data.get("name", "unknown"),
                                device_data.get("attributes", {})
                            )

                            # Check if door_opened state changed
                            new_door_opened = device_data.get("attributes", {}).get("door_opened", False)
                            old_door_opened = existing_device.attributes.get("door_opened", False)

                            # Also check external_contact_opened
                            new_external_opened = device_data.get("attributes", {}).get("external_contact_opened")
                            old_external_opened = existing_device.attributes.get("external_contact_opened")

                            if new_door_opened != old_door_opened or (new_external_opened is not None and new_external_opened != old_external_opened):
                                _LOGGER.info(
                                    "Wire_input '%s' state changed: %s -> %s",
                                    existing_device.name,
                                    "open" if old_door_opened else "closed",
                                    "open" if new_door_opened else "closed"
                                )
                                # Update the device attributes
                                existing_device.attributes["door_opened"] = new_door_opened
                                if new_external_opened is not None:
                                    existing_device.attributes["external_contact_opened"] = new_external_opened
                                updated_count += 1

                        # Notify Home Assistant of changes if any updates occurred
                        if updated_count > 0:
                            _LOGGER.debug("Updated %d wire_input device(s), notifying listeners", updated_count)
                            self.async_update_listeners()

                except asyncio.CancelledError:
                    _LOGGER.info("Wire_input polling cancelled for space %s", space_id)
                    raise

                except Exception as err:
                    _LOGGER.error("Error polling wire_input devices for space %s: %s", space_id, err, exc_info=True)

                # Wait before next poll
                await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            _LOGGER.info("Wire_input polling stopped for space %s", space_id)
            raise

    async def _async_process_device_update(self, space_id: str, update: dict) -> None:
        """Process a device status update from the stream."""
        try:
            update_type = update.get("type")

            # Process snapshot (full device state)
            # Snapshots arrive périodiquement (~60s) avec l'état complet de tous les devices
            # C'est le seul moyen de détecter la fermeture de porte en temps réel
            if update_type == "snapshot":
                _LOGGER.debug("Processing device snapshot for space %s", space_id)
                devices = update.get("devices", [])

                space = self.account.spaces.get(space_id)
                if not space:
                    return

                # Update all devices from snapshot
                for device_data in devices:
                    device_id = device_data.get("id")
                    if not device_id or device_id not in space.devices:
                        continue

                    device = space.devices[device_id]
                    attributes = device_data.get("attributes", {})

                    # Update door_opened state from snapshot
                    # If door_opened is not in attributes, it means the door is closed
                    old_door_state = device.attributes.get("door_opened", False)
                    new_door_state = attributes.get("door_opened", False)

                    if old_door_state != new_door_state:
                        device.attributes["door_opened"] = new_door_state
                        state_str = "opened" if new_door_state else "closed"
                        _LOGGER.debug("Device '%s': Door %s (via snapshot)", device.name, state_str)
                        # Notify Home Assistant of change
                        self.async_set_updated_data(self.account)

                return

            # Process real-time update
            if update_type == "update":
                update_data = update.get("data", {})
                device_id = update_data.get("device_id")
                status_data = update_data.get("status", {})

                if not device_id or not status_data:
                    return

                # Get the device
                space = self.account.spaces.get(space_id)
                if not space:
                    return

                device = space.devices.get(device_id)
                if not device:
                    return

                # Update device attributes based on status data
                updated = False

                # Motion detected
                if "motion_detected" in status_data:
                    device.attributes["motion_detected"] = status_data["motion_detected"]
                    if "motion_detected_at" in status_data:
                        device.attributes["motion_detected_at"] = status_data["motion_detected_at"].isoformat()
                    _LOGGER.debug("Device '%s': Motion status updated via stream: %s", device.name, status_data["motion_detected"])
                    updated = True

                # Door opened/closed
                if "door_opened" in status_data:
                    old_state = device.attributes.get("door_opened", False)
                    new_state = status_data["door_opened"]
                    if old_state != new_state:
                        device.attributes["door_opened"] = new_state
                        state_str = "opened" if new_state else "closed"
                        _LOGGER.debug("Device '%s': Door %s (via stream)", device.name, state_str)
                        updated = True

                        # Start fast polling when door opens to quickly detect closure
                        if new_state and device_id not in self._fast_poll_tasks:
                            _LOGGER.info("🔄 POLLING: 🚪➡️ Door '%s' OPENED, starting fast polling @ 500ms", device.name)
                            task = asyncio.create_task(self._async_fast_poll_door_sensor(space_id, device_id))
                            self._fast_poll_tasks[device_id] = task
                        # Cancel fast polling if door closed (detected via stream or snapshot)
                        elif not new_state and device_id in self._fast_poll_tasks:
                            _LOGGER.info("🔄 POLLING: 🚪⬅️ Door '%s' CLOSED (via stream), cancelling fast polling", device.name)
                            task = self._fast_poll_tasks[device_id]
                            task.cancel()
                            del self._fast_poll_tasks[device_id]

                # Notify Home Assistant of changes
                if updated:
                    self.async_set_updated_data(self.account)

        except Exception as err:
            _LOGGER.error("Error processing device update for space %s: %s", space_id, err, exc_info=True)

    async def _async_init_account(self) -> None:
        """Initialize the account data."""
        # Get account info
        account_data = await self.api.async_get_account()

        self.account = AjaxAccount(
            user_id=account_data.get("user_hex_id", ""),
            name=account_data.get("name", "Unknown"),
            email=account_data.get("email", ""),
        )

        _LOGGER.info("Initialized account for %s", self.account.name)

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

            # Parse device type
            raw_device_type = device_data.get("type", "unknown")
            device_type = self._parse_device_type(raw_device_type)

            # Create or update device
            if device_id not in space.devices:
                device = AjaxDevice(
                    id=device_id,
                    name=device_data.get("name", "Unknown Device"),
                    type=device_type,
                    space_id=space_id,
                    hub_id=device_data.get("hub_id", space.hub_id or ""),
                    raw_type=raw_device_type,
                    room_id=device_data.get("room_id"),
                    group_id=device_data.get("group_id"),
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
                    _LOGGER.debug("Added new device: %s (%s)", device.name, device.type.value)
            else:
                device = space.devices[device_id]
                # Update raw_type in case it changed
                device.raw_type = raw_device_type

            # Update device attributes
            device.online = device_data.get("online", True)
            device.bypassed = device_data.get("bypassed", False)
            device.malfunctions = device_data.get("malfunctions", 0)
            device.battery_level = device_data.get("battery_level")
            device.battery_state = device_data.get("battery_state")
            device.signal_strength = device_data.get("signal_strength")
            device.firmware_version = device_data.get("firmware_version")
            device.hardware_version = device_data.get("hardware_version")
            device.states = device_data.get("states", [])

            # Update device metadata
            device.device_color = device_data.get("device_color")
            device.device_label = device_data.get("device_label")
            device.device_marketing_id = device_data.get("device_marketing_id")

            # Update device attributes dict
            if "attributes" in device_data:
                device.attributes.update(device_data["attributes"])
            # Update room association
            if device.room_id and device.room_id in space.rooms:
                room = space.rooms[device.room_id]
                if device_id not in room.device_ids:
                    room.device_ids.append(device_id)

        # Log summary of devices loaded
        if new_devices_count > 0:
            _LOGGER.info("Discovered %d new device(s) in space %s", new_devices_count, space_id)
        _LOGGER.debug("Total devices in space %s: %d", space_id, len(space.devices))

    def _reset_expired_motion_detections(self, space: AjaxSpace) -> None:
        """Reset motion_detected to False for motion detectors if no recent detection.

        Motion detection events are impulse-based (not persistent state),
        so we reset them after 30 seconds of no new detection.

        Args:
            space: The AjaxSpace to process
        """
        from datetime import timedelta

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
                # No timestamp, reset immediately
                device.attributes["motion_detected"] = False
                _LOGGER.debug("Device '%s': Motion reset (no timestamp)", device.name)
                continue

            # Parse timestamp
            try:
                last_detected = datetime.fromisoformat(last_detected_at)
                # Make timezone-aware if needed
                if last_detected.tzinfo is None:
                    last_detected = last_detected.replace(tzinfo=timezone.utc)

                # Check if expired
                if (now - last_detected).total_seconds() > expiry_seconds:
                    device.attributes["motion_detected"] = False
                    _LOGGER.debug(
                        "Device '%s': Motion reset (expired after %d seconds)",
                        device.name,
                        expiry_seconds
                    )
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
            # Fetch notifications from API
            notifications_data = await self.api.async_find_notifications(space_id, limit)

            # Clear existing notifications and add new ones
            space.notifications.clear()

            for notif_data in notifications_data:
                # Parse notification data
                from datetime import datetime
                from .models import AjaxNotification, NotificationType

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

                # Note: We do NOT update device state from historical notifications
                # Device state comes from the real-time device snapshot only
                # Real-time notification updates happen via AWS SQS events

            # Update unread count
            space.unread_notifications = sum(1 for n in space.notifications if not n.read)

            _LOGGER.info(
                "Updated notifications for space %s: %d total, %d unread",
                space_id,
                len(space.notifications),
                space.unread_notifications,
            )

        except Exception as err:
            _LOGGER.error("Error updating notifications for space %s: %s", space_id, err, exc_info=True)

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

        _LOGGER.debug(
            "Fired event '%s' for device %s in room %s",
            ha_event_name,
            device_name,
            room_name or "unknown",
        )

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

        _LOGGER.debug(
            "Fired event '%s' for space %s: %s -> %s",
            event_name,
            space.name,
            old_state.value,
            new_state.value,
        )

    def _update_device_from_notification(self, space: AjaxSpace, notification: AjaxNotification) -> None:
        """Update device state based on notification event."""

        device = space.devices.get(notification.device_id)
        if not device:
            return

        if not notification.title:
            return

        event_type = notification.title.lower()
        _LOGGER.debug("Updating device %s from notification: %s", device.name, event_type)

        # Update device attributes based on event type
        if "motion" in event_type or "mouvement" in event_type:
            # Motion detected event
            if "detected" in event_type or "détecté" in event_type:
                device.attributes["motion_detected"] = True
                device.attributes["motion_detected_at"] = notification.timestamp.isoformat()
                device.last_trigger_time = notification.timestamp.isoformat()
                _LOGGER.debug("Device '%s': Motion detected", device.name)
            elif "cleared" in event_type or "norm" in event_type:
                device.attributes["motion_detected"] = False
                _LOGGER.debug("Device '%s': Motion cleared", device.name)

        elif "door" in event_type or "porte" in event_type:
            # Door sensor event
            if "opened" in event_type or "open" in event_type or "ouvert" in event_type:
                device.attributes["door_opened"] = True
                _LOGGER.debug("Device '%s': Door opened", device.name)

                # Start fast polling when door opens to quickly detect closure
                device_id = notification.device_id
                # Clean up any finished task first
                if device_id in self._fast_poll_tasks and self._fast_poll_tasks[device_id].done():
                    _LOGGER.debug("Removing finished fast poll task for device %s", device_id)
                    del self._fast_poll_tasks[device_id]

                if device_id not in self._fast_poll_tasks:
                    _LOGGER.debug("Door opened, starting fast polling for device %s", device_id)
                    task = asyncio.create_task(self._async_fast_poll_door_sensor(space.id, device_id))
                    self._fast_poll_tasks[device_id] = task
                else:
                    _LOGGER.debug("Fast polling already running for device %s", device_id)

            elif "closed" in event_type or "close" in event_type or "fermé" in event_type:
                device.attributes["door_opened"] = False
                _LOGGER.debug("Device '%s': Door closed", device.name)

                # Cancel fast polling if door closed
                device_id = notification.device_id
                if device_id in self._fast_poll_tasks:
                    _LOGGER.debug("Door closed, cancelling fast polling for device %s", device_id)
                    task = self._fast_poll_tasks[device_id]
                    task.cancel()
                    del self._fast_poll_tasks[device_id]

        elif "window" in event_type or "fenêtre" in event_type:
            # Window sensor event (use same attribute as door)
            if "opened" in event_type or "open" in event_type:
                device.attributes["door_opened"] = True
                _LOGGER.debug("Device '%s': Window opened", device.name)
            elif "closed" in event_type or "close" in event_type:
                device.attributes["door_opened"] = False
                _LOGGER.debug("Device '%s': Window closed", device.name)

        elif "glass" in event_type or "verre" in event_type or "vitre" in event_type:
            # Glass break event
            if "detected" in event_type or "bris" in event_type or "cassé" in event_type:
                device.attributes["glass_break_detected"] = True
                device.attributes["glass_break_detected_at"] = notification.timestamp.isoformat()
                device.last_trigger_time = notification.timestamp.isoformat()
                _LOGGER.debug("Device '%s': Glass break detected", device.name)
            elif "cleared" in event_type or "norm" in event_type:
                device.attributes["glass_break_detected"] = False

        elif "smoke" in event_type or "fire" in event_type or "fumée" in event_type or "incendie" in event_type:
            # Smoke/fire event
            if "detected" in event_type or "détecté" in event_type:
                device.attributes["smoke_detected"] = True
                device.attributes["smoke_detected_at"] = notification.timestamp.isoformat()
                device.last_trigger_time = notification.timestamp.isoformat()
                _LOGGER.debug("Device '%s': Smoke detected", device.name)
            elif "cleared" in event_type or "norm" in event_type:
                device.attributes["smoke_detected"] = False

        elif "leak" in event_type or "water" in event_type or "flood" in event_type or "fuite" in event_type or "inondation" in event_type:
            # Water leak event
            if "detected" in event_type or "détecté" in event_type:
                device.attributes["leak_detected"] = True
                device.attributes["leak_detected_at"] = notification.timestamp.isoformat()
                device.last_trigger_time = notification.timestamp.isoformat()
                _LOGGER.debug("Device '%s': Leak detected", device.name)
            elif "cleared" in event_type or "norm" in event_type:
                device.attributes["leak_detected"] = False

        # Tamper detection removed - was causing issues with buggy sensors

        elif "external_contact" in event_type or "external contact" in event_type:
            # External contact event (for EOL/wire_input sensors)
            if "lost" in event_type or "open" in event_type:
                # Contact lost = door/window opened or wire disconnected
                device.attributes["door_opened"] = True
                device.attributes["external_contact_state"] = "lost"
                _LOGGER.debug("Device '%s': External contact lost (opened)", device.name)
            elif "ok" in event_type or "closed" in event_type:
                # Contact OK = door/window closed or wire connected
                device.attributes["door_opened"] = False
                device.attributes["external_contact_state"] = "ok"
                _LOGGER.debug("Device '%s': External contact OK (closed)", device.name)
            elif "short" in event_type or "circuit" in event_type:
                # Short circuit fault
                device.attributes["external_contact_state"] = "short_circuit"
                device.attributes["problem"] = True
                _LOGGER.warning("Device '%s': External contact short circuit", device.name)
            elif "fault" in event_type or "resistance" in event_type:
                # Resistance fault or hard fault
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
                _LOGGER.debug("Hub '%s': Battery reconnected", device.name)

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
                    _LOGGER.debug("Hub '%s': External power restored", device.name)

            # Cellular/GSM signal events
            elif "cellular" in event_type or "gsm" in event_type or "signal" in event_type or "cellulaire" in event_type:
                if "low" in event_type or "weak" in event_type or "faible" in event_type:
                    device.attributes["cellular_signal_low"] = True
                    device.attributes["cellular_signal_low_at"] = notification.timestamp.isoformat()
                    _LOGGER.warning("Hub '%s': Cellular signal low", device.name)
                elif "ok" in event_type or "normal" in event_type or "restored" in event_type:
                    device.attributes["cellular_signal_low"] = False
                    _LOGGER.debug("Hub '%s': Cellular signal restored", device.name)

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
                    _LOGGER.debug("Hub '%s': GSM antenna OK", device.name)

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
                        _LOGGER.debug("Hub '%s': Jeweller noise normal", device.name)
                    elif "wings" in event_type:
                        device.attributes["wings_noise_high"] = False
                        _LOGGER.debug("Hub '%s': Wings noise normal", device.name)

            # Software/System fault events
            elif "software" in event_type or "system" in event_type or "logiciel" in event_type or "système" in event_type:
                if "fault" in event_type or "error" in event_type or "défaut" in event_type or "erreur" in event_type:
                    device.attributes["system_fault"] = True
                    device.attributes["system_fault_at"] = notification.timestamp.isoformat()
                    device.attributes["problem"] = True
                    _LOGGER.error("Hub '%s': System fault detected", device.name)
                elif "ok" in event_type or "cleared" in event_type or "résolu" in event_type:
                    device.attributes["system_fault"] = False
                    _LOGGER.debug("Hub '%s': System fault cleared", device.name)

            # CMS connection loss events
            elif "cms" in event_type or "monitoring" in event_type or "télésurveillance" in event_type:
                if "loss" in event_type or "lost" in event_type or "perte" in event_type:
                    device.attributes["cms_connection_loss"] = True
                    device.attributes["cms_connection_loss_at"] = notification.timestamp.isoformat()
                    _LOGGER.warning("Hub '%s': CMS connection loss", device.name)
                elif "restored" in event_type or "ok" in event_type or "rétabli" in event_type:
                    device.attributes["cms_connection_loss"] = False
                    _LOGGER.debug("Hub '%s': CMS connection restored", device.name)

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

    async def async_arm_space(self, space_id: str, force: bool = False) -> None:
        """Arm a space.

        Args:
            space_id: The space ID to arm
            force: If True, ignore alarms and force arm even with open sensors or problems
        """
        _LOGGER.info("Arming space %s (force=%s)", space_id, force)

        try:
            await self.api.async_arm(space_id, force=force)
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
            await self.api.async_arm_night_mode(space_id, force=force)
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
        """Shutdown the coordinator."""
        _LOGGER.info("Shutting down Ajax coordinator")

        # Stop all streaming tasks
        for space_id, task in self._streaming_tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._streaming_tasks.clear()

        # Stop all notification streaming tasks
        for space_id, task in self._notification_streaming_tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._notification_streaming_tasks.clear()

        # Stop all device streaming tasks
        for space_id, task in self._device_streaming_tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._device_streaming_tasks.clear()

        # Stop all device-specific streaming tasks
        for device_id, task in self._specific_device_streams.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._specific_device_streams.clear()

        # Stop all wire_input polling tasks
        for space_id, task in self._wire_input_polling_tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._wire_input_polling_tasks.clear()

        # Close API connection
        await self.api.close()
