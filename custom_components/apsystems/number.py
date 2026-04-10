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
from .coordinator import ApSystemsConfigEntry, ApSystemsData, ApSystemsDataCoordinator, _fmt_err
from .entity import ApSystemsEntity

# Hardware limits as defined by APsystems for the EZ1-M.
# These are used as safe fallbacks if the inverter does not report its own limits.
HARDWARE_MIN_POWER = 30
HARDWARE_MAX_POWER = 800


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
        """Return minimum power limit.

        Uses the value reported by the inverter via get_device_info(),
        with a safe fallback to the EZ1-M hardware minimum of 30W.
        """
        return float(int(self._api.min_power or HARDWARE_MIN_POWER))

    @property
    def native_max_value(self) -> float:
        """Return maximum power limit.

        Uses the value reported by the inverter via get_device_info(),
        with a safe fallback to the EZ1-M hardware maximum of 800W.
        Note: newer models like the EZ1-D support up to 1800W – the inverter
        will report the correct value for its model via get_device_info().
        """
        return float(int(self._api.max_power or HARDWARE_MAX_POWER))

    @property
    def native_value(self) -> float | None:
        """Return the current power limit from coordinator."""
        return self.coordinator.current_max_power

    async def async_set_native_value(self, value: float) -> None:
        """Set a new power limit.

        Waits for any active poll to finish before sending the command,
        preventing concurrent API calls on the same inverter connection.
        Validates against the inverter's reported hardware limits before
        sending, and catches ValueError from the library as a safety net.
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

            # Fix: if getDefaultMaxPower is available and the new value exceeds
            # the flash limit, the inverter silently caps output at the flash value.
            # Keep both in sync by calling setDefaultMaxPower as well.
            default_mp = self.coordinator.default_max_power
            if default_mp is not None and int(value) > default_mp:
                ok, reason = await self.coordinator._try_set_default_max_power(int(value))
                if ok:
                    LOGGER.info(
                        "Power limit set to %sW – flash limit also updated "
                        "(was %sW, now %sW).",
                        int(value), default_mp, int(value),
                    )
                else:
                    LOGGER.info(
                        "Power limit set to %sW in RAM. Flash-Grenze konnte nicht "
                        "aktualisiert werden (aktuell %sW): %s",
                        int(value), default_mp, reason,
                    )
            else:
                LOGGER.info("Power limit set to %sW.", int(value))

        except ValueError as err:
            LOGGER.error("Failed to set power limit to %sW: %s", value, err)
            raise HomeAssistantError(
                f"Inverter rejected power limit of {value}W: {err}"
            ) from err
        finally:
            self.coordinator._poll_active = False

        self.coordinator.current_max_power = value
        self.async_write_ha_state()
        LOGGER.info("Power limit set to %sW", value)
