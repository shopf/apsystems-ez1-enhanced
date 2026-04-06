"""The coordinator for APsystems local API integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta

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

from .const import CONF_POLLING_INTERVAL, DOMAIN, LOGGER, POLLING_INTERVAL

STORE_VERSION = 1
STORE_KEY = "apsystems_lifetime_offset"

# Minimum today-energy value above which a sudden drop to 0.0 is treated as a
# firmware bug (EZ1 firmware 1.12.2 resets e1/e2 to 0 ~11 min before shutdown).
# Below this threshold the reset is treated as a legitimate midnight reset.
_TODAY_RESET_THRESHOLD = 0.01  # kWh – values below this are treated as "near zero"
# Minimum production seen today before a near-zero reading is treated as a firmware bug.
# Below this, the inverter may legitimately be starting up on a cloudy morning.
_SIGNIFICANT_PRODUCTION = 0.05  # kWh – 50 Wh

# Alarm info is read every Nth poll to reduce load on the inverter and
# avoid WLAN reconnects on firmware 1.12.2 which reconnects frequently.
# Output data is read on every poll.
_ALARM_POLL_INTERVAL = 10


def _fmt_err(err: Exception) -> str:
    """Format an exception as TypeName: message, or just TypeName if no message.

    Python built-in exceptions like TimeoutError have no message string,
    which would result in a trailing colon in log output.
    """
    name = type(err).__name__
    msg = str(err).strip()
    return f"{name}: {msg}" if msg else name


def _make_fallback_output() -> ReturnOutputData:
    """Return a safe all-zero output data object used before first successful poll."""
    return ReturnOutputData(p1=0, e1=0, te1=0, p2=0, e2=0, te2=0)


def _make_fallback_alarm() -> ReturnAlarmInfo:
    """Return a safe alarm info object used before first successful poll."""
    return ReturnAlarmInfo(
        offgrid=False,
        shortcircuit_1=False,
        shortcircuit_2=False,
        operating=True,
    )


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
    inverter_reachable: bool = False  # False until first successful poll

    # Lifetime energy overflow compensation
    _te1_offset: float = 0.0
    _te2_offset: float = 0.0
    _te1_last_raw: float | None = None   # last raw inverter value (for reset detection)
    _te2_last_raw: float | None = None
    _te1_last_out: float | None = None   # last value sent to HA (for jitter suppression)
    _te2_last_out: float | None = None

    # Today energy protection – firmware 1.12.2 bug: e1/e2 reset to 0.0 before shutdown
    _e1_protected: float = 0.0  # highest e1 seen today – never decreases within a day
    _e2_protected: float = 0.0  # highest e2 seen today – never decreases within a day
    _e1_reset_logged: bool = False  # prevents repeated WARNING for same reset event
    _e2_reset_logged: bool = False
    _protected_date: date | None = None  # date when _e1/e2_protected were last updated
    _stable_polls_after_error: int = 0  # counts successful polls after reconnect
    _device_info_retries: int = 0  # counts remaining retries for device info

    # Device IP address shown in device info
    device_ip: str = "unknown"

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
            update_interval=timedelta(seconds=config_entry.data.get(CONF_POLLING_INTERVAL, POLLING_INTERVAL)),
        )
        self.api = api
        self.device_version = "unknown"
        self.battery_system = False
        self.current_max_power = None

        # _fallback_data is always valid – sensors read from it when the inverter
        # is offline. Initialised with safe zero values, updated on every
        # successful poll. This replaces the old _last_good_data / RestoreEntity
        # approach and ensures sensors are never unavailable.
        self._fallback_data = ApSystemsSensorData(
            output_data=_make_fallback_output(),
            alarm_info=_make_fallback_alarm(),
        )

        # _poll_active prevents concurrent API calls from coordinator, number and
        # switch entities running simultaneously on the same inverter connection.
        self._poll_active: bool = False

        # Counter to reduce alarm polling frequency
        self._poll_count: int = 0

        self._consecutive_errors: int = 0
        self._store: Store = Store(
            hass,
            STORE_VERSION,
            f"{STORE_KEY}_{config_entry.entry_id}",
        )

    async def _async_setup(self) -> None:
        """Set up coordinator.

        If the inverter is offline at startup (e.g. HA restarted at night),
        we continue with safe fallback values instead of raising UpdateFailed.
        This prevents the 'Setup error' message in the UI – sensors immediately
        show zero values (from _fallback_data) and will update as soon as the
        inverter comes back online.
        """
        await self._load_offsets()
        try:
            device_info = await self.api.get_device_info()
            self.api.max_power = getattr(device_info, "maxPower", 800)
            self.api.min_power = getattr(device_info, "minPower", 30)
            self.device_version = getattr(device_info, "devVer", "unknown")
            self.battery_system = getattr(device_info, "isBatterySystem", False)
            self.device_ip = getattr(device_info, "ipAddr", "unknown")
            LOGGER.info(
                "APsystems inverter connected – firmware: %s, IP: %s, battery system: %s",
                self.device_version,
                self.device_ip,
                self.battery_system,
            )
            await self._fetch_max_power()
        except Exception as err:  # noqa: BLE001
            LOGGER.info(
                "APsystems inverter not reachable during setup – using fallback values. "
                "Will retry on next poll. Error: %s", _fmt_err(err)
            )
            # Use stored values as fallback so number/switch entities
            # show correct limits immediately even when inverter is offline
            self.api.max_power = int(self.current_max_power or 800)
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
            # Lifetime energy overflow compensation
            self._te1_offset = float(data.get("te1_offset", 0.0))
            self._te2_offset = float(data.get("te2_offset", 0.0))
            self._te1_last_raw = data.get("te1_last_raw")
            self._te2_last_raw = data.get("te2_last_raw")
            self._te1_last_out = data.get("te1_last_out")
            self._te2_last_out = data.get("te2_last_out")

            # Today energy protection
            self._e1_protected = float(data.get("e1_protected", 0.0))
            self._e2_protected = float(data.get("e2_protected", 0.0))
            pd = data.get("protected_date")
            self._protected_date = date.fromisoformat(pd) if pd else None

            # Power limit
            mp = data.get("current_max_power")
            if mp is not None:
                self.current_max_power = float(mp)

            # Device info
            self.device_version = data.get("device_version", "unknown")
            self.device_ip = data.get("device_ip", "unknown")

            # Restore fallback data so sensors show last known values immediately
            fb = self._fallback_data.output_data
            fb.p1 = 0.0  # power always 0 at startup – inverter may be off
            fb.p2 = 0.0
            fb.e1 = float(data.get("fb_e1", 0.0))
            fb.e2 = float(data.get("fb_e2", 0.0))
            fb.te1 = float(data.get("fb_te1", 0.0))
            fb.te2 = float(data.get("fb_te2", 0.0))

            LOGGER.info(
                "Restored state from storage – "
                "te1_out=%.5f kWh, te2_out=%.5f kWh, "
                "e1_protected=%.5f kWh, e2_protected=%.5f kWh, "
                "max_power=%s W, firmware=%s",
                self._te1_last_out or 0.0, self._te2_last_out or 0.0,
                self._e1_protected, self._e2_protected,
                self.current_max_power, self.device_version,
            )

    async def _save_state(self) -> None:
        """Persist all coordinator state to storage so it survives HA restarts."""
        fb = self._fallback_data.output_data
        await self._store.async_save({
            # Lifetime energy overflow compensation
            "te1_offset": self._te1_offset,
            "te2_offset": self._te2_offset,
            "te1_last_raw": self._te1_last_raw,
            "te2_last_raw": self._te2_last_raw,
            "te1_last_out": self._te1_last_out,
            "te2_last_out": self._te2_last_out,
            # Today energy protection
            "e1_protected": self._e1_protected,
            "e2_protected": self._e2_protected,
            "protected_date": self._protected_date.isoformat() if self._protected_date else None,
            # Power limit
            "current_max_power": self.current_max_power,
            # Last known sensor values (fallback data)
            "fb_p1": fb.p1,
            "fb_p2": fb.p2,
            "fb_e1": fb.e1,
            "fb_e2": fb.e2,
            "fb_te1": fb.te1,
            "fb_te2": fb.te2,
            # Device info
            "device_version": self.device_version,
            "device_ip": self.device_ip,
        })

    def _compensate_lifetime_energy(self, output_data: ReturnOutputData) -> tuple[ReturnOutputData, bool]:
        """Compensate for two known EZ1-M lifetime energy issues:

        1. OVERFLOW BUG: At ~540 kWh the firmware resets te1/te2 to 0.
           Detected when raw value drops > 1 kWh vs last raw value.
           Offset is accumulated so HA sees a continuously increasing total.

        2. ROUNDING JITTER: Inverter occasionally returns a marginally smaller
           value due to firmware floating point rounding (e.g. 176.58319 → 176.58315).
           Fixed by tracking the last value sent to HA and never going below it.
           This eliminates the HA 'state is not strictly increasing' warning.
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
        if self._te1_last_out is not None:
            te1 = max(te1, self._te1_last_out)
        if self._te2_last_out is not None:
            te2 = max(te2, self._te2_last_out)

        self._te1_last_out = te1
        self._te2_last_out = te2

        output_data.te1 = te1
        output_data.te2 = te2

        # 3. TODAY ENERGY PROTECTION (firmware bug on all known EZ1 versions)
        # The inverter resets e1/e2 to exactly 0 before or during shutdown –
        # sometimes minutes before going offline. This is NOT a midnight reset.
        #
        # Strategy:
        # - _e1_protected tracks the HIGHEST e1 seen today (never decreases intraday)
        # - On midnight (new calendar date): _e1_protected resets to 0
        # - If e1==0 and _e1_protected > threshold: firmware bug → hold protected value
        # - If e1==0 and new day: legitimate reset → accept 0, start fresh
        e1_raw = output_data.e1
        e2_raw = output_data.e2
        today = date.today()

        # Midnight reset is handled in _async_update_data – see _check_midnight_reset()

        # Track highest value seen today – never allow decrease within same day
        if e1_raw > self._e1_protected:
            self._e1_protected = e1_raw
            self._protected_date = today
            self._e1_reset_logged = False  # new higher value → reset logged flag
        if e2_raw > self._e2_protected:
            self._e2_protected = e2_raw
            self._protected_date = today
            self._e2_reset_logged = False

        # Detect firmware bug: e1/e2 near zero but significant production already seen today.
        # Uses _SIGNIFICANT_PRODUCTION to avoid false positives on cloudy mornings.
        if e1_raw < _TODAY_RESET_THRESHOLD and self._e1_protected > _SIGNIFICANT_PRODUCTION:
            if not self._e1_reset_logged:
                LOGGER.warning(
                    "APsystems EZ1 today energy (e1) reset to 0 while last known value "
                    "was %.5f kWh – firmware bug detected. Holding last value until midnight.",
                    self._e1_protected,
                )
                self._e1_reset_logged = True
            else:
                LOGGER.debug("e1 still 0 – holding protected value %.5f kWh.", self._e1_protected)
            output_data.e1 = self._e1_protected

        if e2_raw < _TODAY_RESET_THRESHOLD and self._e2_protected > _SIGNIFICANT_PRODUCTION:
            if not self._e2_reset_logged:
                LOGGER.warning(
                    "APsystems EZ1 today energy (e2) reset to 0 while last known value "
                    "was %.5f kWh – firmware bug detected. Holding last value until midnight.",
                    self._e2_protected,
                )
                self._e2_reset_logged = True
            else:
                LOGGER.debug("e2 still 0 – holding protected value %.5f kWh.", self._e2_protected)
            output_data.e2 = self._e2_protected

        return output_data, needs_save

    def _check_midnight_reset(self) -> None:
        """Reset today energy protection at midnight, regardless of inverter state.

        This runs on every poll – even when the inverter is offline – so the
        reset happens at midnight and not when the inverter comes back online
        the next morning (which would show yesterday's value until first poll).
        """
        today = date.today()
        if self._protected_date is not None and today != self._protected_date:
            LOGGER.info(
                "Today energy counters reset at midnight – P1: %.5f kWh, P2: %.5f kWh.",
                self._e1_protected, self._e2_protected,
            )
            self._e1_protected = 0.0
            self._e2_protected = 0.0
            self._e1_reset_logged = False
            self._e2_reset_logged = False
            self._protected_date = today
            # Also reset fallback data today energy values
            self._fallback_data.output_data.e1 = 0.0
            self._fallback_data.output_data.e2 = 0.0
            LOGGER.debug("Fallback data today energy reset to 0 at midnight.")

    async def _async_update_data(self) -> ApSystemsSensorData:
        """Fetch data from inverter, always returning valid data.

        On error, _fallback_data (last known good values) is returned so
        sensors never become unavailable. Power values are zeroed after
        several consecutive errors to reflect that the inverter is off.
        """
        # Midnight reset runs every poll regardless of inverter state
        self._check_midnight_reset()

        # Skip if another API call is already in progress
        if self._poll_active:
            LOGGER.debug("Poll already active – returning cached data.")
            return self._fallback_data


        try:
            self._poll_active = True
            return await self._do_fetch()

        except InverterReturnedError:
            self._consecutive_errors += 1
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
            # Zero power immediately on any error – prevents false statistics
            self._fallback_data.output_data.p1 = 0
            self._fallback_data.output_data.p2 = 0
            self._stable_polls_after_error = 0  # reset – must re-stabilize before restore
            self._power_limit_restored = False  # allow restore on next reconnect
            self.inverter_reachable = False
            return self._fallback_data

        except Exception as err:  # noqa: BLE001
            self._consecutive_errors += 1
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
                    self._consecutive_errors * self.update_interval.total_seconds(),
                    _fmt_err(err),
                )
            else:
                LOGGER.debug(
                    "Inverter unreachable (consecutive: %d) – serving cached data.",
                    self._consecutive_errors,
                )
            # Zero power immediately on any error – prevents false statistics
            self._fallback_data.output_data.p1 = 0
            self._fallback_data.output_data.p2 = 0
            self._stable_polls_after_error = 0  # reset – must re-stabilize before restore
            self._power_limit_restored = False  # allow restore on next reconnect
            self.inverter_reachable = False
            return self._fallback_data

        finally:
            self._poll_active = False

    async def _do_fetch(self) -> ApSystemsSensorData:
        """Perform the actual API calls and return sensor data."""
        output_data = await self.api.get_output_data()

        # Alarm info is expensive – only poll every Nth cycle
        self._poll_count += 1
        if self._poll_count % _ALARM_POLL_INTERVAL == 1:
            alarm_info = await self.api.get_alarm_info()
            self._fallback_data = ApSystemsSensorData(
                output_data=self._fallback_data.output_data,
                alarm_info=alarm_info,
            )
        else:
            alarm_info = self._fallback_data.alarm_info

        # If max power was not available during setup, retry on first successful poll
        if self.current_max_power is None:
            await self._fetch_max_power()

        # If device info was not available during setup, retry up to 3 times
        # on subsequent polls (every 5th poll) until a value is retrieved.
        if self.device_version == "unknown" and self._device_info_retries < 3:
            self._poll_count_device = getattr(self, "_poll_count_device", 0) + 1
            if self._poll_count_device % 5 == 1:
                try:
                    device_info = await self.api.get_device_info()
                    self.api.max_power = getattr(device_info, "maxPower", 800)
                    self.api.min_power = getattr(device_info, "minPower", 30)
                    self.device_version = getattr(device_info, "devVer", "unknown")
                    self.battery_system = getattr(device_info, "isBatterySystem", False)
                    self.device_ip = getattr(device_info, "ipAddr", "unknown")
                    if self.device_version != "unknown":
                        LOGGER.info(
                            "APsystems inverter info retrieved – firmware: %s, IP: %s",
                            self.device_version,
                            self.device_ip,
                        )
                        self._device_info_retries = 99  # stop retrying
                    else:
                        self._device_info_retries += 1
                except Exception as err:  # noqa: BLE001
                    self._device_info_retries += 1
                    LOGGER.debug(
                        "Could not retrieve inverter info on poll (retry %d/3): %s",
                        self._device_info_retries, _fmt_err(err)
                    )

        if self._consecutive_errors > 0:
            LOGGER.info(
                "APsystems inverter back online after %d consecutive errors.",
                self._consecutive_errors,
            )
            self._consecutive_errors = 0
            self._stable_polls_after_error = 0

        self.inverter_reachable = True

        # Restore power limit after inverter restart.
        # Wait for 3 stable polls (≈36s) before attempting restore to ensure
        # inverter is fully ready. Retry up to 5 times if it fails (Timeout).
        self._stable_polls_after_error += 1
        if self._stable_polls_after_error >= 3 and self.current_max_power is not None:
            if not getattr(self, "_power_limit_restored", False):
                try:
                    inverter_limit = await self.api.get_max_power()
                    LOGGER.debug(
                        "Power limit check: inverter=%sW, stored=%sW",
                        inverter_limit, self.current_max_power
                    )
                    if inverter_limit is not None and abs(float(inverter_limit) - self.current_max_power) > 1:
                        await self.api.set_max_power(int(self.current_max_power))
                        LOGGER.info(
                            "Restored power limit to %sW after inverter restart "
                            "(inverter reported %sW).",
                            self.current_max_power,
                            inverter_limit,
                        )
                        self._power_limit_restored = True
                    else:
                        LOGGER.debug(
                            "Power limit OK after restart: inverter=%sW, stored=%sW.",
                            inverter_limit, self.current_max_power,
                        )
                        self._power_limit_restored = True
                except Exception as err:  # noqa: BLE001
                    LOGGER.warning(
                        "Could not restore power limit (attempt %d): %s",
                        self._stable_polls_after_error - 2, _fmt_err(err)
                    )
                    # Will retry on next poll automatically

        output_data, needs_save = self._compensate_lifetime_energy(output_data)
        if needs_save:
            await self._save_state()
        elif self._poll_count % 10 == 0:
            # Periodically save last_out so it survives HA restarts
            # and prevents lifetime energy jumps after cold start
            await self._save_state()

        result = ApSystemsSensorData(output_data=output_data, alarm_info=alarm_info)
        self._fallback_data = result
        return result
