"""Spaces polling mixin for ``AjaxDataCoordinator``.

Owns ``_async_update_spaces_from_hubs`` — the per-tick reconciliation of
the Ajax hubs / spaces / rooms / users / groups tree against the
in-memory account. It is the entry point of every coordinator refresh:
called from ``_async_update_data`` with ``full_refresh=True`` on metadata
ticks and ``False`` on light state-only ticks.

State stays on ``self``; the mixin owns no attributes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .api import AjaxRestApiError, AjaxRestAuthError
from .models import AjaxGroup, AjaxRoom, AjaxSpace, GroupState

if TYPE_CHECKING:
    from .api import AjaxRestApi
    from .models import AjaxAccount, SecurityState

_LOGGER = logging.getLogger(__name__)


class AjaxSpacesMixin:
    """Coordinator mixin: per-tick spaces / rooms / users / groups refresh."""

    # Host attributes — provided by the coordinator __init__.
    if TYPE_CHECKING:
        account: AjaxAccount | None
        api: AjaxRestApi
        all_discovered_spaces: dict[str, str]
        sqs_manager: Any | None
        sse_manager: Any | None
        _enabled_spaces: list[str] | None
        _space_binding_cache: dict[str, dict]
        _skip_state_change_event: bool

        def _parse_security_state(self, value: Any) -> SecurityState: ...
        def has_pending_ha_action(self, hub_id: str) -> bool: ...
        def _update_polling_interval(self, state: SecurityState) -> None: ...
        def _fire_security_state_event(self, *args: Any, **kwargs: Any) -> None: ...
        def _create_event_from_state_change(self, *args: Any, **kwargs: Any) -> None: ...

    async def _async_update_spaces_from_hubs(self, full_refresh: bool = True) -> None:
        """Update spaces by fetching hubs directly (use hub_id as space_id).

        Args:
            full_refresh: If True, fetch all metadata (rooms, users, groups).
                         If False, only fetch hub state (light polling).
        """
        if self.account is None:
            return

        hubs_data = await self.api.async_get_hubs()

        for hub_data in hubs_data:
            hub_id = hub_data.get("hubId")
            if not hub_id:
                continue

            # Store all discovered spaces for options flow
            # First use hubName as fallback
            hub_name = hub_data.get("hubName", f"Hub {hub_id[:6]}")
            self.all_discovered_spaces[hub_id] = hub_name

            # Try to get the proper space name for all spaces (including disabled).
            # Cache the result so we don't hit the API on every tick — refresh
            # only on full_refresh or when the entry is missing.
            cached_binding = self._space_binding_cache.get(hub_id)
            if cached_binding is None or full_refresh:
                try:
                    space_binding = await self.api.async_get_space_by_hub(hub_id)
                    if space_binding:
                        self._space_binding_cache[hub_id] = space_binding
                        cached_binding = space_binding
                except AjaxRestApiError as err:
                    _LOGGER.debug("Could not resolve space name for %s: %s", hub_id, err)
            if cached_binding and cached_binding.get("name"):
                hub_name = str(cached_binding.get("name"))
                self.all_discovered_spaces[hub_id] = hub_name

            # Skip spaces that are not enabled
            if self._enabled_spaces is not None and hub_id not in self._enabled_spaces:
                _LOGGER.debug("Skipping disabled space: %s (%s)", hub_name, hub_id)
                continue

            # Get hub details to get the name and state
            try:
                hub_details = await self.api.async_get_hub(hub_id)

                # Get space details only on full refresh or for new spaces
                space_id = hub_id
                is_new_space = space_id not in self.account.spaces

                space_name = None
                real_space_id = None
                rooms_data: list[dict] = []
                rooms_map: dict = {}

                if full_refresh or is_new_space:
                    # Full refresh: use the cached space_binding resolved above
                    # rather than re-hitting the API.
                    space_binding = self._space_binding_cache.get(hub_id)
                    if space_binding:
                        space_name = space_binding.get("name")
                        real_space_id = space_binding.get("id")
                        _LOGGER.debug(
                            "Found space '%s' (id: %s) for hub %s",
                            space_name,
                            real_space_id,
                            hub_id,
                        )

                    # Get rooms for this hub
                    try:
                        rooms_data = await self.api.async_get_rooms(hub_id)
                        # Build room_id -> room_name mapping
                        rooms_map = {room.get("id"): room.get("roomName") for room in rooms_data if room.get("id")}
                        _LOGGER.debug(
                            "Loaded %d rooms for hub %s",
                            len(rooms_map),
                            hub_id,
                        )
                    except Exception as room_err:
                        _LOGGER.warning("Could not get rooms for hub %s: %s", hub_id, room_err)
                else:
                    # Light refresh: reuse existing metadata
                    existing_space = self.account.spaces[space_id]
                    space_name = existing_space.name
                    real_space_id = existing_space.real_space_id
                    rooms_map = existing_space.rooms_map

                # Try to get name: prefer space name, fallback to hub details
                hub_name = (
                    space_name  # Space name from /spaces endpoint (e.g., "Maison")
                    or hub_details.get("name")
                    or hub_details.get("hubName")
                    or hub_details.get("deviceName")
                    or f"Hub {hub_id[:6]}"  # Use first 6 chars of hub_id as fallback
                )
                # Update all_discovered_spaces with the proper name
                self.all_discovered_spaces[hub_id] = hub_name

                # Parse security state from hub details
                # Check night mode first - it can be active even when groups are disarmed
                hub_state = hub_details.get("state", "DISARMED")
                # Night mode can be in dedicated fields OR in the state string itself
                # e.g., state="DISARMED_NIGHT_MODE_ON" means night mode is active
                night_mode_active = (
                    hub_details.get("nightMode")
                    or hub_details.get("nightModeEnabled")
                    or hub_details.get("nightModeActive")
                    or hub_details.get("isNightMode")
                    or "NIGHT_MODE_ON" in hub_state.upper()
                )
                _LOGGER.debug(
                    "Hub %s state parsing: state=%s, night_mode_active=%s",
                    hub_id,
                    hub_state,
                    night_mode_active,
                )
                if night_mode_active:
                    security_state = SecurityState.NIGHT_MODE
                else:
                    security_state = self._parse_security_state(hub_state)
            except (AjaxRestApiError, AjaxRestAuthError) as err:
                _LOGGER.warning("Failed to get hub details for %s: %s", hub_id, err)
                hub_name = f"Hub {hub_id}"
                security_state = SecurityState.NONE
                hub_details = {}
                rooms_data = []
                rooms_map = {}

            # Use hub_id as space_id since we're mapping 1:1
            space_id = hub_id

            # Create or update space
            if space_id not in self.account.spaces:
                # New space - always use API state for initial value
                space = AjaxSpace(
                    id=space_id,
                    name=hub_name,
                    hub_id=hub_id,
                    real_space_id=real_space_id,  # Actual space ID for video edges
                    security_state=security_state,  # Use API state at creation
                    hub_details=hub_details,  # Store all hub information
                )
                self.account.spaces[space_id] = space
                _LOGGER.info(
                    "Added new space from hub: %s (hub_id: %s, initial state: %s)",
                    space.name,
                    space.hub_id,
                    security_state,
                )

                # Set initial polling interval based on security state
                self._update_polling_interval(security_state)
            else:
                # Existing space - update name, hub_id, hub_details, and potentially state
                space = self.account.spaces[space_id]
                space.name = hub_name
                space.hub_id = hub_id
                space.real_space_id = real_space_id  # Update real space ID
                space.hub_details = hub_details  # Update hub information

            # Only update rooms, users on full refresh (they rarely change)
            if full_refresh or is_new_space:
                # Store rooms mapping in space for device room name lookup
                space.rooms_map = rooms_map

                # Populate space.rooms with AjaxRoom objects
                for room_data in rooms_data:
                    room_id = room_data.get("id")
                    if room_id:
                        space.rooms[room_id] = AjaxRoom(
                            id=room_id,
                            name=room_data.get("roomName", f"Room {room_id}"),
                            space_id=space_id,
                            image_id=room_data.get("imageId"),
                            image_url=room_data.get("imageUrl"),
                        )

                # Fetch users for this hub
                try:
                    users_data = await self.api.async_get_users(hub_id)
                    space.users = users_data
                except (AjaxRestApiError, AjaxRestAuthError):
                    space.users = []

            # Fetch groups on every poll by default. When SSE/SQS is active,
            # group arm/disarm transitions are pushed in real time and a full
            # metadata refresh is forced via _force_metadata_refresh after
            # each security event — so we can skip the per-tick groups fetch
            # on light cycles, which was a significant share of API calls.
            groups_enabled = hub_details.get("groupsEnabled", False)
            space.group_mode_enabled = groups_enabled
            realtime_active = (self.sse_manager is not None) or (self.sqs_manager is not None)
            should_fetch_groups = groups_enabled and (full_refresh or not realtime_active)
            if should_fetch_groups:
                # Check if HA recently triggered an action (protect optimistic updates)
                ha_action_pending = self.has_pending_ha_action(hub_id)
                try:
                    groups_data = await self.api.async_get_groups(hub_id)
                    _LOGGER.debug(
                        "Hub %s: API returned %d groups, raw states: %s",
                        hub_id,
                        len(groups_data),
                        [(g.get("groupName"), g.get("state")) for g in groups_data],
                    )
                    for group_data in groups_data:
                        group_id = group_data.get("id")
                        if group_id:
                            # Parse group state
                            group_state_str = group_data.get("state", "DISARMED")
                            if group_state_str == "ARMED":
                                group_state = GroupState.ARMED
                            elif group_state_str == "DISARMED":
                                group_state = GroupState.DISARMED
                            else:
                                group_state = GroupState.NONE

                            # Check if group already exists
                            existing_group = space.groups.get(group_id)
                            if existing_group and ha_action_pending:
                                # Protect optimistic update - keep existing state
                                _LOGGER.debug(
                                    "Group %s: REST has %s but HA action pending, keeping %s",
                                    group_id,
                                    group_state.value,
                                    existing_group.state.value,
                                )
                                group_state = existing_group.state

                            space.groups[group_id] = AjaxGroup(
                                id=group_id,
                                name=group_data.get("groupName", f"Group {group_id}"),
                                space_id=space_id,
                                state=group_state,
                                bulk_arm_involved=group_data.get("bulkArmInvolved", False),
                                bulk_disarm_involved=group_data.get("bulkDisarmInvolved", False),
                            )
                    # Log group states for debugging
                    group_states = [f"{g.name}={g.state.value}" for g in space.groups.values()]
                    _LOGGER.debug(
                        "Hub %s: Updated %d groups: %s",
                        hub_id,
                        len(space.groups),
                        ", ".join(group_states) if group_states else "none",
                    )
                except (AjaxRestApiError, AjaxRestAuthError) as err:
                    _LOGGER.warning("Failed to get groups for hub %s: %s", hub_id, err)

            # Check if SQS/SSE recently updated this hub's state
            # If so, don't overwrite with potentially stale REST data
            old_state = space.security_state
            if old_state != security_state:
                # Check real-time event protection (don't overwrite recent updates)
                sqs_protected = self.sqs_manager and self.sqs_manager.is_state_protected(hub_id)
                sse_protected = self.sse_manager and self.sse_manager.is_state_protected(hub_id)
                if sqs_protected or sse_protected:
                    _LOGGER.debug(
                        "Hub %s: REST has %s but real-time event recently set %s (protected)",
                        hub_id,
                        security_state.value,
                        old_state.value,
                    )
                else:
                    space.security_state = security_state
                    _LOGGER.info(
                        "Hub %s: %s -> %s",
                        hub_id,
                        old_state.value,
                        security_state.value,
                    )

                    # Update polling interval based on new state
                    self._update_polling_interval(security_state)

                    # Create event from state change (skip if SQS already created it)
                    if self._skip_state_change_event:
                        _LOGGER.debug("Skipping state change event (SQS already created it)")
                    else:
                        self._create_event_from_state_change(space, old_state, security_state)
