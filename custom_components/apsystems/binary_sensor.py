"""The read-only binary sensors for APsystems local API integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from APsystemsEZ1 import ReturnAlarmInfo

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import ApSystemsConfigEntry, ApSystemsData, ApSystemsDataCoordinator
from .entity import ApSystemsEntity


@dataclass(frozen=True, kw_only=True)
class ApsystemsLocalApiBinarySensorDescription(BinarySensorEntityDescription):
    """Describes APsystems Inverter binary sensor entity."""

    is_on: Callable[[ReturnAlarmInfo], bool | None]


BINARY_SENSORS: tuple[ApsystemsLocalApiBinarySensorDescription, ...] = (
    ApsystemsLocalApiBinarySensorDescription(
        key="off_grid_status",
        translation_key="off_grid_status",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on=lambda c: c.offgrid,
    ),
    ApsystemsLocalApiBinarySensorDescription(
        key="dc_1_short_circuit_error_status",
        translation_key="dc_1_short_circuit_error_status",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on=lambda c: c.shortcircuit_1,
    ),
    ApsystemsLocalApiBinarySensorDescription(
        key="dc_2_short_circuit_error_status",
        translation_key="dc_2_short_circuit_error_status",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on=lambda c: c.shortcircuit_2,
    ),
    # FIX: the original used `not c.operating` with device_class=PROBLEM.
    # This caused the sensor to show "Problem detected" every night when the
    # inverter legitimately shuts down at dusk – not a fault condition.
    # Renamed to "inverter_active" with device_class=RUNNING so the semantics
    # are correct: ON = running normally, OFF = standby/night mode.
    ApsystemsLocalApiBinarySensorDescription(
        key="inverter_active",
        translation_key="inverter_active",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on=lambda c: c.operating,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ApSystemsConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the binary sensor platform."""
    config = config_entry.runtime_data

    add_entities(
        ApSystemsBinarySensorWithDescription(
            data=config,
            entity_description=desc,
        )
        for desc in BINARY_SENSORS
    )


class ApSystemsBinarySensorWithDescription(
    CoordinatorEntity[ApSystemsDataCoordinator], ApSystemsEntity, BinarySensorEntity
):
    """Base binary sensor to be used with description."""

    entity_description: ApsystemsLocalApiBinarySensorDescription

    def __init__(
        self,
        data: ApSystemsData,
        entity_description: ApsystemsLocalApiBinarySensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(data.coordinator)
        ApSystemsEntity.__init__(self, data)
        self.entity_description = entity_description
        self._attr_unique_id = f"{data.device_id}_{entity_description.key}"

    @property
    def is_on(self) -> bool | None:
        """Return value of sensor.

        For the inverter_active sensor: always return False when the inverter
        is unreachable, regardless of cached data. The inverter physically shuts
        down at night and is no longer reachable – showing "In Betrieb" from
        cached data would be misleading.

        For all other binary sensors (alarm/fault states): use cached data as
        usual so they remain stable while the inverter is offline.
        """
        if self.coordinator.data is None:
            return None
        if (
            self.entity_description.key == "inverter_active"
            and not self.coordinator.inverter_reachable
        ):
            return False
        return self.entity_description.is_on(self.coordinator.data.alarm_info)
