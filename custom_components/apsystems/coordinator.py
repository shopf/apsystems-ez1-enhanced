"""The coordinator for APsystems local API integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta
import time

from APsystemsEZ1 import (
    APsystemsEZ1M,
    InverterReturnedError,
    ReturnAlarmInfo,
    ReturnOutputData,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_IP_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_SYSTEM,
    CONF_DETAIL_POLL,
    CONF_LIFETIME_OFFSET_P1,
    CONF_LIFETIME_OFFSET_P2,
    CONF_POLLING_INTERVAL,
    CONF_SHOWN_OFFSET_P1,
    CONF_SHOWN_OFFSET_P2,
    CONF_SLOW_DETAIL_POLL,
    DOMAIN,
    LOGGER,
    MODEL_BY_MAX_POWER,
    POLLING_INTERVAL,
    STORE_KEY,
    STORE_VERSION,
    UNKNOWN_MODEL_NAME,
)

_OVERFLOW_RESET_THRESHOLD = 500.0  # kWh – real firmware overflow drops from ~540 to ~0

# Today-energy (e1/e2) protection no longer uses a fixed "is this close enough
# to zero" threshold (see _compensate_lifetime_energy). Field reports showed
# the firmware bug does not reliably reset e1/e2 to exactly 0.0 – it can land
# on an arbitrary intermediate value (observed: 0.01 kWh, ~1.1 kWh, ~0.3 kWh).
# Since today's cumulative energy can physically never decrease except at
# midnight (handled separately in _check_midnight_reset), any decrease is
# now clamped to the previous highest value, regardless of its size. This
# constant only throttles the INFO-level log message so that sub-rounding
# jitter (like with te1/te2) does not spam the log at INFO level.
_TODAY_DROP_LOG_EPSILON = 0.01  # kWh – drops smaller than this only log at DEBUG

# Maximum difference allowed between e1_raw and te1_delta (te1_now - te1_at_midnight)
# before a post-midnight carry-over is assumed. Covers minor measurement-timing
# differences between the firmware's daily and lifetime counters.
_TODAY_TE_DELTA_TOLERANCE = 0.02  # kWh


# Alarm info is read every Nth poll to reduce load on the inverter and
# avoid WLAN reconnects on firmware 1.12.2 which reconnects frequently.
# Output data is read on every poll.
_ALARM_POLL_INTERVAL = 10

# /getOutputDataDetail (voltage, current, temperature, grid data) is a heavier
# endpoint than /getOutputData. Some models – notably the EZ1-D on firmware
# 2.2.6 – need 20-30 s of HTTP-server recovery time after each call. Calling
# it on every poll therefore causes a reliable timeout cycle. We decouple it
# from the main poll and call it at most once every 60 seconds regardless of
# the configured polling interval. Voltage / temperature sensors stay fresh
# enough; power / energy sensors continue updating on every poll.
_DETAIL_MIN_INTERVAL = 60.0  # seconds between getOutputDataDetail calls

_SWITCH_RESTORE_START_POLL = 3
_SWITCH_RESTORE_MAX_ATTEMPTS = 5
_SWITCH_RESTORE_VERIFY_INTERVAL = 180


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
    t: float | None = None  # inverter temperature, preserved when offline


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
    _protected_date: date | None = None  # set to today in __init__, None only before first poll
    # Lifetime energy at the start of the current day (set at each midnight reset).
    # Used to derive true today production independently of the firmware's daily
    # counter (e1/e2), which may carry over from the previous day without resetting.
    _te1_day_start: float | None = None
    _te2_day_start: float | None = None
    _stable_polls_after_error: int = 0  # counts successful polls after reconnect
    _device_info_retries: int = 0  # counts remaining retries for device info
    default_max_power: int | None = None  # from /getDefaultMaxPower (flash value)

    # Log throttle counters – reset at midnight or when condition clears.
    # Prevents per-poll DEBUG spam during multi-hour firmware-bug windows.
    _e1_carryover_count: int = 0  # increments each poll while carry-over active
    _e2_carryover_count: int = 0
    _e1_hold_count: int = 0       # increments each poll while monotonic hold active
    _e2_hold_count: int = 0

    # Timestamp of the last successful getOutputDataDetail call (monotonic).
    # float('-inf') ensures the first poll always triggers a real API call,
    # even if time.monotonic() is less than _DETAIL_MIN_INTERVAL seconds
    # (e.g. on a freshly booted system or container where monotonic time
    # starts near zero). Reset to 0.0 on reconnect – by then uptime is
    # always well above 60 s so the first post-outage poll fires immediately.
    _detail_last_ts: float = float("-inf")

    # Device IP address shown in device info
    device_ip: str = "unknown"

    # Date of the last flash-write warning (older firmware path).
    # Used to throttle the warning to at most once per day.
    _last_flash_warning_date: date | None = None

    # Count of setMaxPower calls that write to flash (older firmware without
    # getDefaultMaxPower). Persisted across restarts. Shown as a diagnostic
    # sensor so users can track cumulative flash wear.
    flash_write_count: int = 0

    # Desired inverter on/off state as set by the user via the switch entity.
    # Persisted so it can be restored after the inverter reboots overnight.
    # True = on (default), False = off.
    inverter_switch_on: bool = True

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

        # Reduced-polling skip counter (used after 10 consecutive errors)
        self._skip_poll_counter: int = 0

        # Power limit restore state after inverter restart
        self._power_limit_restored: bool = False
        self._power_limit_verify_at: float = 0.0  # monotonic timestamp, 0 = not scheduled
        self._power_limit_verify_count: int = 0   # number of verification rounds done

        # Switch (on/off) restore state after inverter restart
        self._switch_restore_done: bool = False
        self._switch_restore_verify_at: float = 0.0

        # Initialize protected_date to today so midnight reset fires correctly
        # even if the inverter never delivers a successful poll before midnight
        self._protected_date: date = dt_util.now().date()

        # Counts successful polls after a reconnect – used for restore timing
        self._stable_polls_after_error: int = 0

        # Counter for device-info retry polls
        self._poll_count_device: int = 0
        self._store: Store = Store(
            hass,
            STORE_VERSION,
            f"{STORE_KEY}_{config_entry.entry_id}",
        )
        # Timestamp when _poll_active was last set to True.
        # Used to detect stuck locks (e.g. after asyncio.CancelledError).
        self._poll_active_since: float = time.monotonic()
        # Callback registered by sensor platform to dynamically add the
        # flash write count sensor after firmware type is confirmed.
        # Only called once, only when older firmware is detected.
        self._add_flash_sensor: object = None  # set by async_setup_entry
        self._flash_sensor_registered: bool = False
        # Set to True once an "unknown model" warning has been logged for
        # this coordinator instance, so it is only logged once per HA run.
        self._unknown_model_logged: bool = False

    @property
    def _log_id(self) -> str:
        """Short identifier for log messages – configured IP, always available.

        Uses the IP entered in the config flow (present from the very first log
        message, before any successful API poll).  Falls back to the IP reported
        by the inverter once that is known.
        """
        return self.config_entry.data.get(CONF_IP_ADDRESS, self.device_ip)

    @property
    def detected_model(self) -> str:
        """Return the inverter model name, detected from the hardware maxPower value.

        self.api.max_power holds the hardware maximum output power (VA) as
        reported by getDeviceInfo() (field "maxPower") at startup / first
        successful poll. This value never changes for a given physical
        device, unlike current_max_power/default_max_power which reflect
        the user's chosen output limit and can be lower than the hardware
        ceiling. Using the hardware value avoids misdetecting a deliberately
        throttled EZ1-D/EZ1-H as a smaller model.

        Guard: while device_version == "unknown", get_device_info() has not
        yet succeeded and api.max_power may still hold the *fallback* value
        of 1800 set in _async_setup() (chosen so EZ1-D users are never
        capped at 800W during this brief window). That fallback is not a
        real reading and must not be treated as a confirmed EZ1D – without
        this guard every inverter would briefly show as "EZ1D" right after
        a HA restart, until the next successful poll confirms the real
        model.
        """
        if self.device_version == "unknown":
            return UNKNOWN_MODEL_NAME
        hardware_max = int(self.api.max_power or 0)
        model = MODEL_BY_MAX_POWER.get(hardware_max)
        if model is None:
            self._check_known_model(hardware_max)
            return UNKNOWN_MODEL_NAME
        return model

    def _check_known_model(self, hardware_max: int) -> None:
        """Log a one-time warning if the hardware maxPower value is unknown.

        This can happen for two reasons:
        - A new/different EZ1 model not yet listed in MODEL_BY_MAX_POWER.
        - The value has not been fetched yet (hardware_max == 0), e.g.
          right at startup before get_device_info() has completed. This
          case is intentionally not warned about.
        """
        if hardware_max <= 0:
            return  # not fetched yet – not an unknown model, just not ready
        if self._unknown_model_logged:
            return
        LOGGER.warning(
            "[%s] Unknown EZ1 device (maxPower=%sVA). Please open an issue at "
            "https://github.com/shopf/apsystems-ez1-enhanced/issues "
            "and report maxPower=%sVA so the model can be added.",
            self._log_id, hardware_max, hardware_max,
        )
        self._unknown_model_logged = True

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
                "[%s] Inverter connected – firmware: %s, battery system: %s",
                self._log_id,
                self.device_version,
                self.battery_system,
            )
            await self._fetch_max_power()
        except Exception as err:  # noqa: BLE001
            LOGGER.debug(
                "[%s] Inverter not reachable during setup – using fallback values. "
                "Will retry on next poll. Error: %s", self._log_id, _fmt_err(err)
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
                "[%s] Skipping flash reset – device info not yet known."
                " Will retry after next successful poll.", self._log_id,
            )
            return

        hardware_max = int(self.api.max_power)
        if hardware_max <= 0:
            LOGGER.debug(
                "[%s] Skipping flash reset – hardware max is not valid (%sW).",
                self._log_id, hardware_max,
            )
            return

        if self.default_max_power == hardware_max:
            LOGGER.debug(
                "[%s] Flash already at hardware maximum (%sW) – no write needed.",
                self._log_id, hardware_max,
            )
            return
        try:
            await self.api._request(f"setDefaultMaxPower?p={hardware_max}")
            LOGGER.debug(
                "[%s] Flash power limit reset to hardware maximum %sW "
                "(was %sW). Flash will not be written again.",
                self._log_id, hardware_max, self.default_max_power,
            )
            self.default_max_power = hardware_max
            await self._save_state()  # persist so restart knows reset is done
        except Exception as err:  # noqa: BLE001
            LOGGER.warning(
                "[%s] Could not reset flash power limit to %sW: %s. "
                "Will retry on next startup. RAM-only restore will still work correctly.",
                self._log_id, hardware_max, _fmt_err(err),
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
                self.default_max_power = int(float(resp["data"]["power"]))
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
                LOGGER.debug(
                    "[%s] Power limit – RAM (current): %sW, flash (default): %sW",
                    self._log_id, self.current_max_power, self.default_max_power,
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
                    "[%s] Power limit fetched (getMaxPower, flash-backed): %sW",
                    self._log_id, self.current_max_power,
                )
            else:
                LOGGER.warning(
                    "[%s] Inverter returned no value for max power limit. "
                    "The power limit entity may not be available.",
                    self._log_id,
                )
        except Exception as err:  # noqa: BLE001
            LOGGER.warning(
                "[%s] Could not fetch max power limit from inverter: %s. "
                "The power limit entity may not be available.",
                self._log_id, _fmt_err(err),
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
            self._te1_offset = float(data.get("te1_offset", 0.0))
            self._te2_offset = float(data.get("te2_offset", 0.0))
            self._te1_last_raw = data.get("te1_last_raw")
            self._te2_last_raw = data.get("te2_last_raw")
            self._te1_last_out = data.get("te1_last_out")
            self._te2_last_out = data.get("te2_last_out")

            self._e1_protected = float(data.get("e1_protected", 0.0))
            self._e2_protected = float(data.get("e2_protected", 0.0))
            pd = data.get("protected_date")
            self._protected_date = date.fromisoformat(pd) if pd else dt_util.now().date()

            # Restore day-start lifetime anchors for post-midnight carry-over detection.
            # Only restore if protected_date matches today – if it's a new day, the
            # midnight reset will set fresh anchors on the next poll.
            if self._protected_date == dt_util.now().date():
                te1ds = data.get("te1_day_start")
                te2ds = data.get("te2_day_start")
                self._te1_day_start = float(te1ds) if te1ds is not None else None
                self._te2_day_start = float(te2ds) if te2ds is not None else None

                # If te1_day_start is missing (null) – e.g. after upgrading from an
                # older version or after a same-day reconfigure before midnight – derive
                # it from the corrected lifetime total minus today's protected production.
                # Without a valid anchor, the carry-over check and te1_delta floor are
                # both disabled, causing today energy sensors to freeze whenever the
                # firmware's e1/e2 counter resets below _e1_protected.
                if self._te1_day_start is None and self._te1_last_out is not None:
                    self._te1_day_start = self._te1_last_out - self._e1_protected
                    LOGGER.debug(
                        "[%s] te1_day_start derived from te1_last_out − e1_protected"
                        " = %.5f kWh (te1_day_start was not saved).",
                        self._log_id, self._te1_day_start,
                    )
                if self._te2_day_start is None and self._te2_last_out is not None:
                    self._te2_day_start = self._te2_last_out - self._e2_protected

            mp = data.get("current_max_power")
            if mp is not None:
                self.current_max_power = float(mp)
            dmp = data.get("default_max_power")
            if dmp is not None:
                self.default_max_power = int(dmp)
            self.flash_write_count = int(data.get("flash_write_count", 0))
            if self.flash_write_count > 0:
                self._flash_sensor_registered = False
            self.inverter_switch_on = bool(data.get("inverter_switch_on", True))

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

            # Detect reconfigure: compute the delta between what the user entered
            # in the reconfigure dialog and the reference value that was shown to
            # them at the time they opened it.
            #
            # CONF_SHOWN_OFFSET_P1 is written by the reconfigure flow to record
            # exactly what was pre-filled in the dialog.  This is the correct
            # reference for the delta, covering both the normal case (user changed
            # a value they entered themselves) and the bug-recovery case (storage
            # held an unexpected offset while the config entry showed 0.0).
            #
            # Falls back to applied_offset_p1 (backward compat with older versions
            # that did not write shown_offset) and then to 0 if neither is present.
            shown_p1 = cfg.get(CONF_SHOWN_OFFSET_P1)
            shown_p2 = cfg.get(CONF_SHOWN_OFFSET_P2)
            if shown_p1 is not None:
                prev_p1 = float(shown_p1)
                prev_p2 = float(shown_p2) if shown_p2 is not None else 0.0
                # Clear the transient keys now that we have consumed them.
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={
                        k: v for k, v in self.config_entry.data.items()
                        if k not in (CONF_SHOWN_OFFSET_P1, CONF_SHOWN_OFFSET_P2)
                    },
                )
            else:
                prev_p1 = float(data.get("applied_offset_p1", 0.0))
                prev_p2 = float(data.get("applied_offset_p2", 0.0))
            delta_p1 = cfg_p1 - prev_p1
            delta_p2 = cfg_p2 - prev_p2
            if abs(delta_p1) > 0.0001 or abs(delta_p2) > 0.0001:
                self._te1_offset += delta_p1
                self._te2_offset += delta_p2
                if self._te1_last_out is not None:
                    self._te1_last_out += delta_p1
                if self._te2_last_out is not None:
                    self._te2_last_out += delta_p2
                # Keep te1_day_start consistent after the offset shift so the
                # carry-over check and te1_delta floor remain valid.
                # If no anchor existed yet (null), derive it now from the
                # corrected lifetime total and today's protected production.
                if self._te1_day_start is not None:
                    self._te1_day_start += delta_p1
                elif self._te1_last_out is not None:
                    self._te1_day_start = self._te1_last_out - self._e1_protected
                if self._te2_day_start is not None:
                    self._te2_day_start += delta_p2
                elif self._te2_last_out is not None:
                    self._te2_day_start = self._te2_last_out - self._e2_protected
                LOGGER.info(
                    "[%s] Lifetime energy offset updated via reconfigure – "
                    "P1 delta: %+.5f kWh (new total: %.5f kWh), "
                    "P2 delta: %+.5f kWh (new total: %.5f kWh)",
                    self._log_id,
                    delta_p1, self._te1_offset,
                    delta_p2, self._te2_offset,
                )

            LOGGER.debug(
                "[%s] Restored state from storage – "
                "te1_out=%.5f kWh, te2_out=%.5f kWh, "
                "e1_protected=%.5f kWh, e2_protected=%.5f kWh, "
                "te1_day_start=%s kWh, max_power=%s W, firmware=%s",
                self._log_id,
                self._te1_last_out or 0.0, self._te2_last_out or 0.0,
                self._e1_protected, self._e2_protected,
                f"{self._te1_day_start:.5f}" if self._te1_day_start is not None else "not set",
                self.current_max_power, self.device_version,
            )
        else:
            # First start – no storage yet. Apply the user-entered initial
            # lifetime offset from the config entry (0.0 if not provided).
            if cfg_p1 or cfg_p2:
                self._te1_offset = cfg_p1
                self._te2_offset = cfg_p2
                LOGGER.debug(
                    "[%s] Applied initial lifetime energy offset from setup – "
                    "P1: %.5f kWh, P2: %.5f kWh",
                    self._log_id, cfg_p1, cfg_p2,
                )

    async def _save_state(self) -> None:
        """Persist all coordinator state to storage so it survives HA restarts."""
        fb = self._fallback_data.output_data
        cfg = self.config_entry.data
        try:
            await self._store.async_save({
                "te1_offset": self._te1_offset,
                "te2_offset": self._te2_offset,
                "te1_last_raw": self._te1_last_raw,
                "te2_last_raw": self._te2_last_raw,
                "te1_last_out": self._te1_last_out,
                "te2_last_out": self._te2_last_out,
                # Used to detect reconfigure changes (delta vs. last applied offset)
                "applied_offset_p1": float(cfg.get(CONF_LIFETIME_OFFSET_P1, 0.0)),
                "applied_offset_p2": float(cfg.get(CONF_LIFETIME_OFFSET_P2, 0.0)),
                "e1_protected": self._e1_protected,
                "e2_protected": self._e2_protected,
                "protected_date": self._protected_date.isoformat(),
                "te1_day_start": self._te1_day_start,
                "te2_day_start": self._te2_day_start,
                "current_max_power": self.current_max_power,
                # Persisted so we can detect on offline startup whether the
                # one-time flash reset to hardware max was already done.
                "default_max_power": self.default_max_power,
                "flash_write_count": self.flash_write_count,
                "inverter_switch_on": self.inverter_switch_on,
                "fb_p1": fb.p1,
                "fb_p2": fb.p2,
                "fb_e1": fb.e1,
                "fb_e2": fb.e2,
                "fb_te1": fb.te1,
                "fb_te2": fb.te2,
                "fb_temperature": self._last_temperature,
                "device_version": self.device_version,
                "device_ip": self.device_ip,
            })
        except Exception as err:  # noqa: BLE001
            LOGGER.warning(
                "[%s] Could not persist coordinator state to storage: %s. "
                "Sensor history and power limit may not survive a restart.",
                self._log_id, _fmt_err(err),
            )

    def _compensate_lifetime_energy(self, output_data: ReturnOutputData) -> tuple[ReturnOutputData, bool]:
        """Compensate for two known EZ1-M lifetime energy issues:

        1. OVERFLOW BUG: At ~540 kWh the firmware resets te1/te2 to 0.
           Detected when raw value drops by more than _OVERFLOW_RESET_THRESHOLD
           vs last raw value. Offset is accumulated so HA sees a continuously
           increasing total.

        2. ROUNDING JITTER: Inverter occasionally returns a marginally smaller
           value due to firmware floating point rounding (e.g. 176.58319 → 176.58315).
           Fixed by tracking the last value sent to HA and never going below it.
           This eliminates the HA 'state is not strictly increasing' warning.
        """
        te1_raw = output_data.te1
        te2_raw = output_data.te2

        # 1. Detect and compensate overflow reset
        needs_save = False
        if self._te1_last_raw is not None and te1_raw < (self._te1_last_raw - _OVERFLOW_RESET_THRESHOLD):
            self._te1_offset += self._te1_last_raw
            needs_save = True
            LOGGER.warning(
                "[%s] Lifetime energy counter reset on Input 1! "
                "Previous: %.5f kWh → New: %.5f kWh. "
                "Accumulated offset: %.5f kWh. HA counter continues correctly.",
                self._log_id, self._te1_last_raw, te1_raw, self._te1_offset,
            )

        if self._te2_last_raw is not None and te2_raw < (self._te2_last_raw - _OVERFLOW_RESET_THRESHOLD):
            self._te2_offset += self._te2_last_raw
            needs_save = True
            LOGGER.warning(
                "[%s] Lifetime energy counter reset on Input 2! "
                "Previous: %.5f kWh → New: %.5f kWh. "
                "Accumulated offset: %.5f kWh. HA counter continues correctly.",
                self._log_id, self._te2_last_raw, te2_raw, self._te2_offset,
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
        # The inverter can momentarily report e1/e2 below the true cumulative
        # value for today – sometimes (close to) a full reset to 0, sometimes
        # a partial drop to some arbitrary intermediate value caused by an
        # internal recalculation. A fixed "is this near zero" threshold is
        # fundamentally unable to catch all cases (field reports showed drops
        # to 0.01 kWh, ~50% of the daily total, and values landing just above
        # whatever threshold was configured).
        #
        # Today's cumulative energy can physically never decrease within the
        # same day – the only legitimate reset is exactly at midnight, which
        # is handled separately in _check_midnight_reset(). We therefore use
        # the same monotonic-floor strategy already used above for te1/te2:
        # any decrease, however small or large, is clamped to the previous
        # highest value seen today.
        #
        # POST-MIDNIGHT CARRY-OVER (additional protection, e.g. battery systems):
        # On some setups (e.g. EZ1 behind a Marstek B2500) the inverter runs
        # continuously through midnight. Its firmware never resets the daily
        # e1/e2 counter at midnight, so the first post-midnight value is the
        # previous day's total, not 0. The monotonic floor alone cannot catch
        # this because midnight resets _e1_protected to 0, making any positive
        # carry-over value appear "new".
        #
        # Fix: use the independently tracked lifetime counter delta as the
        # authoritative source for today's production. At midnight,
        # _check_midnight_reset() saves _te1_day_start = _te1_last_out. Then
        # te1_delta = te1_now - te1_day_start represents true today production
        # (te1 is monotonic and overflow-compensated). If e1_raw exceeds
        # te1_delta by more than _TODAY_TE_DELTA_TOLERANCE, it is carry-over
        # and is replaced by te1_delta before the monotonic floor runs.
        e1_raw = output_data.e1
        e2_raw = output_data.e2
        today = dt_util.now().date()

        # Midnight reset is handled in _async_update_data – see _check_midnight_reset()

        # Compute te1/te2 deltas once – used for both carry-over detection and as a
        # continuously-growing floor during intra-day firmware resets (see hold branch).
        te1_delta_e1: float | None = None
        te2_delta_e2: float | None = None

        if self._te1_day_start is not None:
            te1_delta_e1 = max(0.0, te1 - self._te1_day_start)
            if e1_raw > te1_delta_e1 + _TODAY_TE_DELTA_TOLERANCE:
                self._e1_carryover_count += 1
                if self._e1_carryover_count == 1 or self._e1_carryover_count % 50 == 0:
                    LOGGER.debug(
                        "[%s] e1 carry-over: firmware=%.3f kWh, te1_delta=%.3f kWh"
                        " – overriding (poll %d).",
                        self._log_id, e1_raw, te1_delta_e1, self._e1_carryover_count,
                    )
                e1_raw = te1_delta_e1
            else:
                self._e1_carryover_count = 0  # carry-over resolved

        if self._te2_day_start is not None:
            te2_delta_e2 = max(0.0, te2 - self._te2_day_start)
            if e2_raw > te2_delta_e2 + _TODAY_TE_DELTA_TOLERANCE:
                self._e2_carryover_count += 1
                if self._e2_carryover_count == 1 or self._e2_carryover_count % 50 == 0:
                    LOGGER.debug(
                        "[%s] e2 carry-over: firmware=%.3f kWh, te2_delta=%.3f kWh"
                        " – overriding (poll %d).",
                        self._log_id, e2_raw, te2_delta_e2, self._e2_carryover_count,
                    )
                e2_raw = te2_delta_e2
            else:
                self._e2_carryover_count = 0  # carry-over resolved

        if e1_raw < self._e1_protected:
            # Use te1_delta as a continuously-growing floor so the sensor keeps
            # updating even while the firmware holds e1 at 0 after an intra-day
            # reset. Without this, the sensor would freeze at _e1_protected until
            # e1_raw eventually climbs back above it.
            effective_e1 = max(self._e1_protected, te1_delta_e1 if te1_delta_e1 is not None else 0.0)
            drop = effective_e1 - e1_raw
            self._e1_hold_count += 1
            if not self._e1_reset_logged and drop > _TODAY_DROP_LOG_EPSILON:
                LOGGER.debug(
                    "[%s] Today energy (e1) dropped to %.3f kWh"
                    " (floor %.3f kWh) – firmware bug, holding until recovered.",
                    self._log_id, e1_raw, effective_e1,
                )
                self._e1_reset_logged = True
            elif self._e1_hold_count % 50 == 0:
                LOGGER.debug(
                    "[%s] e1 hold active: firmware=%.3f kWh, floor=%.3f kWh (poll %d).",
                    self._log_id, e1_raw, effective_e1, self._e1_hold_count,
                )
            self._e1_protected = effective_e1
            output_data.e1 = effective_e1
        else:
            self._e1_protected = e1_raw
            self._protected_date = today
            self._e1_reset_logged = False
            self._e1_hold_count = 0
            output_data.e1 = e1_raw  # propagate carry-over override (if fired) to sensor

        if e2_raw < self._e2_protected:
            effective_e2 = max(self._e2_protected, te2_delta_e2 if te2_delta_e2 is not None else 0.0)
            drop = effective_e2 - e2_raw
            self._e2_hold_count += 1
            if not self._e2_reset_logged and drop > _TODAY_DROP_LOG_EPSILON:
                LOGGER.debug(
                    "[%s] Today energy (e2) dropped to %.3f kWh"
                    " (floor %.3f kWh) – firmware bug, holding until recovered.",
                    self._log_id, e2_raw, effective_e2,
                )
                self._e2_reset_logged = True
            elif self._e2_hold_count % 50 == 0:
                LOGGER.debug(
                    "[%s] e2 hold active: firmware=%.3f kWh, floor=%.3f kWh (poll %d).",
                    self._log_id, e2_raw, effective_e2, self._e2_hold_count,
                )
            self._e2_protected = effective_e2
            output_data.e2 = effective_e2
        else:
            self._e2_protected = e2_raw
            self._protected_date = today
            self._e2_reset_logged = False
            self._e2_hold_count = 0
            output_data.e2 = e2_raw  # propagate carry-over override (if fired) to sensor

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
        if today != self._protected_date:
            LOGGER.debug(
                "[%s] Today energy counters reset at midnight – P1: %.3f kWh, P2: %.3f kWh.",
                self._log_id, self._e1_protected, self._e2_protected,
            )
            # Anchor te1/te2 lifetime values at midnight so that carry-over detection
            # in _compensate_lifetime_energy can derive true today production as
            # te1_now - te1_day_start, independently of the firmware's daily counter.
            self._te1_day_start = self._te1_last_out
            self._te2_day_start = self._te2_last_out
            self._e1_protected = 0.0
            self._e2_protected = 0.0
            self._e1_reset_logged = False
            self._e2_reset_logged = False
            self._e1_carryover_count = 0
            self._e2_carryover_count = 0
            self._e1_hold_count = 0
            self._e2_hold_count = 0
            self._protected_date = today
            # Also reset fallback data today energy values
            self._fallback_data.output_data.e1 = 0.0
            self._fallback_data.output_data.e2 = 0.0
            LOGGER.debug("[%s] Fallback data today energy reset to 0 at midnight.", self._log_id)
            # Reset error counter so the morning polls are all attempted normally.
            if self._consecutive_errors > 0:
                LOGGER.debug(
                    "[%s] Consecutive error counter reset at midnight (%d → 0).",
                    self._log_id, self._consecutive_errors,
                )
                self._consecutive_errors = 0
                self._skip_poll_counter = 0
                self._switch_restore_done = False
                self._switch_restore_verify_at = 0.0

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
            stuck_for = time.monotonic() - self._poll_active_since
            if stuck_for > 30:
                LOGGER.warning(
                    "[%s] poll_active lock stuck for %.0fs – force-releasing.",
                    self._log_id, stuck_for,
                )
                self._poll_active = False
            else:
                LOGGER.debug("[%s] Poll already active – returning cached data.", self._log_id)
                return self._fallback_data

        # Reduce actual API attempts after 10 consecutive errors (inverter is
        # very likely off for the night). Only attempt every 5th poll (~60s
        # effective interval) to save network load and log noise.
        # Uses a separate skip counter so consecutive_errors keeps incrementing
        # correctly and the midnight reset (which checks consecutive_errors) works.
        if self._consecutive_errors > 10:
            self._skip_poll_counter += 1
            if self._skip_poll_counter % 5 != 0:
                return self._fallback_data
        else:
            self._skip_poll_counter = 0

        try:
            self._poll_active = True
            self._poll_active_since = time.monotonic()
            return await self._do_fetch()

        except InverterReturnedError:
            self._consecutive_errors += 1
            if self._consecutive_errors == 1:
                LOGGER.debug(
                    "[%s] Inverter returned an error – serving cached data "
                    "(likely entering night/standby mode).", self._log_id,
                )
            elif self._consecutive_errors == 10:
                LOGGER.debug(
                    "[%s] Inverter still returning errors after %d polls (%ds).",
                    self._log_id, self._consecutive_errors,
                    self._consecutive_errors * POLLING_INTERVAL,
                )
            elif self._consecutive_errors % 50 == 0:
                LOGGER.debug(
                    "[%s] Inverter error (consecutive: %d) – serving cached data.",
                    self._log_id, self._consecutive_errors,
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
            self._stable_polls_after_error = 0
            if self._consecutive_errors >= 3:
                self._power_limit_restored = False
            if self._consecutive_errors >= 2:
                self._switch_restore_done = False
                self._switch_restore_verify_at = 0.0
            self.inverter_reachable = False
            return self._fallback_data

        except Exception as err:  # noqa: BLE001
            self._consecutive_errors += 1
            if self._consecutive_errors == 1:
                LOGGER.debug(
                    "[%s] Inverter unreachable – serving cached data. Error: %s",
                    self._log_id, _fmt_err(err),
                )
            elif self._consecutive_errors == 10:
                LOGGER.debug(
                    "[%s] Inverter still unreachable after %d polls (%ds). Error: %s",
                    self._log_id, self._consecutive_errors,
                    int(self._consecutive_errors * self.update_interval.total_seconds()),
                    _fmt_err(err),
                )
            elif self._consecutive_errors % 50 == 0:
                LOGGER.debug(
                    "[%s] Inverter unreachable (consecutive: %d) – serving cached data.",
                    self._log_id, self._consecutive_errors,
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
            self._stable_polls_after_error = 0
            if self._consecutive_errors >= 3:
                self._power_limit_restored = False
            if self._consecutive_errors >= 2:
                self._switch_restore_done = False
                self._switch_restore_verify_at = 0.0
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
                d = resp["data"]
                # Auto-detect: some firmware versions (e.g. EZ1-D 1.0.3) respond
                # with SUCCESS but only return basic output fields (p1, e1, te1 …)
                # without voltage/current. Stop polling the endpoint in that case.
                has_detail_fields = any(
                    d.get(k) not in (None, "", "null") for k in ("v1", "v2", "c1", "c2")
                )
                if not has_detail_fields:
                    LOGGER.debug(
                        "[%s] getOutputDataDetail response lacks voltage/current fields"
                        " – marking as unsupported (firmware may not implement it).",
                        self._log_id,
                    )
                    self._detail_supported = False
                    return None
                self._detail_supported = True
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
                    "[%s] getOutputDataDetail not available on this firmware: %s",
                    self._log_id, _fmt_err(err),
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
            self._poll_count_device += 1
            if self._poll_count_device % 5 == 1:
                try:
                    device_info = await self.api.get_device_info()
                    self.api.max_power = getattr(device_info, "maxPower", 800)
                    self.api.min_power = getattr(device_info, "minPower", 30)
                    self.device_version = getattr(device_info, "devVer", "unknown")
                    self.battery_system = getattr(device_info, "isBatterySystem", False)
                    self.device_ip = getattr(device_info, "ipAddr", "unknown")
                    if self.device_version != "unknown":
                        LOGGER.debug(
                            "[%s] Inverter info retrieved – firmware: %s",
                            self._log_id, self.device_version,
                        )
                        self._device_info_retries = 99  # stop retrying
                    else:
                        self._device_info_retries += 1
                except Exception as err:  # noqa: BLE001
                    self._device_info_retries += 1
                    LOGGER.debug(
                        "[%s] Could not retrieve inverter info on poll (retry %d/3): %s",
                        self._log_id, self._device_info_retries, _fmt_err(err),
                    )

        if self._consecutive_errors > 0:
            LOGGER.debug(
                "[%s] Inverter back online after %d consecutive errors.",
                self._log_id, self._consecutive_errors,
            )
            self._consecutive_errors = 0
            self._skip_poll_counter = 0
            self._stable_polls_after_error = 0
            self._power_limit_restored = False
            self._power_limit_verify_count = 0
            self._switch_restore_done = False
            self._switch_restore_verify_at = 0.0
            self._detail_last_ts = 0.0  # fetch fresh detail data on first post-outage poll

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
        # After a successful restore, verify repeatedly until _MAX_VERIFY_ROUNDS is
        # reached – battery systems (isBatterySystem=True) may reload flash multiple
        # times during morning startup depending on battery charge state.
        # Standard:  5 rounds × 10 min = up to 50 min of monitoring
        # Battery:  10 rounds ×  5 min = up to 50 min of monitoring (more frequent)
        _MAX_RESTORE_ATTEMPTS = 5
        _is_battery = bool(self.config_entry.data.get(CONF_BATTERY_SYSTEM, False))
        _MAX_VERIFY_ROUNDS = 10 if _is_battery else 5
        _VERIFY_INTERVAL = 300 if _is_battery else 600  # seconds between verify rounds

        self._stable_polls_after_error += 1
        _now = time.monotonic()
        _verify_due = self._power_limit_verify_at > 0 and _now >= self._power_limit_verify_at
        _do_restore = not self._power_limit_restored or _verify_due
        if self._stable_polls_after_error >= 3 and self.current_max_power is not None and _do_restore:
            _restore_attempt = self._stable_polls_after_error - 2
            if not self._power_limit_restored and _restore_attempt <= _MAX_RESTORE_ATTEMPTS:
                try:
                    inverter_limit = await self.api.get_max_power()
                    LOGGER.debug(
                        "[%s] Power limit check: inverter RAM=%sW, stored=%sW",
                        self._log_id, inverter_limit, self.current_max_power,
                    )
                    if inverter_limit is not None and abs(float(inverter_limit) - self.current_max_power) > 1:
                        await self.api.set_max_power(int(self.current_max_power))
                        LOGGER.debug(
                            "[%s] Power limit restored to %sW (RAM) after restart"
                            " (inverter had %sW).%s",
                            self._log_id, self.current_max_power, inverter_limit,
                            " Battery system – extended verification active." if _is_battery else "",
                        )
                        self._power_limit_verify_at = time.monotonic() + _VERIFY_INTERVAL
                        self._power_limit_verify_count = 0
                    else:
                        LOGGER.debug(
                            "[%s] Power limit OK after restart: inverter RAM=%sW, stored=%sW.",
                            self._log_id, inverter_limit, self.current_max_power,
                        )
                        self._power_limit_verify_at = time.monotonic() + _VERIFY_INTERVAL
                        self._power_limit_verify_count = 0
                    self._power_limit_restored = True
                except Exception as err:  # noqa: BLE001
                    if _restore_attempt < _MAX_RESTORE_ATTEMPTS:
                        LOGGER.warning(
                            "[%s] Could not restore power limit (attempt %d/%d): %s",
                            self._log_id, _restore_attempt, _MAX_RESTORE_ATTEMPTS, _fmt_err(err),
                        )
                    else:
                        LOGGER.warning(
                            "[%s] Power limit restore failed after %d attempts: %s. "
                            "Will retry on next inverter restart.",
                            self._log_id, _MAX_RESTORE_ATTEMPTS, _fmt_err(err),
                        )
                        self._power_limit_restored = True
            elif _verify_due:
                # Verification round: check if EZ1 still has the correct limit
                self._power_limit_verify_count += 1
                self._power_limit_verify_at = 0.0  # clear; may be rescheduled below
                try:
                    inverter_limit = await self.api.get_max_power()
                    if inverter_limit is not None and abs(float(inverter_limit) - self.current_max_power) > 1:
                        await self.api.set_max_power(int(self.current_max_power))
                        LOGGER.debug(
                            "[%s] Power limit re-applied to %sW (RAM) – EZ1 had reloaded "
                            "flash (%sW) (verification round %d/%d).",
                            self._log_id, self.current_max_power, inverter_limit,
                            self._power_limit_verify_count, _MAX_VERIFY_ROUNDS,
                        )
                    else:
                        LOGGER.debug(
                            "[%s] Power limit verification OK (round %d/%d): RAM=%sW.",
                            self._log_id, self._power_limit_verify_count, _MAX_VERIFY_ROUNDS,
                            inverter_limit,
                        )
                    # Schedule next round if budget remaining
                    if self._power_limit_verify_count < _MAX_VERIFY_ROUNDS:
                        self._power_limit_verify_at = time.monotonic() + _VERIFY_INTERVAL
                    else:
                        LOGGER.debug(
                            "[%s] Power limit verification complete after %d rounds.",
                            self._log_id, _MAX_VERIFY_ROUNDS,
                        )
                except Exception:  # noqa: BLE001
                    # Reschedule on transient error if budget remaining
                    if self._power_limit_verify_count < _MAX_VERIFY_ROUNDS:
                        self._power_limit_verify_at = time.monotonic() + _VERIFY_INTERVAL

        if not self.inverter_switch_on:
            _now_sw = time.monotonic()
            _switch_verify_due = (
                self._switch_restore_verify_at > 0
                and _now_sw >= self._switch_restore_verify_at
            )
            _switch_restore_attempt = self._stable_polls_after_error - _SWITCH_RESTORE_START_POLL + 1

            if (
                self._stable_polls_after_error >= _SWITCH_RESTORE_START_POLL
                and not self._switch_restore_done
                and _switch_restore_attempt <= _SWITCH_RESTORE_MAX_ATTEMPTS
            ):
                try:
                    resp = await self.api._request("getOnOff")
                    already_off = resp and resp.get("data", {}).get("status") == "1"
                    if already_off:
                        LOGGER.debug("[%s] Switch restore: EZ1 already OFF – skipping.", self._log_id)
                    else:
                        await self.api.set_device_power_status(0)
                        LOGGER.debug(
                            "[%s] Inverter switch restored to OFF after reconnect (attempt %d/%d).",
                            self._log_id, _switch_restore_attempt, _SWITCH_RESTORE_MAX_ATTEMPTS,
                        )
                    self._switch_restore_verify_at = _now_sw + _SWITCH_RESTORE_VERIFY_INTERVAL
                    self._switch_restore_done = True
                except Exception as err:  # noqa: BLE001
                    if _switch_restore_attempt < _SWITCH_RESTORE_MAX_ATTEMPTS:
                        LOGGER.warning(
                            "[%s] Could not restore inverter OFF state (attempt %d/%d): %s",
                            self._log_id, _switch_restore_attempt, _SWITCH_RESTORE_MAX_ATTEMPTS,
                            _fmt_err(err),
                        )
                    else:
                        LOGGER.warning(
                            "[%s] Could not restore inverter OFF state after %d attempts: %s. "
                            "Please toggle the switch manually.",
                            self._log_id, _SWITCH_RESTORE_MAX_ATTEMPTS, _fmt_err(err),
                        )
                        self._switch_restore_done = True

            elif _switch_verify_due:
                self._switch_restore_verify_at = 0.0
                try:
                    resp = await self.api._request("getOnOff")
                    if resp and resp.get("data", {}).get("status") == "0":
                        await self.api.set_device_power_status(0)
                        LOGGER.debug("[%s] Switch re-applied to OFF – EZ1 had reset to ON.", self._log_id)
                    else:
                        LOGGER.debug("[%s] Switch verification OK – EZ1 is OFF.", self._log_id)
                    self._switch_restore_verify_at = _now_sw + _SWITCH_RESTORE_VERIFY_INTERVAL
                except Exception:  # noqa: BLE001
                    self._switch_restore_verify_at = _now_sw + _SWITCH_RESTORE_VERIFY_INTERVAL

        output_data, needs_save = self._compensate_lifetime_energy(output_data)
        if needs_save:
            await self._save_state()
        elif self._poll_count % 10 == 0:
            # Periodically save last_out so it survives HA restarts
            # and prevents lifetime energy jumps after cold start
            await self._save_state()

        # Extended data polling – three-level control:
        #   CONF_DETAIL_POLL = False → never call getOutputDataDetail (max stability)
        #   CONF_DETAIL_POLL = True, CONF_SLOW_DETAIL_POLL = True  → every 60 s
        #   CONF_DETAIL_POLL = True, CONF_SLOW_DETAIL_POLL = False → every poll
        _detail_enabled = bool(self.config_entry.data.get(CONF_DETAIL_POLL, True))
        _slow_detail = bool(self.config_entry.data.get(CONF_SLOW_DETAIL_POLL, False))

        if not _detail_enabled:
            detail_data = None
        else:
            _now_detail = time.monotonic()
            _detail_due = (not _slow_detail) or (
                _now_detail - self._detail_last_ts >= _DETAIL_MIN_INTERVAL
            )
            if _detail_due:
                detail_data = await self._get_output_data_detail()
                if detail_data is not None:
                    self._detail_last_ts = _now_detail
                    self._fallback_detail = detail_data
            else:
                detail_data = None
                LOGGER.debug(
                    "[%s] getOutputDataDetail skipped (last call %.0fs ago,"
                    " interval %ss) – serving cached detail.",
                    self._log_id,
                    _now_detail - self._detail_last_ts,
                    int(_DETAIL_MIN_INTERVAL),
                )

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
