"""The Ajax Security System integration."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .api import AjaxRestApi, AjaxRestApiError, AjaxRestAuthError
from .const import (
    CONF_API_KEY,
    CONF_AWS_ACCESS_KEY,
    CONF_AWS_REGION,
    CONF_AWS_SECRET_KEY,
    CONF_EVENTS_QUEUE,
    CONF_INTEGRATION_ID,
    DEFAULT_AWS_REGION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import AjaxDataCoordinator
from .sqs_client import AjaxSQSClient

if TYPE_CHECKING:
    from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Ajax Security System component."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Ajax Security System from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Get API credentials
    integration_id = entry.data[CONF_INTEGRATION_ID]
    api_key = entry.data[CONF_API_KEY]

    # Get AWS SQS configuration (optional)
    aws_access_key = entry.data.get(CONF_AWS_ACCESS_KEY)
    aws_secret_key = entry.data.get(CONF_AWS_SECRET_KEY)
    events_queue = entry.data.get(CONF_EVENTS_QUEUE)
    aws_region = entry.data.get(CONF_AWS_REGION, DEFAULT_AWS_REGION)

    # Create REST API instance
    api = AjaxRestApi(
        integration_id=integration_id,
        api_key=api_key,
    )

    try:
        # Test API connection by getting hubs
        await api.async_get_hubs()
        _LOGGER.info("Successfully connected to Ajax REST API")

    except AjaxRestAuthError as err:
        _LOGGER.error("Authentication failed: %s", err)
        return False
    except AjaxRestApiError as err:
        _LOGGER.error("API error during setup: %s", err)
        raise ConfigEntryNotReady from err

    # Create coordinator
    coordinator = AjaxDataCoordinator(hass, api)

    # Initialize AWS SQS client if credentials are provided
    if aws_access_key and aws_secret_key and events_queue:
        _LOGGER.info("Initializing AWS SQS client for real-time events")
        try:
            sqs_client = AjaxSQSClient(
                aws_access_key=aws_access_key,
                aws_secret_key=aws_secret_key,
                queue_name=events_queue,
                region=aws_region,
            )
            # Set event callback to coordinator's handle_event method
            sqs_client.set_event_callback(coordinator.handle_event)
            coordinator.sqs_client = sqs_client
            # Start SQS listener
            await sqs_client.start()
            _LOGGER.info("AWS SQS listener started successfully")
        except Exception as err:
            _LOGGER.error("Failed to initialize AWS SQS client: %s", err)
            _LOGGER.info("Will use polling fallback (%ss interval)", DEFAULT_SCAN_INTERVAL)
    else:
        _LOGGER.info(
            "AWS SQS not configured, using polling fallback (%ss interval)",
            DEFAULT_SCAN_INTERVAL,
        )

    # Store coordinator
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS
    ):
        coordinator: AjaxDataCoordinator = hass.data[DOMAIN].pop(entry.entry_id)

        # Stop SQS client if running
        if hasattr(coordinator, "sqs_client") and coordinator.sqs_client:
            _LOGGER.info("Stopping AWS SQS client")
            await coordinator.sqs_client.stop()

        # Close API connection
        await coordinator.api.close()

    return unload_ok
