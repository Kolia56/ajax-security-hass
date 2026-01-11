"""SSE Manager for Ajax real-time events via proxy.

This manager receives events from the SSE client (proxy mode) and processes them
in the same way as the SQS manager. The event format from the proxy should be
compatible with the SQS event format.

Architecture:
- SSE events are used for INSTANT state updates (< 1 second)
- REST API polling confirms state periodically (fallback)
- SSE events directly update coordinator state for fastest response
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .event_codes import (
    DEFAULT_LANGUAGE,
    parse_event_code,
)

# Import event mappings from SQS manager to avoid duplication
from .sqs_manager import (
    DEVICE_STATUS_EVENTS,
    DOOR_EVENTS,
    EVENT_TAG_TO_STATE,
    FLOOD_EVENTS,
    GLASS_EVENTS,
    MOTION_EVENTS,
    RELAY_EVENTS,
    SCENARIO_EVENTS,
    SMOKE_EVENTS,
    TAMPER_EVENTS,
)

if TYPE_CHECKING:
    from .coordinator import AjaxDataCoordinator
    from .sse_client import AjaxSSEClient

_LOGGER = logging.getLogger(__name__)


class SSEManager:
    """Manages SSE events from Ajax proxy."""

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        sse_client: AjaxSSEClient,
    ):
        """Initialize SSE Manager.

        Args:
            coordinator: The Ajax data coordinator
            sse_client: SSE client instance
        """
        self.coordinator = coordinator
        self.sse_client = sse_client
        self._language = DEFAULT_LANGUAGE
        self._last_state_update: dict[str, float] = {}  # hub_id -> timestamp
        self._recent_events: dict[str, float] = {}  # event_key -> timestamp
        self._dedup_window = 5  # seconds to ignore duplicate events

    def set_language(self, language: str) -> None:
        """Set language for event messages."""
        self._language = language

    async def start(self) -> bool:
        """Start receiving SSE events.

        Returns:
            True if started successfully
        """
        _LOGGER.info("Starting SSE Manager...")

        # Set up callback for received events
        self.sse_client._callback = self._handle_event

        # Start SSE client
        success = await self.sse_client.start()

        if success:
            _LOGGER.info("SSE Manager started successfully")
        else:
            _LOGGER.error("Failed to start SSE Manager")

        return success

    async def stop(self) -> None:
        """Stop receiving SSE events."""
        _LOGGER.info("Stopping SSE Manager...")
        await self.sse_client.stop()
        _LOGGER.info("SSE Manager stopped")

    def is_state_protected(self, hub_id: str) -> bool:
        """Check if a hub's state was recently updated via SSE.

        This prevents REST polling from overwriting recent SSE updates.

        Args:
            hub_id: Hub ID to check

        Returns:
            True if state was updated via SSE in the last 5 seconds
        """
        last_update = self._last_state_update.get(hub_id, 0)
        return (time.time() - last_update) < 5

    async def _handle_event(self, event_data: dict[str, Any]) -> None:
        """Handle an SSE event.

        The proxy should send events in a format similar to SQS:
        {
            "event": {
                "eventTag": "Disarm",
                "eventCode": "M_22_00",
                "hubId": "002BB321",
                "timestamp": 1234567890,
                "source": {"name": "User Name", "type": "USER"},
                "device": {"id": "xxx", "name": "Device Name", "type": "DoorProtect"}
            }
        }

        Or a simplified format from the proxy:
        {
            "eventTag": "Disarm",
            "hubId": "002BB321",
            "sourceName": "User Name",
            ...
        }
        """
        try:
            # Handle both nested and flat event formats
            event = event_data.get("event", event_data)

            event_tag = event.get("eventTag", "").lower()
            hub_id = event.get("hubId")

            if not event_tag or not hub_id:
                _LOGGER.debug("SSE event missing eventTag or hubId: %s", event_data)
                return

            # Extract event details
            event_code = event.get("eventCode", "")

            # Try multiple ways to get source info (different proxy formats)
            source = event.get("source", {})
            device = event.get("device", {})

            # Source name: try device.name, source.name, sourceObjectName, sourceName
            source_name = (
                device.get("name")
                if isinstance(device, dict) and device.get("name")
                else (source.get("name") if isinstance(source, dict) else None)
            )
            if not source_name:
                source_name = event.get("sourceObjectName") or event.get(
                    "sourceName", ""
                )

            # Source ID: try device.id, sourceObjectId, deviceId
            source_id = (
                device.get("id")
                if isinstance(device, dict) and device.get("id")
                else event.get("sourceObjectId") or event.get("deviceId", "")
            )

            # Source type: try device.type, source.type, sourceObjectType, sourceType
            source_type = (
                device.get("type")
                if isinstance(device, dict) and device.get("type")
                else (source.get("type") if isinstance(source, dict) else None)
            )
            if not source_type:
                source_type = event.get("sourceObjectType") or event.get(
                    "sourceType", ""
                )

            # Parse event code for type info
            code_info = parse_event_code(event_code)
            event_type = code_info.get("type", "UNKNOWN") if code_info else "UNKNOWN"
            transition = (
                code_info.get("transition", "TRIGGERED") if code_info else "TRIGGERED"
            )

            _LOGGER.info(
                "SSE event: type=%s, tag=%s, code=%s, source=%s (%s), id=%s, transition=%s",
                event_type,
                event_tag,
                event_code,
                source_name,
                source_type,
                source_id,
                transition,
            )

            # Deduplication: ignore duplicate events within window
            event_key = f"{source_id}:{event_tag}:{transition}"
            now = time.time()
            last_time = self._recent_events.get(event_key, 0)
            if now - last_time < self._dedup_window:
                _LOGGER.debug(
                    "SSE event ignored (duplicate): %s, last seen %.1fs ago",
                    event_key,
                    now - last_time,
                )
                return
            self._recent_events[event_key] = now

            # Cleanup old entries (keep dict from growing indefinitely)
            self._recent_events = {
                k: v for k, v in self._recent_events.items() if now - v < 60
            }

            # Get space by hub_id
            space = None
            for s in self.coordinator.account.spaces.values():
                if s.hub_id == hub_id:
                    space = s
                    break

            if not space:
                _LOGGER.warning("SSE: Unknown hub %s", hub_id)
                return

            # Process event by type
            if event_tag in EVENT_TAG_TO_STATE:
                await self._handle_security_event(space, event_tag, source_name)
            elif event_tag in DOOR_EVENTS:
                self._handle_door_event(
                    space, event_tag, source_name, source_id, transition
                )
            elif event_tag in MOTION_EVENTS:
                self._handle_motion_event(space, event_tag, source_name, source_id)
            elif event_tag in SMOKE_EVENTS:
                self._handle_smoke_event(space, event_tag, source_name, source_id)
            elif event_tag in FLOOD_EVENTS:
                self._handle_flood_event(space, event_tag, source_name, source_id)
            elif event_tag in GLASS_EVENTS:
                self._handle_glass_event(space, event_tag, source_name, source_id)
            elif event_tag in TAMPER_EVENTS:
                self._handle_tamper_event(
                    space, event_tag, source_name, source_id, transition
                )
            elif event_tag in DEVICE_STATUS_EVENTS:
                self._handle_device_status_event(
                    space, event_tag, source_name, source_id
                )
            elif event_tag in RELAY_EVENTS:
                self._handle_relay_event(space, event_tag, source_name, source_id)
            elif event_tag in SCENARIO_EVENTS:
                self._handle_scenario_event(space, event, event_tag)
            else:
                _LOGGER.warning(
                    "SSE event not handled: tag=%s, type=%s, source=%s (id=%s)",
                    event_tag,
                    source_type,
                    source_name,
                    source_id,
                )

            # Notify HA of update
            self.coordinator.async_set_updated_data(self.coordinator.account)

        except Exception as err:
            _LOGGER.error("SSE event processing error: %s", err, exc_info=True)

    async def _handle_security_event(
        self, space, event_tag: str, source_name: str
    ) -> None:
        """Handle arm/disarm/night mode events."""
        new_state = EVENT_TAG_TO_STATE.get(event_tag)
        if not new_state:
            return

        old_state = space.security_state
        state_changed = old_state != new_state

        _LOGGER.info(
            "SSE security: tag=%s, old=%s, new=%s, changed=%s",
            event_tag,
            old_state.value,
            new_state.value,
            state_changed,
        )

        # Check if this was triggered by Home Assistant
        if self.coordinator.get_pending_ha_action(space.hub_id):
            source_name = "Home Assistant"

        # Group arm/disarm events need a FULL refresh to update group states
        is_group_event = event_tag in ("grouparm", "groupdisarm")
        if is_group_event:
            _LOGGER.debug(
                "SSE: Group event %s detected, forcing metadata refresh for group states",
                event_tag,
            )
            await self.coordinator.async_force_metadata_refresh()

        if state_changed and not is_group_event:
            space.security_state = new_state
            self._last_state_update[space.hub_id] = time.time()

        # Always create notification (even if state unchanged)
        _LOGGER.info(
            "SSE instant: %s -> %s par %s (state_changed=%s)",
            old_state.value,
            new_state.value,
            source_name or "inconnu",
            state_changed,
        )

        # Create notification
        await self.coordinator._create_sqs_notification(
            action=new_state.value,
            source_name=source_name,
            space_name=space.name,
        )

    def _find_device(self, space, source_name: str, source_id: str):
        """Find device by name or ID.

        Tries multiple matching strategies similar to SQS manager.
        """
        # Try by exact ID match first
        if source_id:
            if source_id in space.devices:
                return space.devices[source_id]

            # For WireInput devices: try matching by suffix (wire input index)
            if len(source_id) == 8:
                for device in space.devices.values():
                    if len(device.id) == 16 and device.id.endswith(source_id):
                        _LOGGER.debug(
                            "SSE: Matched device %s by suffix %s",
                            device.name,
                            source_id,
                        )
                        return device

        # Fall back to name match
        if source_name:
            for device in space.devices.values():
                if device.name == source_name:
                    return device

        return None

    def _handle_door_event(
        self, space, event_tag: str, source_name: str, source_id: str, transition: str
    ) -> None:
        """Handle door opened/closed events."""
        action_key, is_triggered = DOOR_EVENTS[event_tag]

        # Use transition to determine actual state
        if transition == "RECOVERED":
            is_triggered = False
        elif transition == "TRIGGERED":
            is_triggered = True

        dev = self._find_device(space, source_name, source_id)
        if dev:
            dev.attributes["door_opened"] = is_triggered
            dev.attributes["door_opened_at"] = datetime.now(timezone.utc).isoformat()
            _LOGGER.info("SSE instant: %s -> %s", dev.name, action_key)
        else:
            _LOGGER.warning(
                "SSE: Door device not found: name=%s, id=%s", source_name, source_id
            )

    def _handle_motion_event(
        self, space, event_tag: str, source_name: str, source_id: str
    ) -> None:
        """Handle motion detected events."""
        action_key, is_triggered = MOTION_EVENTS[event_tag]

        dev = self._find_device(space, source_name, source_id)
        if dev:
            dev.attributes["motion_detected"] = is_triggered
            dev.attributes["motion_detected_at"] = datetime.now(
                timezone.utc
            ).isoformat()
            _LOGGER.info("SSE instant: %s -> %s", dev.name, action_key)
        else:
            _LOGGER.warning(
                "SSE: Motion device not found: name=%s, id=%s", source_name, source_id
            )

    def _handle_smoke_event(
        self, space, event_tag: str, source_name: str, source_id: str
    ) -> None:
        """Handle smoke/fire detector events."""
        action_key, is_triggered = SMOKE_EVENTS[event_tag]

        dev = self._find_device(space, source_name, source_id)
        if dev:
            if "smoke" in action_key:
                dev.attributes["smoke_detected"] = is_triggered
            elif "temp" in action_key:
                dev.attributes["temperature_alert"] = is_triggered
            elif "co" in action_key:
                dev.attributes["co_detected"] = is_triggered
            _LOGGER.info("SSE instant: %s -> %s", dev.name, action_key)
        else:
            _LOGGER.warning(
                "SSE: Smoke device not found: name=%s, id=%s", source_name, source_id
            )

    def _handle_flood_event(
        self, space, event_tag: str, source_name: str, source_id: str
    ) -> None:
        """Handle flood/leak detector events."""
        action_key, is_triggered = FLOOD_EVENTS[event_tag]

        dev = self._find_device(space, source_name, source_id)
        if dev:
            dev.attributes["leak_detected"] = is_triggered
            _LOGGER.info("SSE instant: %s -> %s", dev.name, action_key)
        else:
            _LOGGER.warning(
                "SSE: Flood device not found: name=%s, id=%s", source_name, source_id
            )

    def _handle_glass_event(
        self, space, event_tag: str, source_name: str, source_id: str
    ) -> None:
        """Handle glass break events."""
        action_key, is_triggered = GLASS_EVENTS[event_tag]

        dev = self._find_device(space, source_name, source_id)
        if dev:
            dev.attributes["glass_break_detected"] = is_triggered
            _LOGGER.info("SSE instant: %s -> %s", dev.name, action_key)
        else:
            _LOGGER.warning(
                "SSE: Glass device not found: name=%s, id=%s", source_name, source_id
            )

    def _handle_tamper_event(
        self, space, event_tag: str, source_name: str, source_id: str, transition: str
    ) -> None:
        """Handle tamper events."""
        action_key, is_triggered = TAMPER_EVENTS[event_tag]

        # Use transition to determine actual state (like door events)
        if transition == "RECOVERED":
            is_triggered = False
        elif transition == "TRIGGERED":
            is_triggered = True

        dev = self._find_device(space, source_name, source_id)
        if dev:
            dev.attributes["tampered"] = is_triggered
            _LOGGER.info(
                "SSE instant: %s -> %s (transition=%s)",
                dev.name,
                action_key,
                transition,
            )
        else:
            _LOGGER.warning(
                "SSE: Device not found for tamper: name=%s, id=%s",
                source_name,
                source_id,
            )

    def _handle_device_status_event(
        self, space, event_tag: str, source_name: str, source_id: str
    ) -> None:
        """Handle device status events (online/offline, battery)."""
        action_key, is_problem = DEVICE_STATUS_EVENTS[event_tag]

        dev = self._find_device(space, source_name, source_id)
        if dev:
            if "online" in action_key or "offline" in action_key:
                dev.online = not is_problem
            elif "battery" in action_key:
                dev.attributes["low_battery"] = is_problem
            elif "power" in action_key:
                dev.attributes["external_power_lost"] = is_problem
            _LOGGER.info("SSE instant: %s -> %s", dev.name, action_key)
        else:
            _LOGGER.warning(
                "SSE: Device not found for status: name=%s, id=%s",
                source_name,
                source_id,
            )

    def _handle_relay_event(
        self, space, event_tag: str, source_name: str, source_id: str
    ) -> None:
        """Handle relay/socket on/off events."""
        action_key, is_on = RELAY_EVENTS[event_tag]

        dev = self._find_device(space, source_name, source_id)
        if dev:
            dev.attributes["is_on"] = is_on
            _LOGGER.info("SSE instant: %s -> %s", dev.name, action_key)
        else:
            _LOGGER.warning(
                "SSE: Relay device not found: name=%s, id=%s", source_name, source_id
            )

    def _handle_scenario_event(self, space, event: dict, event_tag: str) -> None:
        """Handle scenario events that might be triggered by a Button.

        When a Button is configured in 'Control' mode, Ajax doesn't send a direct
        button press event. Instead, it sends a scenario event (e.g., RelayOnByScenario).
        We extract the initiator info to identify the button and fire an HA event.
        """
        # Extract initiator info from additionalDataV2
        additional_data_v2 = event.get("additionalDataV2", [])
        source_name = event.get("sourceObjectName", "")

        initiator_name = None
        initiator_type = None
        for data in additional_data_v2:
            if data.get("additionalDataV2Type") == "INITIATOR_INFO":
                initiator_name = data.get("objectName")
                initiator_type = data.get("objectType")
                break

        if not initiator_name:
            _LOGGER.debug("SSE scenario: no initiator info found")
            return

        _LOGGER.info(
            "SSE scenario: %s triggered by %s (type=%s)",
            event_tag,
            initiator_name,
            initiator_type,
        )

        # Fire a Home Assistant event for automations
        self.coordinator.hass.bus.async_fire(
            "ajax_scenario_triggered",
            {
                "scenario_name": initiator_name,
                "initiator_type": initiator_type,
                "target_name": source_name,
                "event_tag": event_tag,
                "space_name": space.name,
            },
        )
