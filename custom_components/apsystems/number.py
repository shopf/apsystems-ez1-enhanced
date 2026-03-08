"""The number entities for APsystems local API integration."""

from __future__ import annotations

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

        try:
            await self._api.set_max_power(int(value))
        except ValueError as err:
            # The library performs its own range check – surface it clearly.
            LOGGER.error("Failed to set power limit to %sW: %s", value, err)
            raise HomeAssistantError(
                f"Inverter rejected power limit of {value}W: {err}"
            ) from err

        self.coordinator.current_max_power = value
        self.async_write_ha_state()
        LOGGER.info("Power limit set to %sW", value)
