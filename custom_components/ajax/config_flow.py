"""Config flow for Ajax integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .api import AjaxRestApi, AjaxRestApiError, AjaxRestAuthError
from .const import (
    CONF_API_KEY,
    CONF_AWS_ACCESS_KEY,
    CONF_AWS_REGION,
    CONF_AWS_SECRET_KEY,
    CONF_EVENTS_QUEUE,
    CONF_INTEGRATION_ID,
    DEFAULT_AWS_REGION,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class AjaxConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ajax Security Systems."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step - API credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate API credentials
            try:
                api = AjaxRestApi(
                    user_input[CONF_INTEGRATION_ID],
                    user_input[CONF_API_KEY],
                )

                # Test API connection by getting hubs
                await api.async_get_hubs()
                await api.close()

                # Create entry with credentials
                return self.async_create_entry(
                    title=f"Ajax - {user_input[CONF_INTEGRATION_ID]}",
                    data=user_input,
                )

            except AjaxRestAuthError:
                _LOGGER.error("Invalid API credentials")
                errors["base"] = "invalid_auth"
            except AjaxRestApiError as err:
                _LOGGER.error("Cannot connect to Ajax API: %s", err)
                errors["base"] = "cannot_connect"
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception: %s", err)
                errors["base"] = "unknown"

        # Show configuration form
        data_schema = vol.Schema(
            {
                vol.Required(CONF_INTEGRATION_ID): str,
                vol.Required(CONF_API_KEY): str,
                vol.Optional(CONF_AWS_ACCESS_KEY): str,
                vol.Optional(CONF_AWS_SECRET_KEY): str,
                vol.Optional(CONF_EVENTS_QUEUE): str,
                vol.Optional(CONF_AWS_REGION, default=DEFAULT_AWS_REGION): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> AjaxOptionsFlowHandler:
        """Get the options flow for this handler."""
        return AjaxOptionsFlowHandler(config_entry)


class AjaxOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Ajax integration."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options - update AWS SQS credentials."""
        if user_input is not None:
            # Update config entry with new AWS credentials
            new_data = {**self.config_entry.data}

            # Update AWS SQS fields if provided
            if CONF_AWS_ACCESS_KEY in user_input:
                new_data[CONF_AWS_ACCESS_KEY] = user_input[CONF_AWS_ACCESS_KEY]
            if CONF_AWS_SECRET_KEY in user_input:
                new_data[CONF_AWS_SECRET_KEY] = user_input[CONF_AWS_SECRET_KEY]
            if CONF_EVENTS_QUEUE in user_input:
                new_data[CONF_EVENTS_QUEUE] = user_input[CONF_EVENTS_QUEUE]
            if CONF_AWS_REGION in user_input:
                new_data[CONF_AWS_REGION] = user_input[CONF_AWS_REGION]

            # Update the config entry
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data=new_data,
            )

            # Reload the integration to apply changes
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)

            return self.async_create_entry(title="", data={})

        # Show options form with current values
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_AWS_ACCESS_KEY,
                        default=self.config_entry.data.get(CONF_AWS_ACCESS_KEY, ""),
                    ): str,
                    vol.Optional(
                        CONF_AWS_SECRET_KEY,
                        default=self.config_entry.data.get(CONF_AWS_SECRET_KEY, ""),
                    ): str,
                    vol.Optional(
                        CONF_EVENTS_QUEUE,
                        default=self.config_entry.data.get(CONF_EVENTS_QUEUE, ""),
                    ): str,
                    vol.Optional(
                        CONF_AWS_REGION,
                        default=self.config_entry.data.get(
                            CONF_AWS_REGION, DEFAULT_AWS_REGION
                        ),
                    ): str,
                }
            ),
        )
