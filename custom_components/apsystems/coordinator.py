"""The coordinator for APsystems local API integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from APsystemsEZ1 import (
    APsystemsEZ1M,
    InverterReturnedError,
    ReturnAlarmInfo,
    ReturnOutputData,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, LOGGER, POLLING_INTERVAL

STORE_VERSION = 1
STORE_KEY = "apsystems_lifetime_offset"


def _fmt_err(err: Exception) -> str:
    """Format an exception as TypeName: message, or just TypeName if no message.

    Python built-in exceptions like TimeoutError have no message string,
    which would result in a trailing colon in log output.
    """
    name = type(err).__name__
    msg = str(err).strip()
    return f"{name}: {msg}" if msg else name


@dataclass
class ApSystemsSensorData:
    """Representing different APsystems sensor data."""

    output_data: ReturnOutputData
    alarm_info: ReturnAlarmInfo


@dataclass
class ApSystemsData:
    """Store runtime data."""

    coordinator: ApSystemsDataCoordinator
    device_id: str


type ApSystemsConfigEntry = ConfigEntry[ApSystemsData]


class ApSystemsDataCoordinator(DataUpdateCoordinator[ApSystemsSensorData]):
    """Coordinator used for all sensors."""

    config_entry: ApSystemsConfigEntry
    device_version: str
    battery_system: bool
    current_max_power: float | None
    _last_good_data: ApSystemsSensorData | None = None
    _consecutive_errors: int = 0
    inverter_reachable: bool = False  # False until first successful poll

    # Lifetime energy overflow compensation
    _te1_offset: float = 0.0
    _te2_offset: float = 0.0
    _te1_last_raw: float | None = None   # last raw inverter value (for reset detection)
    _te2_last_raw: float | None = None
    _te1_last_out: float | None = None   # last value sent to HA (for jitter suppression)
    _te2_last_out: float | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ApSystemsConfigEntry,
        api: APsystemsEZ1M,
    ) -> None:
        """Initialize coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name="APSystems Data",
            update_interval=timedelta(seconds=POLLING_INTERVAL),
        )
        self.api = api
        self.device_version = "unknown"
        self.battery_system = False
        self.current_max_power = None
        self._store: Store = Store(
            hass,
            STORE_VERSION,
            f"{STORE_KEY}_{config_entry.entry_id}",
        )

    async def _async_setup(self) -> None:
        """Set up coordinator.

        If the inverter is offline at startup (e.g. HA restarted at night),
        we continue with safe fallback values instead of raising UpdateFailed.
        This prevents the "Setup error" message in the UI and allows
        RestoreEntity to serve the last known sensor values immediately.
        The inverter will be fully initialised on the first successful poll.
        """
        await self._load_offsets()
        try:
            device_info = await self.api.get_device_info()
            self.api.max_power = getattr(device_info, "maxPower", 800)
            self.api.min_power = getattr(device_info, "minPower", 30)
            self.device_version = getattr(device_info, "devVer", "unknown")
            self.battery_system = getattr(device_info, "isBatterySystem", False)
            LOGGER.info(
                "APsystems inverter connected – firmware: %s, battery system: %s",
                self.device_version,
                self.battery_system,
            )
            await self._fetch_max_power()
        except Exception as err:  # noqa: BLE001
            LOGGER.warning(
                "APsystems inverter not reachable during setup – using fallback values. "
                "Will retry on next poll. Error: %s", _fmt_err(err)
            )
            self.api.max_power = 800
            self.api.min_power = 30

    async def _fetch_max_power(self) -> None:
        """Fetch the current power limit from the inverter."""
        try:
            result = await self.api.get_max_power()
            if result is not None:
                self.current_max_power = float(result)
                LOGGER.debug("Max power limit fetched: %sW", self.current_max_power)
            else:
                LOGGER.warning(
                    "APsystems inverter returned no value for max power limit. "
                    "The power limit entity may not be available."
                )
        except Exception as err:  # noqa: BLE001
            LOGGER.warning(
                "Could not fetch max power limit from inverter: %s. "
                "The power limit entity may not be available.", _fmt_err(err)
            )


    async def _load_offsets(self) -> None:
        """Load persisted lifetime energy offsets from storage.

        Offsets survive HA restarts so the compensated lifetime total
        remains correct even after the firmware overflow counter resets.
        """
        data = await self._store.async_load()
        if data:
            self._te1_offset = float(data.get("te1_offset", 0.0))
            self._te2_offset = float(data.get("te2_offset", 0.0))
            self._te1_last_raw = data.get("te1_last_raw")
            self._te2_last_raw = data.get("te2_last_raw")
            if self._te1_offset > 0 or self._te2_offset > 0:
                LOGGER.info(
                    "Restored lifetime energy offsets from storage – "
                    "P1: %.5f kWh, P2: %.5f kWh",
                    self._te1_offset, self._te2_offset,
                )

    async def _save_offsets(self) -> None:
        """Persist lifetime energy offsets to storage."""
        await self._store.async_save({
            "te1_offset": self._te1_offset,
            "te2_offset": self._te2_offset,
            "te1_last_raw": self._te1_last_raw,
            "te2_last_raw": self._te2_last_raw,
        })

    def _compensate_lifetime_energy(self, output_data: ReturnOutputData) -> tuple[ReturnOutputData, bool]:
        """Compensate for two known EZ1-M lifetime energy issues:

        1. OVERFLOW BUG: At ~540 kWh the firmware resets te1/te2 to 0.
           Detected when raw value drops > 1 kWh vs last raw value.
           Offset is accumulated so HA sees a continuously increasing total.

        2. ROUNDING JITTER: Inverter occasionally returns a marginally smaller
           value due to firmware floating point rounding (e.g. 176.58319 → 176.58315).
           Fixed by tracking the last value sent to HA and never going below it.
           This eliminates the HA "state is not strictly increasing" warning.
        """
        te1_raw = output_data.te1
        te2_raw = output_data.te2

        # 1. Detect and compensate overflow reset (raw drop > 1 kWh)
        needs_save = False
        if self._te1_last_raw is not None and te1_raw < (self._te1_last_raw - 1.0):
            self._te1_offset += self._te1_last_raw
            needs_save = True
            LOGGER.warning(
                "APsystems EZ1 lifetime energy counter reset detected on Input 1! "
                "Previous: %.5f kWh -> New: %.5f kWh. "
                "Accumulated offset: %.5f kWh. HA counter continues correctly.",
                self._te1_last_raw, te1_raw, self._te1_offset,
            )

        if self._te2_last_raw is not None and te2_raw < (self._te2_last_raw - 1.0):
            self._te2_offset += self._te2_last_raw
            needs_save = True
            LOGGER.warning(
                "APsystems EZ1 lifetime energy counter reset detected on Input 2! "
                "Previous: %.5f kWh -> New: %.5f kWh. "
                "Accumulated offset: %.5f kWh. HA counter continues correctly.",
                self._te2_last_raw, te2_raw, self._te2_offset,
            )

        # Store raw values for next reset detection
        self._te1_last_raw = te1_raw
        self._te2_last_raw = te2_raw

        # Apply overflow offset to get compensated value
        te1 = te1_raw + self._te1_offset
        te2 = te2_raw + self._te2_offset

        # 2. Suppress rounding jitter – never send a value lower than last output.
        # We compare against the last value actually sent to HA (after offset),
        # not the raw inverter value.
        if self._te1_last_out is not None:
            te1 = max(te1, self._te1_last_out)
        if self._te2_last_out is not None:
            te2 = max(te2, self._te2_last_out)

        # Store the value we are about to send to HA for next jitter check
        self._te1_last_out = te1
        self._te2_last_out = te2

        output_data.te1 = te1
        output_data.te2 = te2

        return output_data, needs_save

    async def _async_update_data(self) -> ApSystemsSensorData:
        """Fetch data from inverter."""
        try:
            return await self._do_fetch()

        except InverterReturnedError:
            self._consecutive_errors += 1
            if self._last_good_data is not None:
                if self._consecutive_errors == 1:
                    LOGGER.warning(
                        "APsystems inverter returned an error – "
                        "serving cached data (likely entering night/standby mode)."
                    )
                elif self._consecutive_errors == 10:
                    LOGGER.warning(
                        "APsystems inverter still returning errors after %d polls (%ds). "
                        "If this is not nightly standby, check the inverter.",
                        self._consecutive_errors,
                        self._consecutive_errors * POLLING_INTERVAL,
                    )
                else:
                    LOGGER.debug(
                        "Inverter error (consecutive: %d) – serving cached data.",
                        self._consecutive_errors,
                    )
                self.inverter_reachable = False
                return self._last_good_data
            # No cache yet (first poll after HA restart while inverter offline).
            # Return None so RestoreEntity can serve last known values.
            # HA will retry automatically – no UpdateFailed needed.
            LOGGER.warning(
                "APsystems inverter returned an error and no cached data is available. "
                "Sensors will show last known values via RestoreEntity."
            )
            self.inverter_reachable = False
            return None  # type: ignore[return-value]

        except Exception as err:  # noqa: BLE001
            self._consecutive_errors += 1
            if self._last_good_data is not None:
                if self._consecutive_errors == 1:
                    LOGGER.warning(
                        "APsystems inverter unreachable – "
                        "serving cached data. Error: %s", _fmt_err(err),
                    )
                elif self._consecutive_errors == 10:
                    LOGGER.warning(
                        "APsystems inverter still unreachable after %d polls (%ds). "
                        "Check network connection. Error: %s",
                        self._consecutive_errors,
                        self._consecutive_errors * POLLING_INTERVAL,
                        _fmt_err(err),
                    )
                else:
                    LOGGER.debug(
                        "Inverter unreachable (consecutive: %d) – serving cached data.",
                        self._consecutive_errors,
                    )
                self.inverter_reachable = False
                return self._last_good_data
            # No cache yet – return None so RestoreEntity serves last known values.
            LOGGER.warning(
                "APsystems inverter unreachable and no cached data available. "
                "Sensors will show last known values via RestoreEntity. "
                "Error: %s", _fmt_err(err)
            )
            self.inverter_reachable = False
            return None  # type: ignore[return-value]

    async def _do_fetch(self) -> ApSystemsSensorData:
        """Perform the actual API calls and return sensor data."""
        output_data = await self.api.get_output_data()
        alarm_info = await self.api.get_alarm_info()

        # If max power was not available during setup (inverter not fully ready),
        # retry once on the next successful poll – but never on every poll.
        # Polling getMaxPower on every cycle causes sporadic TimeoutError on
        # firmware 1.12.2 even when the value is already known.
        if self.current_max_power is None:
            await self._fetch_max_power()

        if self._consecutive_errors > 0:
            LOGGER.info(
                "APsystems inverter back online after %d consecutive errors.",
                self._consecutive_errors,
            )
            self._consecutive_errors = 0

        self.inverter_reachable = True

        output_data, needs_save = self._compensate_lifetime_energy(output_data)
        if needs_save:
            await self._save_offsets()

        result = ApSystemsSensorData(output_data=output_data, alarm_info=alarm_info)
        self._last_good_data = result
        return result
