"""The switch entities for APsystems local API integration."""

from __future__ import annotations

import asyncio
from typing import Any

from APsystemsEZ1 import APsystemsEZ1M

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import LOGGER
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

    def _inverter_operable(self) -> bool:
        """Return True if the inverter is reachable and not in standby/off state.

        Both conditions must hold before sending a power command:
        - inverter_reachable: the API is responding (False after 3 failed polls)
        - alarm_info.operating: the inverter is not in standby/off state

        When the EZ1 is switched off via this toggle its API still responds,
        but operating = False. When it has crashed or lost power it becomes
        unreachable. Blocking commands in both cases prevents sending orders
        into the void and gives the user clear feedback.
        """
        if not self.coordinator.inverter_reachable:
            return False
        if self.coordinator.data is not None:
            return self.coordinator.data.alarm_info.operating
        return False

    async def _wait_for_poll(self) -> bool:
        """Wait for any active poll to finish. Returns False on timeout."""
        waited = 0
        while self.coordinator._poll_active:
            await asyncio.sleep(0.5)
            waited += 1
            if waited > 20:  # 10 seconds max
                LOGGER.warning("Timed out waiting for poll to finish.")
                return False
        return True

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the inverter on."""
        if not self._inverter_operable():
            raise HomeAssistantError(
                "Command not sent: inverter is unreachable or not operating."
            )
        if not await self._wait_for_poll():
            return
        try:
            self.coordinator._poll_active = True
            await self._api.set_device_power_status(1)
            self._is_on = True
            self.async_write_ha_state()
        finally:
            self.coordinator._poll_active = False

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the inverter off."""
        if not self._inverter_operable():
            raise HomeAssistantError(
                "Command not sent: inverter is unreachable or not operating."
            )
        if not await self._wait_for_poll():
            return
        try:
            self.coordinator._poll_active = True
            await self._api.set_device_power_status(0)
            self._is_on = False
            self.async_write_ha_state()
        finally:
            self.coordinator._poll_active = False
