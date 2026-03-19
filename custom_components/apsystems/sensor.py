"""The read-only sensors for APsystems local API integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from APsystemsEZ1 import ReturnOutputData

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import DiscoveryInfoType, StateType
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import ApSystemsConfigEntry, ApSystemsData, ApSystemsDataCoordinator
from .entity import ApSystemsEntity


@dataclass(frozen=True, kw_only=True)
class ApsystemsLocalApiSensorDescription(SensorEntityDescription):
    """Describes APsystems Inverter sensor entity."""

    value_fn: Callable[[ReturnOutputData], float | None]


SENSORS: tuple[ApsystemsLocalApiSensorDescription, ...] = (
    # ── Combined output ───────────────────────────────────────────────────────
    ApsystemsLocalApiSensorDescription(
        key="total_power",
        translation_key="total_power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.p1 + c.p2,
    ),
    ApsystemsLocalApiSensorDescription(
        key="today_production",
        translation_key="today_production",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda c: c.e1 + c.e2,
    ),
    ApsystemsLocalApiSensorDescription(
        key="lifetime_production",
        translation_key="lifetime_production",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda c: c.te1 + c.te2,
    ),
    # ── PV Input 1 ────────────────────────────────────────────────────────────
    ApsystemsLocalApiSensorDescription(
        key="total_power_p1",
        translation_key="total_power_p1",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.p1,
    ),
    ApsystemsLocalApiSensorDescription(
        key="today_production_p1",
        translation_key="today_production_p1",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda c: c.e1,
    ),
    ApsystemsLocalApiSensorDescription(
        key="lifetime_production_p1",
        translation_key="lifetime_production_p1",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda c: c.te1,
    ),
    # ── PV Input 2 ────────────────────────────────────────────────────────────
    ApsystemsLocalApiSensorDescription(
        key="total_power_p2",
        translation_key="total_power_p2",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.p2,
    ),
    ApsystemsLocalApiSensorDescription(
        key="today_production_p2",
        translation_key="today_production_p2",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda c: c.e2,
    ),
    ApsystemsLocalApiSensorDescription(
        key="lifetime_production_p2",
        translation_key="lifetime_production_p2",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda c: c.te2,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ApSystemsConfigEntry,
    add_entities: AddConfigEntryEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the sensor platform."""
    config = config_entry.runtime_data

    entities: list[SensorEntity] = [
        ApSystemsSensorWithDescription(data=config, entity_description=desc)
        for desc in SENSORS
    ]

    # FIX: expose firmware version as a diagnostic sensor so users can
    # immediately see which firmware they are running and correlate it with
    # known compatibility issues – was silently stored but never surfaced.
    entities.append(ApSystemsFirmwareSensor(data=config))

    add_entities(entities)


class ApSystemsSensorWithDescription(
    CoordinatorEntity[ApSystemsDataCoordinator], ApSystemsEntity, SensorEntity, RestoreEntity
):
    """Base sensor to be used with description."""

    entity_description: ApsystemsLocalApiSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        data: ApSystemsData,
        entity_description: ApsystemsLocalApiSensorDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(data.coordinator)
        ApSystemsEntity.__init__(self, data)
        self.entity_description = entity_description
        self._attr_unique_id = f"{data.device_id}_{entity_description.key}"

    async def async_added_to_hass(self) -> None:
        """Restore last known state on HA startup if coordinator has no data yet.

        This prevents sensors from showing as unavailable immediately after a
        HA restart when the inverter is still offline (e.g. at night).
        HA automatically persists the last known state in its own database –
        no custom storage needed.
        """
        await super().async_added_to_hass()
        if self.coordinator.data is None:
            if last_state := await self.async_get_last_state():
                if last_state.state not in ("unknown", "unavailable"):
                    try:
                        self._attr_native_value = float(last_state.state)
                    except ValueError:
                        pass

    @property
    def native_value(self) -> StateType:
        """Return value of sensor.

        Returns None (shown as unavailable) if no data has been received yet,
        e.g. when the inverter was offline during the initial HA startup.
        Once the coordinator has data (either fresh or cached), the value is
        returned and the entity stays available even while the inverter is off.
        """
        if self.coordinator.data is None:
            return self._attr_native_value
        return self.entity_description.value_fn(self.coordinator.data.output_data)


class ApSystemsFirmwareSensor(
    CoordinatorEntity[ApSystemsDataCoordinator], ApSystemsEntity, SensorEntity
):
    """Diagnostic sensor that exposes the inverter firmware version.

    FIX: device_version was read in coordinator._async_setup() and stored on
    the coordinator but never exposed anywhere in the UI.  Given the number of
    firmware-related breakages this is genuinely useful diagnostic information.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:chip"

    def __init__(self, data: ApSystemsData) -> None:
        """Initialize firmware sensor."""
        super().__init__(data.coordinator)
        ApSystemsEntity.__init__(self, data)
        self._attr_unique_id = f"{data.device_id}_firmware_version"
        self._attr_name = "Firmware Version"

    @property
    def native_value(self) -> str:
        """Return firmware version string."""
        return self.coordinator.device_version
