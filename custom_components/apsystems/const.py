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

# Optional lifetime energy offset entered by the user during setup.
# Allows correcting the lifetime total after a firmware overflow reset
# (the inverter resets its internal counter to 0 at ~540 kWh).
# Stored as config entry data and applied once to _te1_offset / _te2_offset.
CONF_LIFETIME_OFFSET_P1 = "lifetime_offset_p1"
CONF_LIFETIME_OFFSET_P2 = "lifetime_offset_p2"
