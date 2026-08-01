"""Config flow for APsystems local API integration."""

from __future__ import annotations

from typing import Any

from APsystemsEZ1 import APsystemsEZ1M
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_IP_ADDRESS, CONF_PORT
from homeassistant.helpers.storage import Store

from .const import (
    CONF_BATTERY_SYSTEM,
    CONF_DETAIL_POLL,
    CONF_DEVICE_NAME,
    CONF_LIFETIME_OFFSET_P1,
    CONF_LIFETIME_OFFSET_P2,
    CONF_POLLING_INTERVAL,
    CONF_SHOWN_OFFSET_P1,
    CONF_SHOWN_OFFSET_P2,
    CONF_SLOW_DETAIL_POLL,
    DEFAULT_DEVICE_NAME,
    DEFAULT_PORT,
    DOMAIN,
    MAX_POLLING_INTERVAL,
    MIN_POLLING_INTERVAL,
    POLLING_INTERVAL,
    STORE_KEY,
    STORE_VERSION,
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
                            CONF_BATTERY_SYSTEM: user_input.get(CONF_BATTERY_SYSTEM, False),
                            CONF_DETAIL_POLL: user_input.get(CONF_DETAIL_POLL, True),
                            # Auto-reset slow_detail when detail is disabled
                            CONF_SLOW_DETAIL_POLL: (
                                user_input.get(CONF_SLOW_DETAIL_POLL, False)
                                if user_input.get(CONF_DETAIL_POLL, True) else False
                            ),
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
                    vol.Optional(CONF_BATTERY_SYSTEM, default=False): bool,
                    vol.Optional(CONF_DETAIL_POLL, default=True): bool,
                    vol.Optional(CONF_SLOW_DETAIL_POLL, default=False): bool,
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
        current_battery = entry.data.get(CONF_BATTERY_SYSTEM, False)
        current_detail_poll = entry.data.get(CONF_DETAIL_POLL, True)
        current_slow_detail = entry.data.get(CONF_SLOW_DETAIL_POLL, False)

        # Load storage once when the form is first shown (user_input is None).
        # We detect the bug scenario where storage holds a non-zero offset that
        # the user never deliberately set (config entry still shows 0.0).
        # In that case we pre-fill the dialog with the actual stored value so
        # the user can see and correct it.  A previously deliberate entry
        # (current_p1 != 0) is always preserved and never overwritten.
        if user_input is None:
            store = Store(
                self.hass, STORE_VERSION,
                f"{STORE_KEY}_{entry.entry_id}",
            )
            stored = await store.async_load() or {}
            stored_te1 = float(stored.get("te1_offset", 0.0))
            stored_te2 = float(stored.get("te2_offset", 0.0))

            # Bug-recovery: storage has an unexpected offset, config entry is 0.
            cfg_p1_zero = abs(current_p1) < 0.0001
            cfg_p2_zero = abs(current_p2) < 0.0001
            storage_has_offset = abs(stored_te1) > 0.0001 or abs(stored_te2) > 0.0001

            if cfg_p1_zero and cfg_p2_zero and storage_has_offset:
                self._prefill_p1 = stored_te1
                self._prefill_p2 = stored_te2
                self._offset_warning = True
            else:
                self._prefill_p1 = current_p1
                self._prefill_p2 = current_p2
                self._offset_warning = False

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
                        CONF_BATTERY_SYSTEM: user_input.get(CONF_BATTERY_SYSTEM, False),
                        CONF_DETAIL_POLL: user_input.get(CONF_DETAIL_POLL, True),
                        # Auto-reset slow_detail when detail is disabled
                        CONF_SLOW_DETAIL_POLL: (
                            user_input.get(CONF_SLOW_DETAIL_POLL, False)
                            if user_input.get(CONF_DETAIL_POLL, True) else False
                        ),
                        # Record what was shown to the user – the coordinator uses
                        # this as the delta reference and removes it after applying.
                        CONF_SHOWN_OFFSET_P1: self._prefill_p1,
                        CONF_SHOWN_OFFSET_P2: self._prefill_p2,
                    },
                )

        # Build description placeholder: empty string for normal flow, warning
        # text for the bug-recovery case.
        if self._offset_warning:
            offset_info = (
                f"⚠️ A stored offset of {self._prefill_p1:.5f} kWh (P1) / "
                f"{self._prefill_p2:.5f} kWh (P2) was detected in storage that "
                f"differs from your saved configuration (which shows 0). "
                f"This is likely caused by an integration bug in an earlier version. "
                f"The fields below have been pre-filled with the stored values. "
                f"To remove the incorrect offset, set both fields to 0. "
                f"The lifetime counter will be adjusted automatically on next restart. "
                f"⚠️ Reducing the offset will lower the displayed lifetime total permanently.\n\n"
            )
        else:
            offset_info = ""

        prefill_p1_str = str(self._prefill_p1) if self._prefill_p1 else ""
        prefill_p2_str = str(self._prefill_p2) if self._prefill_p2 else ""

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
                        default=prefill_p1_str,
                    ): str,
                    vol.Optional(
                        CONF_LIFETIME_OFFSET_P2,
                        default=prefill_p2_str,
                    ): str,
                    vol.Optional(CONF_BATTERY_SYSTEM, default=current_battery): bool,
                    vol.Optional(CONF_DETAIL_POLL, default=current_detail_poll): bool,
                    vol.Optional(CONF_SLOW_DETAIL_POLL, default=current_slow_detail): bool,
                }
            ),
            errors=errors,
            description_placeholders={
                "offset_info": offset_info,
                "min_interval": str(MIN_POLLING_INTERVAL),
                "max_interval": str(MAX_POLLING_INTERVAL),
            },
        )
