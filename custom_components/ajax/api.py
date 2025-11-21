"""Ajax REST API client for official API."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .const import (
    AJAX_REST_API_BASE_URL,
    AJAX_REST_API_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


class AjaxRestApiError(Exception):
    """Base exception for Ajax REST API errors."""


class AjaxRestAuthError(AjaxRestApiError):
    """Authentication error."""


class AjaxRestApi:
    """Ajax REST API client using integration_id and api_key."""

    def __init__(
        self,
        integration_id: str,
        api_key: str,
    ):
        """Initialize the API client.

        Args:
            integration_id: Integration ID provided by Ajax
            api_key: API Key provided by Ajax
        """
        self.integration_id = integration_id
        self.api_key = api_key
        self.session: aiohttp.ClientSession | None = None
        self._headers = {
            "X-Api-Key": api_key,
            "Content-Type": "application/json",
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def close(self):
        """Close the session."""
        if self.session and not self.session.closed:
            await self.session.close()

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
    ) -> Any:
        """Make API request.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint (without base URL)
            data: Optional JSON data for POST/PUT requests

        Returns:
            API response as dict

        Raises:
            AjaxRestAuthError: If authentication fails
            AjaxRestApiError: For other API errors
        """
        url = f"{AJAX_REST_API_BASE_URL}/{endpoint}"
        session = await self._get_session()

        try:
            async with session.request(
                method,
                url,
                headers=self._headers,
                json=data,
                timeout=aiohttp.ClientTimeout(total=AJAX_REST_API_TIMEOUT),
            ) as response:
                if response.status == 401:
                    raise AjaxRestAuthError("Invalid API key or integration ID")
                elif response.status == 403:
                    raise AjaxRestAuthError("Access denied")

                response.raise_for_status()
                return await response.json()

        except aiohttp.ClientError as err:
            _LOGGER.error(f"API request failed: {err}")
            raise AjaxRestApiError(f"API request failed: {err}") from err
        except asyncio.TimeoutError as err:
            _LOGGER.error("API request timeout")
            raise AjaxRestApiError("API request timeout") from err

    # Hub methods
    async def async_get_hubs(self) -> list[dict[str, Any]]:
        """Get all hubs.

        Returns:
            List of hub dictionaries
        """
        return await self._request("GET", "hubs")

    async def async_get_hub(self, hub_id: str) -> dict[str, Any]:
        """Get hub details.

        Args:
            hub_id: Hub ID

        Returns:
            Hub details dictionary
        """
        return await self._request("GET", f"hubs/{hub_id}")

    async def async_get_hub_mode(self, hub_id: str) -> dict[str, Any]:
        """Get hub alarm mode.

        Args:
            hub_id: Hub ID

        Returns:
            Hub mode dictionary
        """
        return await self._request("GET", f"hubs/{hub_id}/mode")

    async def async_set_hub_mode(self, hub_id: str, mode: str) -> dict[str, Any]:
        """Set hub alarm mode.

        Args:
            hub_id: Hub ID
            mode: Alarm mode (full, partial_1, night, disarmed)

        Returns:
            Updated hub mode
        """
        return await self._request("POST", f"hubs/{hub_id}/mode", {"mode": mode})

    # Device methods
    async def async_get_devices(self) -> list[dict[str, Any]]:
        """Get all devices.

        Returns:
            List of device dictionaries
        """
        return await self._request("GET", "devices")

    async def async_get_device(self, device_id: str) -> dict[str, Any]:
        """Get device details.

        Args:
            device_id: Device ID

        Returns:
            Device details dictionary
        """
        return await self._request("GET", f"devices/{device_id}")

    async def async_get_device_state(self, device_id: str) -> dict[str, Any]:
        """Get device state.

        Args:
            device_id: Device ID

        Returns:
            Device state dictionary
        """
        return await self._request("GET", f"devices/{device_id}/state")

    # Relay methods
    async def async_set_relay_state(self, device_id: str, state: bool) -> dict[str, Any]:
        """Set relay state (on/off).

        Args:
            device_id: Relay device ID
            state: True for on, False for off

        Returns:
            Updated relay state
        """
        return await self._request(
            "POST",
            f"devices/{device_id}/control",
            {"state": "on" if state else "off"}
        )

    async def async_pulse_relay(self, device_id: str, duration: int = 1) -> dict[str, Any]:
        """Trigger relay pulse (for gates, doors, etc.).

        Args:
            device_id: Relay device ID
            duration: Pulse duration in seconds

        Returns:
            Relay response
        """
        return await self._request(
            "POST",
            f"devices/{device_id}/control",
            {"action": "pulse", "duration": duration}
        )

    # Socket methods
    async def async_set_socket_state(self, device_id: str, state: bool) -> dict[str, Any]:
        """Set socket state (on/off).

        Args:
            device_id: Socket device ID
            state: True for on, False for off

        Returns:
            Updated socket state
        """
        return await self._request(
            "POST",
            f"devices/{device_id}/control",
            {"state": "on" if state else "off"}
        )

    async def async_get_socket_power(self, device_id: str) -> dict[str, Any]:
        """Get socket power consumption.

        Args:
            device_id: Socket device ID

        Returns:
            Power consumption data
        """
        return await self._request("GET", f"devices/{device_id}/power")

    # Camera methods
    async def async_get_cameras(self) -> list[dict[str, Any]]:
        """Get all cameras.

        Returns:
            List of camera dictionaries
        """
        return await self._request("GET", "cameras")

    async def async_get_camera(self, camera_id: str) -> dict[str, Any]:
        """Get camera details.

        Args:
            camera_id: Camera ID

        Returns:
            Camera details dictionary
        """
        return await self._request("GET", f"cameras/{camera_id}")

    async def async_get_camera_snapshot(self, camera_id: str) -> bytes:
        """Get camera snapshot.

        Args:
            camera_id: Camera ID

        Returns:
            Snapshot image data as bytes
        """
        url = f"{AJAX_REST_API_BASE_URL}/cameras/{camera_id}/snapshot"
        session = await self._get_session()

        async with session.get(url, headers=self._headers) as response:
            response.raise_for_status()
            return await response.read()

    async def async_get_camera_stream_url(self, camera_id: str) -> str:
        """Get camera stream URL.

        Args:
            camera_id: Camera ID

        Returns:
            Stream URL string
        """
        data = await self._request("GET", f"cameras/{camera_id}/stream")
        return data.get("url", "")

    # Light/Button methods
    async def async_set_light_state(
        self,
        device_id: str,
        state: bool,
        brightness: int | None = None,
    ) -> dict[str, Any]:
        """Set light state.

        Args:
            device_id: Light device ID
            state: True for on, False for off
            brightness: Optional brightness (0-100)

        Returns:
            Updated light state
        """
        payload = {"state": "on" if state else "off"}
        if brightness is not None:
            payload["brightness"] = brightness
        return await self._request("POST", f"devices/{device_id}/control", payload)

    # Automation methods
    async def async_get_automations(self, hub_id: str) -> list[dict[str, Any]]:
        """Get hub automations/scenarios.

        Args:
            hub_id: Hub ID

        Returns:
            List of automation dictionaries
        """
        return await self._request("GET", f"hubs/{hub_id}/automations")

    async def async_trigger_automation(
        self,
        hub_id: str,
        automation_id: str,
    ) -> dict[str, Any]:
        """Trigger automation/scenario.

        Args:
            hub_id: Hub ID
            automation_id: Automation ID

        Returns:
            Automation trigger response
        """
        return await self._request(
            "POST",
            f"hubs/{hub_id}/automations/{automation_id}/trigger"
        )

    # Events methods
    async def async_get_events(self, hub_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Get hub events history.

        Args:
            hub_id: Hub ID
            limit: Maximum number of events to return

        Returns:
            List of event dictionaries
        """
        return await self._request("GET", f"hubs/{hub_id}/events?limit={limit}")

    # NVR methods
    async def async_get_nvr_status(self, nvr_id: str) -> dict[str, Any]:
        """Get NVR status.

        Args:
            nvr_id: NVR device ID

        Returns:
            NVR status dictionary
        """
        return await self._request("GET", f"devices/{nvr_id}/status")

    async def async_get_nvr_recordings(
        self,
        nvr_id: str,
        camera_id: str,
        start: str,
        end: str,
    ) -> list[dict[str, Any]]:
        """Get NVR recordings for a camera.

        Args:
            nvr_id: NVR device ID
            camera_id: Camera ID
            start: Start time (ISO format)
            end: End time (ISO format)

        Returns:
            List of recording dictionaries
        """
        return await self._request(
            "GET",
            f"devices/{nvr_id}/recordings?cameraId={camera_id}&start={start}&end={end}"
        )
