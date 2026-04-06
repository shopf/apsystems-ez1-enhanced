"""Config flow for APsystems local API integration."""

from __future__ import annotations

from typing import Any

from APsystemsEZ1 import APsystemsEZ1M
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_IP_ADDRESS, CONF_PORT

from .const import CONF_DEVICE_NAME, CONF_POLLING_INTERVAL, DEFAULT_DEVICE_NAME, DEFAULT_PORT, DOMAIN, MAX_POLLING_INTERVAL, MIN_POLLING_INTERVAL, POLLING_INTERVAL


class ApSystemsFlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for APsystems."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initiated by the user."""
        errors: dict[str, str] = {}

        if user_input is not None:
            ip = user_input[CONF_IP_ADDRESS]
            port = user_input.get(CONF_PORT, DEFAULT_PORT)
            device_name = user_input.get(CONF_DEVICE_NAME, DEFAULT_DEVICE_NAME).strip() or DEFAULT_DEVICE_NAME

            api = APsystemsEZ1M(ip_address=ip, port=port, timeout=8)
            try:
                device_info = await api.get_device_info()
            except Exception:  # noqa: BLE001
                errors["base"] = "cannot_connect"
            else:
                uid = device_info.deviceId
                await self.async_set_unique_id(uid)
                self._abort_if_unique_id_configured()
                polling_interval = user_input.get(CONF_POLLING_INTERVAL, POLLING_INTERVAL)
                return self.async_create_entry(
                    title=device_name,
                    data={
                        CONF_IP_ADDRESS: ip,
                        CONF_PORT: port,
                        CONF_DEVICE_NAME: device_name,
                        CONF_POLLING_INTERVAL: polling_interval,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_IP_ADDRESS): str,
                    vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
                    vol.Optional(CONF_DEVICE_NAME, default=DEFAULT_DEVICE_NAME): str,
                    vol.Optional(CONF_POLLING_INTERVAL, default=POLLING_INTERVAL): vol.All(
                        int,
                        vol.Range(min=MIN_POLLING_INTERVAL, max=MAX_POLLING_INTERVAL),
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                "min_interval": str(MIN_POLLING_INTERVAL),
                "max_interval": str(MAX_POLLING_INTERVAL),
            },
        )
