"""The number entities for APsystems local API integration."""

from __future__ import annotations

import asyncio

from APsystemsEZ1 import APsystemsEZ1M

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import LOGGER
from .coordinator import ApSystemsConfigEntry, ApSystemsData, ApSystemsDataCoordinator
from .entity import ApSystemsEntity

# Hardware limits used as safe fallbacks when the inverter is offline during setup.
# EZ1-M: 30–800W. EZ1-D: 30–1800W.
# The inverter reports its own limits via get_device_info() – these fallbacks
# only apply when that call fails (e.g. inverter unreachable at HA startup).
HARDWARE_MIN_POWER = 30
HARDWARE_MAX_POWER = 1800  # Upper bound covers both EZ1-M (800W) and EZ1-D (1800W)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ApSystemsConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the number platform."""
    config = config_entry.runtime_data
    add_entities([ApSystemsMaxPowerNumber(data=config)])


class ApSystemsMaxPowerNumber(
    CoordinatorEntity[ApSystemsDataCoordinator], ApSystemsEntity, NumberEntity
):
    """Entity to set the maximum power output."""

    _attr_has_entity_name = True
    _attr_translation_key = "max_output"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1.0  # Whole watts only – avoids "30.0" display in HA UI

    def __init__(self, data: ApSystemsData) -> None:
        """Initialize the number entity."""
        super().__init__(data.coordinator)
        ApSystemsEntity.__init__(self, data)
        self._attr_unique_id = f"{data.device_id}_max_output"
        self._api: APsystemsEZ1M = data.coordinator.api

    @property
    def native_min_value(self) -> float:
        """Return minimum power limit reported by the inverter (fallback: 30W)."""
        return float(int(self._api.min_power or HARDWARE_MIN_POWER))

    @property
    def native_max_value(self) -> float:
        """Return maximum power limit reported by the inverter.

        The inverter reports its hardware maximum via get_device_info():
        - EZ1-M: 800W
        - EZ1-D: 1800W
        Falls back to 1800W if the inverter was unreachable at startup so that
        EZ1-D users are not incorrectly blocked at 800W. The actual hardware
        limit will enforce the correct ceiling once the inverter is online.
        """
        return float(int(self._api.max_power or HARDWARE_MAX_POWER))

    @property
    def native_value(self) -> float | None:
        """Return the current power limit from coordinator."""
        return self.coordinator.current_max_power

    @property
    def available(self) -> bool:
        """Return False when the inverter is offline or not operating.

        Prevents setting a power limit when the EZ1 cannot act on it.
        The last known limit remains stored and is restored on reconnect.
        """
        if not self.coordinator.inverter_reachable:
            return False
        if self.coordinator.data is not None:
            return self.coordinator.data.alarm_info.operating
        return False

    async def async_set_native_value(self, value: float) -> None:
        """Set a new power limit via setMaxPower (RAM only).

        Only setMaxPower is used here – flash is never written by user actions.
        Flash was reset to the hardware maximum once during setup
        (_reset_flash_to_hardware_max) and is never touched again, protecting
        flash longevity. HA stores the desired limit and restores it each
        morning via setMaxPower when the inverter reloads flash into RAM.
        """
        min_p = self.native_min_value
        max_p = self.native_max_value

        if not min_p <= value <= max_p:
            raise HomeAssistantError(
                f"Power limit {value}W is outside the allowed range "
                f"({min_p}W – {max_p}W) for this inverter."
            )

        # Wait for active poll to complete before sending command
        waited = 0
        while self.coordinator._poll_active:
            await asyncio.sleep(0.5)
            waited += 1
            if waited > 20:  # 10 seconds max
                LOGGER.warning("Timed out waiting for poll to finish – aborting set power limit.")
                return

        try:
            self.coordinator._poll_active = True
            await self._api.set_max_power(int(value))

            # On older firmware (no getDefaultMaxPower endpoint), setMaxPower
            # writes directly to flash. Warn once per day so the user is aware
            # of potential flash wear from frequent changes.
            # On newer firmware default_max_power is set → RAM-only → no warning.
            if self.coordinator.default_max_power is None:
                from datetime import date as _date
                today = _date.today()
                self.coordinator.flash_write_count += 1
                if self.coordinator._last_flash_warning_date != today:
                    self.coordinator._last_flash_warning_date = today
                    LOGGER.warning(
                        "Power limit set to %sW. This inverter firmware does not support "
                        "the getDefaultMaxPower endpoint – setMaxPower writes directly to "
                        "flash memory. Frequent changes may cause flash wear over time. "
                        "Consider updating to firmware 1.9.x or later. "
                        "(This warning appears at most once per day.)",
                        int(value),
                    )
        except ValueError as err:
            LOGGER.error("Failed to set power limit to %sW: %s", value, err)
            raise HomeAssistantError(
                f"Inverter rejected power limit of {value}W: {err}"
            ) from err
        finally:
            self.coordinator._poll_active = False

        self.coordinator.current_max_power = value
        self.async_write_ha_state()
        LOGGER.info("Power limit set to %sW (RAM).", int(value))


