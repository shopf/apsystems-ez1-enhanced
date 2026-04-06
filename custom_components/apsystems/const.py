"""Constants for the APsystems local API integration."""

import logging

DOMAIN = "apsystems"
DEFAULT_PORT = 8050
DEFAULT_DEVICE_NAME = "APsystems EZ1"
CONF_DEVICE_NAME = "device_name"
LOGGER = logging.getLogger(__name__)

# Polling interval in seconds – default and allowed range.
POLLING_INTERVAL = 12
CONF_POLLING_INTERVAL = "polling_interval"
MIN_POLLING_INTERVAL = 12   # APsystems local API minimum recommended interval
MAX_POLLING_INTERVAL = 60
