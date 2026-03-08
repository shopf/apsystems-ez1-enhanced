"""Constants for the APsystems local API integration."""

import logging

DOMAIN = "apsystems"
DEFAULT_PORT = 8050
DEFAULT_DEVICE_NAME = "APsystems EZ1"
CONF_DEVICE_NAME = "device_name"
LOGGER = logging.getLogger(__name__)

# Polling interval in seconds.
POLLING_INTERVAL = 12
