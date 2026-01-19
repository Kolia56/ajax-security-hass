"""Ajax camera platform for Video Edge devices.

This module creates camera entities for Ajax VideoEdge surveillance cameras
using RTSP streaming.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AjaxConfigEntry
from .const import CONF_RTSP_PASSWORD, CONF_RTSP_USERNAME, DOMAIN, MANUFACTURER
from .coordinator import AjaxDataCoordinator
from .models import AjaxVideoEdge, VideoEdgeType

_LOGGER = logging.getLogger(__name__)

# Human-readable model names for video edge devices
VIDEO_EDGE_MODEL_NAMES = {
    VideoEdgeType.NVR: "NVR",
    VideoEdgeType.TURRET: "TurretCam",
    VideoEdgeType.TURRET_HL: "TurretCam HL",
    VideoEdgeType.BULLET: "BulletCam",
    VideoEdgeType.BULLET_HL: "BulletCam HL",
    VideoEdgeType.MINIDOME: "MiniDome",
    VideoEdgeType.MINIDOME_HL: "MiniDome HL",
    VideoEdgeType.UNKNOWN: "Video Edge",
}

# Default RTSP port (Ajax cameras use 8554, not standard 554)
DEFAULT_RTSP_PORT = 8554


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ajax camera entities from a config entry."""
    coordinator = entry.runtime_data

    entities: list[Camera] = []

    # Create camera entities for video edges
    for space in coordinator.data.spaces.values():
        for video_edge in space.video_edges.values():
            # Only create camera if we have an IP address
            if video_edge.ip_address:
                # Create main stream camera
                entities.append(
                    AjaxVideoEdgeCamera(
                        coordinator=coordinator,
                        entry=entry,
                        video_edge=video_edge,
                        space_id=space.id,
                        stream_type="main",
                    )
                )

    if entities:
        _LOGGER.debug("Adding %d camera entities", len(entities))
        async_add_entities(entities)


class AjaxVideoEdgeCamera(CoordinatorEntity[AjaxDataCoordinator], Camera):
    """Camera entity for Ajax Video Edge devices."""

    _attr_has_entity_name = True
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        entry: AjaxConfigEntry,
        video_edge: AjaxVideoEdge,
        space_id: str,
        stream_type: str = "main",
    ) -> None:
        """Initialize the camera entity."""
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)

        self._entry = entry
        self._video_edge_id = video_edge.id
        self._space_id = space_id
        self._stream_type = stream_type
        self._attr_unique_id = f"{video_edge.id}_camera_{stream_type}"

        # Camera name
        if stream_type == "main":
            self._attr_name = None  # Use device name
        else:
            self._attr_name = f"Stream {stream_type}"

        # Get human-readable model name
        model_name = VIDEO_EDGE_MODEL_NAMES.get(video_edge.video_edge_type, "Video Edge")
        color = video_edge.color.title() if video_edge.color else ""
        model_display = f"{model_name} ({color})" if color else model_name

        # Device info
        self._attr_device_info = {
            "identifiers": {(DOMAIN, video_edge.id)},
            "name": video_edge.name,
            "manufacturer": MANUFACTURER,
            "model": model_display,
            "sw_version": video_edge.firmware_version,
        }

    @property
    def _video_edge(self) -> AjaxVideoEdge | None:
        """Get the current video edge from coordinator data."""
        space = self.coordinator.data.spaces.get(self._space_id)
        if not space:
            return None
        return space.video_edges.get(self._video_edge_id)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        video_edge = self._video_edge
        if not video_edge:
            return False
        return video_edge.connection_state == "ONLINE"

    @property
    def is_streaming(self) -> bool:
        """Return True if the camera is streaming."""
        return self.available

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Return extra state attributes."""
        attrs = {}

        # Check if RTSP credentials are configured
        username = self._entry.options.get(CONF_RTSP_USERNAME, "")
        password = self._entry.options.get(CONF_RTSP_PASSWORD, "")

        if not username or not password:
            attrs["configuration_help"] = (
                "Pour afficher le flux vidéo, configurez les identifiants ONVIF "
                "dans Paramètres → Appareils et services → Ajax → Configurer → "
                "Identifiants RTSP/ONVIF"
            )

        return attrs if attrs else None

    @property
    def is_recording(self) -> bool:
        """Return True if the camera is recording.

        Note: Ajax cameras may record to NVR/cloud but we report False here
        so HA shows 'Streaming' instead of 'Recording' for better UX.
        """
        return False

    async def stream_source(self) -> str | None:
        """Return the RTSP stream source URL.

        Ajax cameras use a specific RTSP URL format:
        Format: rtsp://[user:pass@]IP:8554/{mac_without_colons}-{channel}_{stream}
        Where:
        - mac_without_colons: MAC address in lowercase without colons
        - channel: 0 for first channel
        - stream: 'm' for main stream, 's' for sub stream
        """
        video_edge = self._video_edge
        if not video_edge or not video_edge.ip_address:
            return None

        # Need MAC address to build the stream path
        if not video_edge.mac_address:
            _LOGGER.warning("No MAC address for %s, cannot build RTSP URL", video_edge.name)
            return None

        # Build RTSP URL
        ip = video_edge.ip_address
        port = DEFAULT_RTSP_PORT

        # Build stream path from MAC address
        # Format: {mac_without_colons}-{channel}_{stream_type}
        mac_clean = video_edge.mac_address.replace(":", "").lower()
        channel_num = "0"  # First channel
        stream_suffix = "m" if self._stream_type == "main" else "s"
        stream_path = f"{mac_clean}-{channel_num}_{stream_suffix}"

        # Get RTSP credentials from options
        username = self._entry.options.get(CONF_RTSP_USERNAME, "")
        password = self._entry.options.get(CONF_RTSP_PASSWORD, "")

        # Build URL with or without credentials
        if username and password:
            # URL-encode credentials to handle special characters
            encoded_user = quote(username, safe="")
            encoded_pass = quote(password, safe="")
            rtsp_url = f"rtsp://{encoded_user}:{encoded_pass}@{ip}:{port}/{stream_path}"
            _LOGGER.debug("Stream source for %s: rtsp://***:***@%s:%s/%s", video_edge.name, ip, port, stream_path)
        else:
            rtsp_url = f"rtsp://{ip}:{port}/{stream_path}"
            _LOGGER.debug("Stream source for %s: %s (no credentials configured)", video_edge.name, rtsp_url)

        return rtsp_url

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """Return a still image from the camera.

        For RTSP cameras, we rely on the stream component to handle snapshots.
        """
        # Let Home Assistant handle snapshot from stream
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
