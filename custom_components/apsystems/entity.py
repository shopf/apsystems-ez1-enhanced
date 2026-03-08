"""Base entity for APsystems integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONF_DEVICE_NAME, DEFAULT_DEVICE_NAME, DOMAIN
from .coordinator import ApSystemsData


class ApSystemsEntity:
    """Defines a base APsystems entity."""

    _attr_has_entity_name = True

    def __init__(self, data: ApSystemsData) -> None:
        """Initialize the entity."""
        self._device_id = data.device_id

        # Use the user-defined device name from the config entry if available.
        device_name = (
            data.coordinator.config_entry.data.get(CONF_DEVICE_NAME)
            or DEFAULT_DEVICE_NAME
        )

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, data.device_id)},
            name=device_name,
            manufacturer="APsystems",
            model="EZ1-M",
            sw_version=data.coordinator.device_version,
            serial_number=data.device_id,
        )
