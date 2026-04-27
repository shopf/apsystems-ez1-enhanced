"""Config flow for APsystems local API integration."""

from __future__ import annotations

from typing import Any

from APsystemsEZ1 import APsystemsEZ1M
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_IP_ADDRESS, CONF_PORT

from .const import (
    CONF_DEVICE_NAME,
    CONF_LIFETIME_OFFSET_P1,
    CONF_LIFETIME_OFFSET_P2,
    CONF_POLLING_INTERVAL,
    DEFAULT_DEVICE_NAME,
    DEFAULT_PORT,
    DOMAIN,
    MAX_POLLING_INTERVAL,
    MIN_POLLING_INTERVAL,
    POLLING_INTERVAL,
)


def _parse_offset(raw: str | None) -> float:
    """Parse a user-entered kWh offset string to float.

    Accepts empty string or None as 0.0. Raises ValueError for invalid input.
    """
    if raw is None or str(raw).strip() == "":
        return 0.0
    return float(str(raw).strip().replace(",", "."))


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
            device_name = (
                user_input.get(CONF_DEVICE_NAME, DEFAULT_DEVICE_NAME).strip()
                or DEFAULT_DEVICE_NAME
            )

            try:
                offset_p1 = _parse_offset(user_input.get(CONF_LIFETIME_OFFSET_P1))
                offset_p2 = _parse_offset(user_input.get(CONF_LIFETIME_OFFSET_P2))
            except ValueError:
                errors[CONF_LIFETIME_OFFSET_P1] = "invalid_offset"

            if not errors:
                api = APsystemsEZ1M(ip_address=ip, port=port, timeout=8)
                try:
                    device_info = await api.get_device_info()
                except Exception:  # noqa: BLE001
                    errors["base"] = "cannot_connect"
                else:
                    uid = device_info.deviceId
                    await self.async_set_unique_id(uid)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=device_name,
                        data={
                            CONF_IP_ADDRESS: ip,
                            CONF_PORT: port,
                            CONF_DEVICE_NAME: device_name,
                            CONF_POLLING_INTERVAL: user_input.get(
                                CONF_POLLING_INTERVAL, POLLING_INTERVAL
                            ),
                            CONF_LIFETIME_OFFSET_P1: offset_p1,
                            CONF_LIFETIME_OFFSET_P2: offset_p2,
                        },
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_IP_ADDRESS): str,
                    vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
                    vol.Optional(CONF_DEVICE_NAME, default=DEFAULT_DEVICE_NAME): str,
                    vol.Optional(
                        CONF_POLLING_INTERVAL, default=POLLING_INTERVAL
                    ): vol.All(
                        int,
                        vol.Range(min=MIN_POLLING_INTERVAL, max=MAX_POLLING_INTERVAL),
                    ),
                    vol.Optional(CONF_LIFETIME_OFFSET_P1, default=""): str,
                    vol.Optional(CONF_LIFETIME_OFFSET_P2, default=""): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "min_interval": str(MIN_POLLING_INTERVAL),
                "max_interval": str(MAX_POLLING_INTERVAL),
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Allow the user to correct the lifetime offset after initial setup."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        current_p1 = entry.data.get(CONF_LIFETIME_OFFSET_P1, 0.0)
        current_p2 = entry.data.get(CONF_LIFETIME_OFFSET_P2, 0.0)

        if user_input is not None:
            try:
                offset_p1 = _parse_offset(user_input.get(CONF_LIFETIME_OFFSET_P1))
                offset_p2 = _parse_offset(user_input.get(CONF_LIFETIME_OFFSET_P2))
            except ValueError:
                errors[CONF_LIFETIME_OFFSET_P1] = "invalid_offset"

            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_IP_ADDRESS: user_input.get(
                            CONF_IP_ADDRESS, entry.data[CONF_IP_ADDRESS]
                        ),
                        CONF_PORT: user_input.get(
                            CONF_PORT, entry.data.get(CONF_PORT, DEFAULT_PORT)
                        ),
                        CONF_POLLING_INTERVAL: user_input.get(
                            CONF_POLLING_INTERVAL,
                            entry.data.get(CONF_POLLING_INTERVAL, POLLING_INTERVAL),
                        ),
                        CONF_LIFETIME_OFFSET_P1: offset_p1,
                        CONF_LIFETIME_OFFSET_P2: offset_p2,
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_IP_ADDRESS, default=entry.data[CONF_IP_ADDRESS]
                    ): str,
                    vol.Optional(
                        CONF_PORT, default=entry.data.get(CONF_PORT, DEFAULT_PORT)
                    ): int,
                    vol.Optional(
                        CONF_POLLING_INTERVAL,
                        default=entry.data.get(CONF_POLLING_INTERVAL, POLLING_INTERVAL),
                    ): vol.All(
                        int,
                        vol.Range(min=MIN_POLLING_INTERVAL, max=MAX_POLLING_INTERVAL),
                    ),
                    vol.Optional(
                        CONF_LIFETIME_OFFSET_P1,
                        default=str(current_p1) if current_p1 else "",
                    ): str,
                    vol.Optional(
                        CONF_LIFETIME_OFFSET_P2,
                        default=str(current_p2) if current_p2 else "",
                    ): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "min_interval": str(MIN_POLLING_INTERVAL),
                "max_interval": str(MAX_POLLING_INTERVAL),
            },
        )
