"""SQS Manager for Ajax real-time events.

This module manages the integration between AWS SQS events and the Ajax coordinator.
It acts as a bridge between real-time events and the REST API polling system.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .event_parser import AjaxEventParser

if TYPE_CHECKING:
    from .coordinator import AjaxDataCoordinator
    from .sqs_client import AjaxSQSClient

_LOGGER = logging.getLogger(__name__)


class SQSManager:
    """Manager for AWS SQS real-time event integration."""

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        sqs_client: AjaxSQSClient,
    ) -> None:
        """Initialize SQS manager.

        Args:
            coordinator: Ajax data coordinator
            sqs_client: SQS client instance
        """
        self.coordinator = coordinator
        self.sqs_client = sqs_client
        self.parser = AjaxEventParser()

        self._enabled = False
        self._last_event_time: float = 0.0

        _LOGGER.debug("SQS Manager initialized")

    async def start(self) -> bool:
        """Start SQS event receiving.

        Returns:
            True if started successfully
        """
        try:
            # Connect to SQS
            if not await self.sqs_client.connect():
                _LOGGER.error("Failed to connect to SQS")
                return False

            # Set event callback
            self.sqs_client.event_callback = self._handle_event

            # Start receiving
            await self.sqs_client.start_receiving()

            self._enabled = True
            _LOGGER.info("SQS Manager started successfully")
            return True

        except Exception as err:
            _LOGGER.error("Failed to start SQS Manager: %s", err)
            return False

    async def stop(self) -> None:
        """Stop SQS event receiving."""
        self._enabled = False

        try:
            await self.sqs_client.stop_receiving()
            await self.sqs_client.close()
            _LOGGER.info("SQS Manager stopped")
        except Exception as err:
            _LOGGER.error("Error stopping SQS Manager: %s", err)

    async def _handle_event(self, event_data: dict[str, Any]) -> None:
        """Handle received SQS event.

        Args:
            event_data: Raw event data from SQS
        """
        if not self._enabled:
            return

        try:
            # Parse event
            event = self.parser.parse_event(event_data)
            if not event:
                return

            _LOGGER.info(
                "Received Ajax event: %s - %s (%s)",
                event["event_type"],
                event["action"],
                event["source_name"],
            )

            # Update last event time
            self._last_event_time = event.get("timestamp", 0)

            # Store event in space recent_events (keep last 5)
            space_id = event.get("space_id")
            if space_id and self.coordinator.account:
                space = self.coordinator.account.spaces.get(space_id)
                if space:
                    # Add event to beginning of list
                    space.recent_events.insert(0, event)
                    # Keep only last 5 events
                    space.recent_events = space.recent_events[:5]

            # Trigger coordinator update based on event type
            await self._process_event(event)

        except Exception as err:
            _LOGGER.error("Error handling SQS event: %s", err)

    async def _process_event(self, event: dict[str, Any]) -> None:
        """Process parsed event and trigger appropriate actions.

        Args:
            event: Parsed event dict
        """
        # Determine if we should fast poll
        should_fast_poll = self.parser.should_trigger_fast_poll(event)

        if should_fast_poll:
            _LOGGER.debug("Event triggers fast poll: %s", event["action"])

        # Fire Home Assistant event for all Ajax events
        await self._fire_ha_event(event)

        # Update device state directly for real-time response
        await self._update_device_state(event)

        # Always trigger an immediate coordinator refresh for real-time updates
        await self.coordinator.async_request_refresh()

        # Handle specific event types
        if self.parser.is_alarm_event(event):
            await self._handle_alarm_event(event)
        elif self.parser.is_arming_event(event):
            await self._handle_arming_event(event)
        elif self.parser.is_malfunction_event(event):
            await self._handle_malfunction_event(event)
        elif self.parser.is_connection_event(event):
            await self._handle_connection_event(event)

    async def _handle_alarm_event(self, event: dict[str, Any]) -> None:
        """Handle alarm event.

        Args:
            event: Parsed alarm event
        """
        action = event["action"]
        source_name = event["source_name"]
        is_active = event["is_active"]

        _LOGGER.warning(
            "ALARM: %s - %s (%s)",
            action,
            source_name,
            "TRIGGERED" if is_active else "CLEARED",
        )

        # Create Home Assistant persistent notification if configured
        if hasattr(self.coordinator, "hass"):
            from homeassistant.components.persistent_notification import async_create

            message = f"Ajax Alarm: {action.replace('_', ' ').title()}\n"
            message += f"Device: {source_name}\n"
            message += f"Status: {'TRIGGERED' if is_active else 'CLEARED'}\n"
            message += f"Time: {event['event_time']}"

            async_create(
                self.coordinator.hass,
                message=message,
                title="Ajax Security Alert",
                notification_id=f"ajax_alarm_{event['event_id']}",
            )

    async def _handle_arming_event(self, event: dict[str, Any]) -> None:
        """Handle arming/disarming event.

        Args:
            event: Parsed arming event
        """
        from .models import SecurityState

        action = event["action"]
        hub_id = event.get("hub_id")

        _LOGGER.info("Hub arming state changed: %s (hub: %s)", action, hub_id)

        # Update space security state directly from SQS event
        if hub_id and self.coordinator.account:
            space = self.coordinator.account.spaces.get(hub_id)
            if space:
                old_state = space.security_state

                # Map SQS action to SecurityState
                action_lower = action.lower()
                if "disarm" in action_lower:
                    space.security_state = SecurityState.DISARMED
                elif "night" in action_lower:
                    space.security_state = SecurityState.NIGHT_MODE
                elif "arm" in action_lower:
                    space.security_state = SecurityState.ARMED

                _LOGGER.info(
                    "Updated security state from SQS: %s -> %s",
                    old_state,
                    space.security_state,
                )

                # Notify Home Assistant of the state change immediately
                self.coordinator.async_set_updated_data(self.coordinator.account)

    async def _handle_malfunction_event(self, event: dict[str, Any]) -> None:
        """Handle malfunction event.

        Args:
            event: Parsed malfunction event
        """
        action = event["action"]
        source_name = event["source_name"]
        is_active = event["is_active"]

        _LOGGER.warning(
            "MALFUNCTION: %s - %s (%s)",
            action,
            source_name,
            "TRIGGERED" if is_active else "CLEARED",
        )

        # Trigger device update
        await self.coordinator.async_request_refresh()

    async def _handle_connection_event(self, event: dict[str, Any]) -> None:
        """Handle connection/offline event.

        Args:
            event: Parsed connection event
        """
        source_name = event["source_name"]
        is_active = event["is_active"]

        _LOGGER.info(
            "Device connection: %s (%s)",
            source_name,
            "OFFLINE" if is_active else "ONLINE",
        )

        # Trigger device update
        await self.coordinator.async_request_refresh()

    async def _fire_ha_event(self, event: dict[str, Any]) -> None:
        """Fire a Home Assistant event for Ajax events.

        Args:
            event: Parsed event dict
        """
        if not hasattr(self.coordinator, "hass"):
            return

        action = event.get("action", "unknown")
        event_type = event.get("event_type", "unknown")

        # Map action to HA event type
        ha_event_type = "ajax_event"
        if action in ["motion_detected"]:
            ha_event_type = "ajax_motion"
        elif action in ["door_opened", "external_contact_opened"]:
            ha_event_type = "ajax_door"
        elif action in ["smoke_detected", "temperature_alarm", "co_detected"]:
            ha_event_type = "ajax_fire"
        elif action in ["leak_detected"]:
            ha_event_type = "ajax_water"
        elif action in ["glass_break_detected"]:
            ha_event_type = "ajax_glass_break"
        elif self.parser.is_alarm_event(event):
            ha_event_type = "ajax_alarm"
        elif self.parser.is_arming_event(event):
            ha_event_type = "ajax_arming"

        event_data = {
            "action": action,
            "event_type": event_type,
            "source_name": event.get("source_name"),
            "hub_id": event.get("hub_id"),
            "is_active": event.get("is_active", False),
            "timestamp": event.get("timestamp"),
        }

        # Add device_id if available
        device_id = self.parser.get_device_id_from_event(event)
        if device_id:
            event_data["device_id"] = device_id

        self.coordinator.hass.bus.async_fire(ha_event_type, event_data)
        _LOGGER.debug("Fired HA event: %s with data: %s", ha_event_type, event_data)

    async def _update_device_state(self, event: dict[str, Any]) -> None:
        """Update device state directly from SQS event for real-time response.

        Args:
            event: Parsed event dict
        """
        action = event.get("action", "")
        source_name = event.get("source_name", "")
        hub_id = event.get("hub_id")
        is_active = event.get("is_active", False)

        if not hub_id or not self.coordinator.account:
            return

        # Find the space
        space = self.coordinator.account.spaces.get(hub_id)
        if not space:
            return

        # Find device by name
        device = None
        for dev in space.devices.values():
            if dev.name == source_name:
                device = dev
                break

        if not device:
            _LOGGER.debug("Device not found for SQS event: %s", source_name)
            return

        # Update device state based on action
        from datetime import datetime, timezone
        from .models import AjaxNotification, NotificationType

        # Create a notification to track the event
        notification = AjaxNotification(
            id=event.get("event_id", ""),
            space_id=hub_id,
            type=NotificationType.ALARM if self.parser.is_alarm_event(event) else NotificationType.SECURITY_EVENT,
            title=action,
            message=f"{action} from {source_name}",
            timestamp=datetime.fromtimestamp(event.get("timestamp", 0), tz=timezone.utc) if event.get("timestamp") else datetime.now(timezone.utc),
            device_id=device.id,
            device_name=device.name,
        )

        device.last_notification = notification
        if is_active:
            device.last_trigger_time = datetime.now(timezone.utc)

        # Update actual device state based on action
        state_updated = False

        if action == "door_opened":
            # is_active=True means door is open, is_active=False means door is closed (recovered)
            device.attributes["door_opened"] = is_active
            state_updated = True
            _LOGGER.info("Door sensor %s: door_opened=%s", device.name, is_active)

        elif action == "external_contact_opened":
            device.attributes["external_contact_opened"] = is_active
            state_updated = True
            _LOGGER.info("External contact %s: opened=%s", device.name, is_active)

        elif action == "motion_detected":
            device.attributes["motion_detected"] = is_active
            state_updated = True

        elif action == "smoke_detected":
            device.attributes["smoke_detected"] = is_active
            state_updated = True

        elif action == "leak_detected":
            device.attributes["leak_detected"] = is_active
            state_updated = True

        elif action == "glass_break_detected":
            device.attributes["glass_break_detected"] = is_active
            state_updated = True

        elif action == "device_tampered":
            device.attributes["tampered"] = is_active
            state_updated = True

        # Notify HA of the state change
        if state_updated:
            self.coordinator.async_set_updated_data(self.coordinator.account)

        _LOGGER.info(
            "Updated device %s state from SQS: %s (active=%s)",
            device.name,
            action,
            is_active,
        )

    @property
    def is_enabled(self) -> bool:
        """Check if SQS is enabled and running.

        Returns:
            True if enabled
        """
        return self._enabled

    @property
    def last_event_time(self) -> float:
        """Get timestamp of last received event.

        Returns:
            Unix timestamp
        """
        return self._last_event_time
