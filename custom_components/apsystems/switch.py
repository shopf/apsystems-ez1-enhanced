"""The switch entities for APsystems local API integration."""

from __future__ import annotations

from typing import Any

from APsystemsEZ1 import APsystemsEZ1M

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import ApSystemsConfigEntry, ApSystemsData, ApSystemsDataCoordinator
from .entity import ApSystemsEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ApSystemsConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switch platform."""
    config = config_entry.runtime_data
    add_entities([ApSystemsInverterSwitch(data=config)])


class ApSystemsInverterSwitch(
    CoordinatorEntity[ApSystemsDataCoordinator], ApSystemsEntity, SwitchEntity
):
    """Switch to turn the inverter on or off."""

    _attr_has_entity_name = True
    _attr_translation_key = "inverter_status"

    def __init__(self, data: ApSystemsData) -> None:
        """Initialize the switch."""
        super().__init__(data.coordinator)
        ApSystemsEntity.__init__(self, data)
        self._attr_unique_id = f"{data.device_id}_inverter_status"
        self._api: APsystemsEZ1M = data.coordinator.api
        self._is_on: bool = True

    @property
    def is_on(self) -> bool:
        """Return true if inverter is on."""
        return self._is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the inverter on."""
        await self._api.set_device_power_status(1)
        self._is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the inverter off."""
        await self._api.set_device_power_status(0)
        self._is_on = False
        self.async_write_ha_state()
