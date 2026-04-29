"""The coordinator for APsystems local API integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta
import time as _monotonic_time

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
from homeassistant.util import dt as dt_util

from .const import CONF_LIFETIME_OFFSET_P1, CONF_LIFETIME_OFFSET_P2, CONF_POLLING_INTERVAL, DOMAIN, LOGGER, POLLING_INTERVAL

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
class ReturnOutputDataDetail:
    """Extended output data from /getOutputDataDetail endpoint.

    Available on firmware 1.7.0+ – adds voltage, current, grid and temperature.
    Falls back gracefully to None values on older firmware.
    """
    # PV input voltages (V)
    v1: float | None = None
    v2: float | None = None
    # PV input currents (A)
    c1: float | None = None
    c2: float | None = None
    # Grid voltage (V) and frequency (Hz)
    gv: float | None = None
    gf: float | None = None
    # Inverter temperature (°C) – last known value is preserved when offline
    t: float | None = None


def _make_fallback_detail() -> ReturnOutputDataDetail:
    """Return a safe all-zero detail data object for offline state.

    Voltages, currents and grid values are 0 when inverter is offline.
    Temperature is intentionally None here and will be filled with the last
    known value once it has been seen at least once (see _load_offsets).
    """
    return ReturnOutputDataDetail(
        v1=0.0, v2=0.0,
        c1=0.0, c2=0.0,
        gv=0.0, gf=0.0,
        t=None,  # filled with last known value after first successful poll
    )


@dataclass
class ApSystemsSensorData:
    """Representing different APsystems sensor data."""

    output_data: ReturnOutputData
    alarm_info: ReturnAlarmInfo
    detail_data: ReturnOutputDataDetail | None = None


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
    default_max_power: int | None = None  # from /getDefaultMaxPower (flash value)

    # Device IP address shown in device info
    device_ip: str = "unknown"

    # Date of the last flash-write warning (older firmware path).
    # Used to throttle the warning to at most once per day.
    _last_flash_warning_date: date | None = None

    # Count of setMaxPower calls that write to flash (older firmware without
    # getDefaultMaxPower). Persisted across restarts. Shown as a diagnostic
    # sensor so users can track cumulative flash wear.
    flash_write_count: int = 0

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
            detail_data=_make_fallback_detail(),
        )
        # _fallback_detail holds offline values for detail sensors.
        # Voltages/currents/grid → 0 when offline; temperature → last known value.
        self._fallback_detail: ReturnOutputDataDetail = _make_fallback_detail()
        # Last known inverter temperature (°C) – preserved across offline periods
        self._last_temperature: float | None = None
        # True once /getOutputDataDetail has succeeded at least once
        self._detail_supported: bool | None = None  # None = not yet tested

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
        # Timestamp when _poll_active was last set to True.
        # Used to detect stuck locks (e.g. after asyncio.CancelledError).
        self._poll_active_since: float = 0.0
        # Callback registered by sensor platform to dynamically add the
        # flash write count sensor after firmware type is confirmed.
        # Only called once, only when older firmware is detected.
        self._add_flash_sensor: object = None  # set by async_setup_entry
        self._flash_sensor_registered: bool = False

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
            # Use hardware maximum as fallback for api.max_power so the number
            # entity's upper limit is not incorrectly capped at current_max_power.
            # current_max_power is the USER's chosen limit, not the hardware ceiling.
            # We use 1800 (EZ1-D ceiling) as the safe fallback so EZ1-D users are
            # never blocked. The correct value will be set on the next successful poll.
            self.api.max_power = 1800
            self.api.min_power = 30

    async def _reset_flash_to_hardware_max(self) -> None:
        """Reset the flash power limit to the hardware maximum via setDefaultMaxPower.

        Background (verified by hardware tests):
        - On newer firmware (1.9.x+): setMaxPower writes RAM only. After each
          nightly shutdown the inverter reloads the flash value into RAM.
          getDefaultMaxPower reads the flash value; setDefaultMaxPower writes it.
        - On older firmware: setMaxPower writes flash directly and the value
          survives power cycles – no restore needed there.

        Strategy to protect flash longevity:
        - Call setDefaultMaxPower exactly ONCE to set flash to the hardware
          maximum (e.g. 800W for EZ1-M, 1800W for EZ1-D).
        - After that, NEVER write flash again. All user-visible power limit
          changes use setMaxPower (RAM only).
        - Each morning when the inverter restarts, HA detects the RAM/stored
          mismatch and restores the user's limit via setMaxPower.

        Safety guard: we only write if we have a confirmed hardware_max from
        get_device_info() (device_version != 'unknown'). Without this guard
        the fallback of 800W would incorrectly cap an EZ1-D (1800W).
        """
        # Only proceed if device_info was successfully fetched.
        # api.max_power defaults to 800 before get_device_info() runs –
        # using that fallback would wrongly cap EZ1-D at 800W.
        if self.device_version == "unknown":
            LOGGER.debug(
                "Skipping flash reset – device info not yet known. Will retry after next successful poll."
            )
            return

        hardware_max = int(self.api.max_power)
        if hardware_max <= 0:
            LOGGER.debug("Skipping flash reset – hardware max is not valid (%sW).", hardware_max)
            return

        if self.default_max_power == hardware_max:
            LOGGER.debug(
                "Flash already at hardware maximum (%sW) – no write needed.", hardware_max
            )
            return
        try:
            await self.api._request(f"setDefaultMaxPower?p={hardware_max}")
            LOGGER.info(
                "Flash power limit reset to hardware maximum %sW "
                "(was %sW). Flash will not be written again.",
                hardware_max, self.default_max_power,
            )
            self.default_max_power = hardware_max
            await self._save_state()  # persist so restart knows reset is done
        except Exception as err:  # noqa: BLE001
            LOGGER.warning(
                "Could not reset flash power limit to %sW: %s. "
                "Will retry on next startup. RAM-only restore will still work correctly.",
                hardware_max, _fmt_err(err),
            )
            # Do NOT update default_max_power – keeps retry condition True

    async def _fetch_max_power(self) -> None:
        """Fetch the current power limits from the inverter.

        On firmware >= 1.9.x: getDefaultMaxPower = flash (survives restart),
        getMaxPower = RAM (reset to flash on each nightly shutdown).
        On older firmware: getMaxPower = flash (persists across restarts).

        After reading the flash value, _reset_flash_to_hardware_max() is
        called once to ensure flash is at the hardware maximum. All
        subsequent user changes use setMaxPower (RAM only).
        """
        # Try getDefaultMaxPower first (firmware 1.9.x+)
        try:
            resp = await self.api._request("getDefaultMaxPower")
            if resp and resp.get("data", {}).get("power"):
                self.default_max_power = int(resp["data"]["power"])
                # Use RAM value (getMaxPower) as the user's current limit –
                # after nightly restart it equals flash, but during the day
                # the user may have set a different RAM value.
                try:
                    ram_val = await self.api.get_max_power()
                    if ram_val is not None:
                        self.current_max_power = float(ram_val)
                    else:
                        self.current_max_power = float(self.default_max_power)
                except Exception:  # noqa: BLE001
                    self.current_max_power = float(self.default_max_power)
                LOGGER.info(
                    "Power limit – RAM (current): %sW, flash (default): %sW",
                    self.current_max_power, self.default_max_power,
                )
                # One-time: reset flash to hardware max so RAM is the only
                # value we ever change going forward.
                await self._reset_flash_to_hardware_max()
                return
        except Exception:  # noqa: BLE001
            pass  # endpoint not available on this firmware – fall through

        # Older firmware: getMaxPower persists across restarts (writes flash)
        try:
            result = await self.api.get_max_power()
            if result is not None:
                self.current_max_power = float(result)
                LOGGER.debug(
                    "Power limit fetched (getMaxPower, flash-backed): %sW",
                    self.current_max_power,
                )
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

        The user can enter an initial offset in the config flow (setup or
        reconfigure) to correct the lifetime total after a firmware overflow
        reset. The last applied config-entry offset is stored alongside the
        running offset so we can detect when the user has changed it and
        apply only the delta – without touching the accumulated overflow
        compensation that is already correct.
        """
        data = await self._store.async_load()
        cfg = self.config_entry.data
        cfg_p1 = float(cfg.get(CONF_LIFETIME_OFFSET_P1, 0.0))
        cfg_p2 = float(cfg.get(CONF_LIFETIME_OFFSET_P2, 0.0))

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
            dmp = data.get("default_max_power")
            if dmp is not None:
                self.default_max_power = int(dmp)
            self.flash_write_count = int(data.get("flash_write_count", 0))
            if self.flash_write_count > 0:
                self._flash_sensor_registered = False

            # Device info
            self.device_version = data.get("device_version", "unknown")
            self.device_ip = data.get("device_ip", "unknown")

            # Restore fallback data so sensors show last known values immediately
            fb = self._fallback_data.output_data
            fb.p1 = 0.0
            fb.p2 = 0.0
            fb.e1 = float(data.get("fb_e1", 0.0))
            fb.e2 = float(data.get("fb_e2", 0.0))
            fb.te1 = float(data.get("fb_te1", 0.0))
            fb.te2 = float(data.get("fb_te2", 0.0))

            self._last_temperature = data.get("fb_temperature")
            self._fallback_detail = ReturnOutputDataDetail(
                v1=0.0, v2=0.0,
                c1=0.0, c2=0.0,
                gv=0.0, gf=0.0,
                t=self._last_temperature,
            )
            self._fallback_data = ApSystemsSensorData(
                output_data=self._fallback_data.output_data,
                alarm_info=self._fallback_data.alarm_info,
                detail_data=self._fallback_detail,
            )

            # Detect reconfigure: if the config-entry offset differs from
            # the last value we applied, the user changed it → apply the delta.
            prev_p1 = float(data.get("applied_offset_p1", 0.0))
            prev_p2 = float(data.get("applied_offset_p2", 0.0))
            delta_p1 = cfg_p1 - prev_p1
            delta_p2 = cfg_p2 - prev_p2
            if abs(delta_p1) > 0.0001 or abs(delta_p2) > 0.0001:
                self._te1_offset += delta_p1
                self._te2_offset += delta_p2
                LOGGER.info(
                    "Lifetime energy offset updated via reconfigure – "
                    "P1 delta: %+.5f kWh (new total offset: %.5f kWh), "
                    "P2 delta: %+.5f kWh (new total offset: %.5f kWh)",
                    delta_p1, self._te1_offset,
                    delta_p2, self._te2_offset,
                )

            LOGGER.info(
                "Restored state from storage – "
                "te1_out=%.5f kWh, te2_out=%.5f kWh, "
                "e1_protected=%.5f kWh, e2_protected=%.5f kWh, "
                "max_power=%s W, firmware=%s",
                self._te1_last_out or 0.0, self._te2_last_out or 0.0,
                self._e1_protected, self._e2_protected,
                self.current_max_power, self.device_version,
            )
        else:
            # First start – no storage yet. Apply the user-entered initial
            # lifetime offset from the config entry (0.0 if not provided).
            if cfg_p1 or cfg_p2:
                self._te1_offset = cfg_p1
                self._te2_offset = cfg_p2
                LOGGER.info(
                    "Applied initial lifetime energy offset from setup – "
                    "P1: %.5f kWh, P2: %.5f kWh",
                    cfg_p1, cfg_p2,
                )

    async def _save_state(self) -> None:
        """Persist all coordinator state to storage so it survives HA restarts."""
        fb = self._fallback_data.output_data
        cfg = self.config_entry.data
        await self._store.async_save({
            # Lifetime energy overflow compensation
            "te1_offset": self._te1_offset,
            "te2_offset": self._te2_offset,
            "te1_last_raw": self._te1_last_raw,
            "te2_last_raw": self._te2_last_raw,
            "te1_last_out": self._te1_last_out,
            "te2_last_out": self._te2_last_out,
            # Last applied config-entry offset – used to detect reconfigure changes
            "applied_offset_p1": float(cfg.get(CONF_LIFETIME_OFFSET_P1, 0.0)),
            "applied_offset_p2": float(cfg.get(CONF_LIFETIME_OFFSET_P2, 0.0)),
            # Today energy protection
            "e1_protected": self._e1_protected,
            "e2_protected": self._e2_protected,
            "protected_date": self._protected_date.isoformat() if self._protected_date else None,
            # Power limit
            "current_max_power": self.current_max_power,
            # Flash power limit (getDefaultMaxPower) – persisted so we can detect
            # on offline startup whether the one-time flash reset was already done.
            "default_max_power": self.default_max_power,
            # Flash write counter (older firmware only)
            "flash_write_count": self.flash_write_count,
            # Last known sensor values (fallback data)
            "fb_p1": fb.p1,
            "fb_p2": fb.p2,
            "fb_e1": fb.e1,
            "fb_e2": fb.e2,
            "fb_te1": fb.te1,
            "fb_te2": fb.te2,
            # Last known temperature (preserved across offline periods)
            "fb_temperature": self._last_temperature,
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
        today = dt_util.now().date()

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
                LOGGER.info(
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
                LOGGER.info(
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

        Also resets consecutive_errors so the reduced polling rate (every 5th
        poll after 10 errors) does not carry over into the next day and block
        the inverter from being detected when it comes back online in the morning.
        """
        today = dt_util.now().date()
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
            # Reset error counter so the morning polls are all attempted normally.
            # Without this, the reduced polling rate (every 5th poll after 10
            # errors) would persist into the new day and delay inverter detection.
            if self._consecutive_errors > 0:
                LOGGER.debug(
                    "Consecutive error counter reset at midnight (%d → 0).",
                    self._consecutive_errors,
                )
                self._consecutive_errors = 0
                self._skip_poll_counter = 0

    async def _async_update_data(self) -> ApSystemsSensorData:
        """Fetch data from inverter, always returning valid data.

        On error, _fallback_data (last known good values) is returned so
        sensors never become unavailable. Power values are zeroed after
        several consecutive errors to reflect that the inverter is off.
        """
        # Midnight reset runs every poll regardless of inverter state
        self._check_midnight_reset()

        # Guard against stuck _poll_active lock (e.g. after CancelledError).
        # If the lock has been held for more than 30 seconds, force-release it.
        if self._poll_active:
            stuck_for = _monotonic_time.monotonic() - self._poll_active_since
            if stuck_for > 30:
                LOGGER.warning(
                    "poll_active lock was stuck for %.0fs – force-releasing. "
                    "This may indicate a previous poll was cancelled unexpectedly.",
                    stuck_for,
                )
                self._poll_active = False
            else:
                LOGGER.debug("Poll already active – returning cached data.")
                return self._fallback_data

        # Reduce actual API attempts after 10 consecutive errors (inverter is
        # very likely off for the night). Only attempt every 5th poll (~60s
        # effective interval) to save network load and log noise.
        # Uses a separate skip counter so consecutive_errors keeps incrementing
        # correctly and the midnight reset (which checks consecutive_errors) works.
        if self._consecutive_errors > 10:
            self._skip_poll_counter = getattr(self, "_skip_poll_counter", 0) + 1
            if self._skip_poll_counter % 5 != 0:
                return self._fallback_data
        else:
            self._skip_poll_counter = 0

        try:
            self._poll_active = True
            self._poll_active_since = _monotonic_time.monotonic()
            return await self._do_fetch()

        except InverterReturnedError:
            self._consecutive_errors += 1
            if self._consecutive_errors == 1:
                LOGGER.info(
                    "APsystems inverter returned an error – "
                    "serving cached data (likely entering night/standby mode)."
                )
            elif self._consecutive_errors == 10:
                LOGGER.info(
                    "APsystems inverter still returning errors after %d polls (%ds). "
                    "If this is not nightly standby, check the inverter.",
                    self._consecutive_errors,
                    self._consecutive_errors * POLLING_INTERVAL,
                )
            elif self._consecutive_errors % 50 == 0:
                # ~10 min throttle to avoid log flood
                LOGGER.debug(
                    "Inverter error (consecutive: %d) – serving cached data.",
                    self._consecutive_errors,
                )
            # Zero power immediately on any error – prevents false statistics
            self._fallback_data.output_data.p1 = 0
            self._fallback_data.output_data.p2 = 0
            # After 3 failed polls, also zero electrical detail sensors
            # (voltage, current, grid) – inverter has clearly gone offline.
            # Temperature is preserved as last known value.
            if self._consecutive_errors >= 3 and self._fallback_detail is not None:
                self._fallback_detail.v1 = 0.0
                self._fallback_detail.v2 = 0.0
                self._fallback_detail.c1 = 0.0
                self._fallback_detail.c2 = 0.0
                self._fallback_detail.gv = 0.0
                self._fallback_detail.gf = 0.0
                self._fallback_data = ApSystemsSensorData(
                    output_data=self._fallback_data.output_data,
                    alarm_info=self._fallback_data.alarm_info,
                    detail_data=self._fallback_detail,
                )
            self._stable_polls_after_error = 0  # reset – must re-stabilize before restore
            # Only invalidate the restore flag after 3+ consecutive errors (~36s).
            # Brief single-poll glitches (e.g. EZ1 morning startup jitter) should
            # not trigger another setMaxPower write – the EZ1 may not yet be ready
            # to accept commands and will silently discard them.
            if self._consecutive_errors >= 3:
                self._power_limit_restored = False
            self.inverter_reachable = False
            return self._fallback_data

        except Exception as err:  # noqa: BLE001
            self._consecutive_errors += 1
            if self._consecutive_errors == 1:
                LOGGER.info(
                    "APsystems inverter unreachable – "
                    "serving cached data. Error: %s", _fmt_err(err),
                )
            elif self._consecutive_errors == 10:
                LOGGER.info(
                    "APsystems inverter still unreachable after %d polls (%ds). "
                    "Check network connection. Error: %s",
                    self._consecutive_errors,
                    self._consecutive_errors * self.update_interval.total_seconds(),
                    _fmt_err(err),
                )
            elif self._consecutive_errors % 50 == 0:
                # ~10 min throttle (50 × 12 s) to avoid log flood
                LOGGER.debug(
                    "Inverter unreachable (consecutive: %d) – serving cached data.",
                    self._consecutive_errors,
                )
            # Zero power immediately on any error – prevents false statistics
            self._fallback_data.output_data.p1 = 0
            self._fallback_data.output_data.p2 = 0
            # After 3 failed polls, also zero electrical detail sensors
            # (voltage, current, grid) – inverter has clearly gone offline.
            # Temperature is preserved as last known value.
            if self._consecutive_errors >= 3 and self._fallback_detail is not None:
                self._fallback_detail.v1 = 0.0
                self._fallback_detail.v2 = 0.0
                self._fallback_detail.c1 = 0.0
                self._fallback_detail.c2 = 0.0
                self._fallback_detail.gv = 0.0
                self._fallback_detail.gf = 0.0
                self._fallback_data = ApSystemsSensorData(
                    output_data=self._fallback_data.output_data,
                    alarm_info=self._fallback_data.alarm_info,
                    detail_data=self._fallback_detail,
                )
            self._stable_polls_after_error = 0  # reset – must re-stabilize before restore
            # Only invalidate restore flag after 3+ consecutive errors
            if self._consecutive_errors >= 3:
                self._power_limit_restored = False
            self.inverter_reachable = False
            return self._fallback_data

        finally:
            self._poll_active = False

    async def _get_output_data_detail(self) -> ReturnOutputDataDetail | None:
        """Fetch extended output data from /getOutputDataDetail.

        Returns a zero-filled fallback (with last known temperature) if the
        endpoint is not available or the inverter is temporarily unreachable.
        Returns None only when the endpoint is confirmed unsupported by firmware.
        """
        if self._detail_supported is False:
            return None
        try:
            resp = await self.api._request("getOutputDataDetail")
            if resp and resp.get("data"):
                self._detail_supported = True
                d = resp["data"]
                detail = ReturnOutputDataDetail(
                    v1=float(d["v1"]) if d.get("v1") not in (None, "", "null") else None,
                    v2=float(d["v2"]) if d.get("v2") not in (None, "", "null") else None,
                    c1=float(d["c1"]) if d.get("c1") not in (None, "", "null") else None,
                    c2=float(d["c2"]) if d.get("c2") not in (None, "", "null") else None,
                    gv=float(d["gv"]) if d.get("gv") not in (None, "", "null") else None,
                    gf=float(d["gf"]) if d.get("gf") not in (None, "", "null") else None,
                    t=float(d["t"]) if d.get("t") not in (None, "", "null") else None,
                )
                # Track last known temperature for offline preservation
                if detail.t is not None:
                    self._last_temperature = detail.t
                # Update fallback detail with current zeros + last temperature
                self._fallback_detail = ReturnOutputDataDetail(
                    v1=0.0, v2=0.0,
                    c1=0.0, c2=0.0,
                    gv=0.0, gf=0.0,
                    t=self._last_temperature,
                )
                return detail
        except Exception as err:  # noqa: BLE001
            if self._detail_supported is None:
                LOGGER.debug(
                    "getOutputDataDetail not available on this firmware: %s", _fmt_err(err)
                )
                self._detail_supported = False
        return None

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
            self._skip_poll_counter = 0
            self._stable_polls_after_error = 0

        self.inverter_reachable = True

        # On the first successful poll where firmware type is known:
        # if getDefaultMaxPower was never available (default_max_power is None
        # even after _fetch_max_power ran), this is confirmed older firmware.
        # Register the flash write count sensor exactly once.
        # This runs AFTER async_setup_entry, so _add_flash_sensor is set.
        if (
            self.current_max_power is not None  # _fetch_max_power has run
            and self.default_max_power is None  # older firmware confirmed
            and not self._flash_sensor_registered
            and self._add_flash_sensor is not None
        ):
            self._flash_sensor_registered = True
            self._add_flash_sensor()

        # Restore power limit after inverter restart (nightly shutdown on newer firmware).
        # On newer firmware the inverter reloads flash (800W) into RAM each morning.
        # We detect this by comparing getMaxPower with our stored current_max_power
        # and restore via setMaxPower (RAM only – flash is never written here).
        # Wait 3 stable polls (≈36s) before attempting to ensure the inverter is ready.
        # Give up after 5 failed attempts to avoid endless warnings.
        # After a successful restore, verify once more after ~25 polls (~5 min) –
        # the EZ1 may reload flash during its morning startup sequence and undo
        # the first restore attempt.
        _MAX_RESTORE_ATTEMPTS = 5
        self._stable_polls_after_error += 1
        _do_restore = (
            not getattr(self, "_power_limit_restored", False)
            or getattr(self, "_power_limit_verify_poll", None) == self._stable_polls_after_error
        )
        if self._stable_polls_after_error >= 3 and self.current_max_power is not None and _do_restore:
            _restore_attempt = self._stable_polls_after_error - 2
            if not getattr(self, "_power_limit_restored", False) and _restore_attempt <= _MAX_RESTORE_ATTEMPTS:
                try:
                    inverter_limit = await self.api.get_max_power()
                    LOGGER.debug(
                        "Power limit check: inverter RAM=%sW, stored=%sW",
                        inverter_limit, self.current_max_power,
                    )
                    if inverter_limit is not None and abs(float(inverter_limit) - self.current_max_power) > 1:
                        # RAM differs from stored value – restore via setMaxPower only
                        await self.api.set_max_power(int(self.current_max_power))
                        LOGGER.info(
                            "Restored power limit to %sW (RAM) after inverter restart "
                            "(inverter RAM was %sW).",
                            self.current_max_power, inverter_limit,
                        )
                        # Schedule a verification poll ~5 min later to catch cases
                        # where the EZ1 reloads flash again during morning startup
                        self._power_limit_verify_poll = self._stable_polls_after_error + 25
                    else:
                        LOGGER.debug(
                            "Power limit OK after restart: inverter RAM=%sW, stored=%sW.",
                            inverter_limit, self.current_max_power,
                        )
                    self._power_limit_restored = True
                except Exception as err:  # noqa: BLE001
                    if _restore_attempt < _MAX_RESTORE_ATTEMPTS:
                        LOGGER.warning(
                            "Could not restore power limit (attempt %d/%d): %s",
                            _restore_attempt, _MAX_RESTORE_ATTEMPTS, _fmt_err(err),
                        )
                    else:
                        LOGGER.info(
                            "Power limit restore gave up after %d attempts: %s. "
                            "Limit will be restored on next inverter restart.",
                            _MAX_RESTORE_ATTEMPTS, _fmt_err(err),
                        )
                        self._power_limit_restored = True  # stop retrying this session
            elif getattr(self, "_power_limit_verify_poll", None) == self._stable_polls_after_error:
                # Verification poll: check if EZ1 still has the correct limit
                try:
                    inverter_limit = await self.api.get_max_power()
                    if inverter_limit is not None and abs(float(inverter_limit) - self.current_max_power) > 1:
                        await self.api.set_max_power(int(self.current_max_power))
                        LOGGER.info(
                            "Power limit re-applied to %sW (RAM) – EZ1 had reloaded "
                            "flash (%sW) during morning startup.",
                            self.current_max_power, inverter_limit,
                        )
                    else:
                        LOGGER.debug(
                            "Power limit verification OK: inverter RAM=%sW, stored=%sW.",
                            inverter_limit, self.current_max_power,
                        )
                    self._power_limit_verify_poll = None
                except Exception:  # noqa: BLE001
                    self._power_limit_verify_poll = None  # give up silently

        output_data, needs_save = self._compensate_lifetime_energy(output_data)
        if needs_save:
            await self._save_state()
        elif self._poll_count % 10 == 0:
            # Periodically save last_out so it survives HA restarts
            # and prevents lifetime energy jumps after cold start
            await self._save_state()

        detail_data = await self._get_output_data_detail()

        # When detail_data is None (transient error while endpoint IS supported),
        # use the zero-fallback so sensors show 0 instead of "unknown"
        effective_detail = detail_data if detail_data is not None else (
            self._fallback_detail if self._detail_supported else None
        )

        result = ApSystemsSensorData(
            output_data=output_data,
            alarm_info=alarm_info,
            detail_data=effective_detail,
        )
        self._fallback_data = result
        return result
