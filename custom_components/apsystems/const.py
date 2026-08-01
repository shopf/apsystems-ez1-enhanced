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

# Internal config-entry keys used to pass the delta-calculation reference from
# the reconfigure flow to the coordinator without writing to storage prematurely.
# Cleared from the config entry after the coordinator has applied the delta.
CONF_SHOWN_OFFSET_P1 = "shown_offset_p1"
CONF_SHOWN_OFFSET_P2 = "shown_offset_p2"

# Storage key and version – defined here so config_flow can load the store
# without importing from coordinator (which would create a circular dependency).
STORE_KEY = "apsystems_lifetime_offset"
STORE_VERSION = 1

# Optional: user declares that the inverter is connected to a battery system.
# Enables more frequent power limit verification after morning restart
# (every 5 min × 10 rounds instead of every 10 min × 5 rounds).
CONF_BATTERY_SYSTEM = "battery_system"
CONF_DETAIL_POLL = "detail_poll"       # False → skip getOutputDataDetail entirely
CONF_SLOW_DETAIL_POLL = "slow_detail_poll"

# Model detection based on the hardware maximum output power (VA) reported
# by getDeviceInfo() (field "maxPower").
#
# If a device reports a maxPower value that is not in this table, the
# model cannot be determined. In that case a one-time warning is logged
# (see ApSystemsDataCoordinator._check_known_model) asking the user to
# open an issue with the reported value, so the table can be extended.
MODEL_BY_MAX_POWER: dict[int, str] = {
    600: "EZ1-M",
    800: "EZ1-M",
    900: "EZ1-LV",
    960: "EZ1-H",
    1600: "EZ1D-L",
    1800: "EZ1D",
    2000: "EZ1D-H",
}
UNKNOWN_MODEL_NAME = "EZ1 (unknown model)"
