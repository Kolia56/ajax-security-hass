"""Config flow for Ajax integration."""
from __future__ import annotations

import logging
from typing import Any
import uuid

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .api import AjaxApi, AjaxApiError, AjaxAuthError
from .const import (
    DOMAIN,
    CONF_DEVICE_ID,
    CONF_PERSISTENT_NOTIFICATION,
    CONF_NOTIFICATION_FILTER,
    NOTIFICATION_FILTER_NONE,
    NOTIFICATION_FILTER_ALARMS_ONLY,
    NOTIFICATION_FILTER_SECURITY_EVENTS,
    NOTIFICATION_FILTER_ALL,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """
    # Generate a unique device ID if not provided
    device_id = data.get(CONF_DEVICE_ID)
    if not device_id:
        device_id = f"homeassistant_{uuid.uuid4().hex[:16]}"

    # Create API client
    api = AjaxApi(
        email=data[CONF_EMAIL],
        password=data[CONF_PASSWORD],
        device_id=device_id,
    )

    # Try to authenticate
    try:
        login_result = await api.async_login()
    except AjaxAuthError as err:
        raise InvalidAuth from err
    except AjaxApiError as err:
        raise CannotConnect from err
    finally:
        await api.close()

    # Return info that you want to store in the config entry
    return {
        "title": f"Ajax - {login_result.get('user_name', data[CONF_EMAIL])}",
        "device_id": device_id,
        "user_id": login_result.get("user_id"),
    }


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ajax."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                # Set unique ID based on user ID
                await self.async_set_unique_id(info["user_id"])
                self._abort_if_unique_id_configured()

                # Create config entry
                return self.async_create_entry(
                    title=info["title"],
                    data={
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_DEVICE_ID: info["device_id"],
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Ajax integration."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_PERSISTENT_NOTIFICATION,
                    default=self.config_entry.options.get(
                        CONF_PERSISTENT_NOTIFICATION, False
                    ),
                ): bool,
                vol.Optional(
                    CONF_NOTIFICATION_FILTER,
                    default=self.config_entry.options.get(
                        CONF_NOTIFICATION_FILTER, NOTIFICATION_FILTER_NONE
                    ),
                ): vol.In(
                    {
                        NOTIFICATION_FILTER_NONE: "None",
                        NOTIFICATION_FILTER_ALARMS_ONLY: "Alarms only",
                        NOTIFICATION_FILTER_SECURITY_EVENTS: "Security events (alarms + arming)",
                        NOTIFICATION_FILTER_ALL: "All notifications",
                    }
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=options_schema)


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
