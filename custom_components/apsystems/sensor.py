"""The read-only sensors for APsystems local API integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from APsystemsEZ1 import ReturnOutputData

from homeassistant.const import UnitOfElectricCurrent, UnitOfElectricPotential, UnitOfFrequency, UnitOfTemperature

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
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import ApSystemsConfigEntry, ApSystemsData, ApSystemsDataCoordinator, ReturnOutputDataDetail
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


@dataclass(frozen=True, kw_only=True)
class ApsystemsDetailSensorDescription(SensorEntityDescription):
    """Describes APsystems detail sensor entity (from getOutputDataDetail)."""

    value_fn: Callable[[ReturnOutputDataDetail], float | None]


DETAIL_SENSORS: tuple[ApsystemsDetailSensorDescription, ...] = (
    # ── PV Input voltages ─────────────────────────────────────────────────────
    ApsystemsDetailSensorDescription(
        key="voltage_p1",
        translation_key="voltage_p1",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.v1,
    ),
    ApsystemsDetailSensorDescription(
        key="voltage_p2",
        translation_key="voltage_p2",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.v2,
    ),
    # ── PV Input currents ─────────────────────────────────────────────────────
    ApsystemsDetailSensorDescription(
        key="current_p1",
        translation_key="current_p1",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.c1,
    ),
    ApsystemsDetailSensorDescription(
        key="current_p2",
        translation_key="current_p2",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.c2,
    ),
    # ── Grid ──────────────────────────────────────────────────────────────────
    ApsystemsDetailSensorDescription(
        key="grid_voltage",
        translation_key="grid_voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: d.gv,
    ),
    ApsystemsDetailSensorDescription(
        key="grid_frequency",
        translation_key="grid_frequency",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.gf,
    ),
    # ── Temperature ───────────────────────────────────────────────────────────
    ApsystemsDetailSensorDescription(
        key="inverter_temperature",
        translation_key="inverter_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: d.t,
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

    # Detail sensors (getOutputDataDetail) – added always, show unavailable
    # on older firmware that doesn't support the endpoint
    for desc in DETAIL_SENSORS:
        entities.append(ApSystemsDetailSensorEntity(data=config, entity_description=desc))

    add_entities(entities)


class ApSystemsSensorWithDescription(
    CoordinatorEntity[ApSystemsDataCoordinator], ApSystemsEntity, SensorEntity
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

    def _handle_coordinator_update(self) -> None:
        """Update device info when coordinator data changes.

        This ensures sw_version and IP in device info are refreshed as soon
        as the inverter comes online after a cold start.
        """
        self._update_device_info()
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> StateType:
        """Return value of sensor.

        The coordinator always provides valid data via _fallback_data –
        sensors are never unavailable, even after a HA restart at night.
        """
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data.output_data)


class ApSystemsDetailSensorEntity(
    CoordinatorEntity[ApSystemsDataCoordinator], ApSystemsEntity, SensorEntity
):
    """Sensor for data from /getOutputDataDetail endpoint.

    Shows as unavailable on firmware that does not support the endpoint.
    """

    entity_description: ApsystemsDetailSensorDescription
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        data: ApSystemsData,
        entity_description: ApsystemsDetailSensorDescription,
    ) -> None:
        """Initialize the detail sensor."""
        super().__init__(data.coordinator)
        ApSystemsEntity.__init__(self, data)
        self.entity_description = entity_description
        self._attr_unique_id = f"{data.device_id}_{entity_description.key}"

    @property
    def native_value(self) -> StateType:
        """Return value of sensor.

        When the inverter is offline, detail_data falls back to a zero-filled
        object (v, c, gv, gf = 0) with the last known temperature preserved.
        Returns None only before the first successful poll or when firmware
        does not support the endpoint.
        """
        if self.coordinator.data is None:
            return None
        detail = self.coordinator.data.detail_data
        if detail is None:
            return None
        return self.entity_description.value_fn(detail)

    @property
    def available(self) -> bool:
        """Return False if firmware does not support getOutputDataDetail."""
        if self.coordinator._detail_supported is False:
            return False
        return super().available


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
