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
        self._coordinator = data.coordinator

        # Use the user-defined device name from the config entry if available.
        self._device_name = (
            data.coordinator.config_entry.data.get(CONF_DEVICE_NAME)
            or DEFAULT_DEVICE_NAME
        )

        self._update_device_info()

    def _update_device_info(self) -> None:
        """Update device info – called on init and when coordinator data changes.

        sw_version and model_id (IP) are read fresh each time so they update
        in the UI as soon as the inverter comes online after a cold start.
        """
        ip = self._coordinator.device_ip
        ip_display = f"IP: {ip}" if ip and ip != "unknown" else None
        version = self._coordinator.device_version

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=self._device_name,
            manufacturer="APsystems",
            model="EZ1-M",
            sw_version=version if version != "unknown" else None,
            serial_number=self._device_id,
            model_id=ip_display,
        )
