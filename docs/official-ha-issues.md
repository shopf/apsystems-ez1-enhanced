# Issue & PR Texte für das offizielle HA-Repository
# Einreichen unter: https://github.com/home-assistant/core/issues/new

===============================================================================
ISSUE 1 – Python Syntax Bug
===============================================================================

Title: [apsystems] SyntaxError in coordinator.py exception handling

**Description:**

`coordinator.py` in the APsystems integration contains a Python 2-style
exception syntax that is a `SyntaxError` in Python 3:

```python
# Line in _async_setup():
except ConnectionError, TimeoutError:
```

In Python 3 this should be:

```python
except (ConnectionError, TimeoutError) as err:
```

As written, a `TimeoutError` during the initial setup call to `get_device_info()`
will not be caught and will raise an unhandled exception instead of a clean
`UpdateFailed`, causing the config entry to fail without a user-friendly error.

**Fix:**
```python
async def _async_setup(self) -> None:
    try:
        device_info = await self.api.get_device_info()
    except (ConnectionError, TimeoutError) as err:
        raise UpdateFailed("Could not connect to inverter during setup") from err
```

**Affected versions:** All


===============================================================================
ISSUE 2 – KeyError on newer firmware
===============================================================================

Title: [apsystems] KeyError crash on firmware 1.1.2_b and 2.0.1_B due to missing maxPower field

**Description:**

Newer APsystems EZ1-M firmware versions (confirmed: `1.1.2_b`, `2.0.1_B`) have
changed the response structure of the `getDeviceInfo` API endpoint. The
`maxPower` and/or `minPower` fields are missing or renamed in the response.

The current `coordinator.py` accesses these fields directly:

```python
self.api.max_power = device_info.maxPower
self.api.min_power = device_info.minPower
```

This raises a `KeyError` / `AttributeError` that crashes the entire integration
on startup. Users with affected firmware cannot use the integration at all.

**Fix:** Use `getattr()` with safe fallback values:

```python
self.api.max_power = getattr(device_info, "maxPower", 800)
self.api.min_power = getattr(device_info, "minPower", 30)
```

**Related issues:** #136288


===============================================================================
ISSUE 3 – Sensors go unknown at night
===============================================================================

Title: [apsystems] All sensors become unavailable/unknown during nightly inverter shutdown

**Description:**

The APsystems EZ1-M shuts down after sunset and returns an error response
to all API calls. The current coordinator handles this by raising `UpdateFailed`,
which marks all entities as `unavailable` or `unknown`.

This has two user-visible problems:

1. Energy accumulation sensors (`today_production`, `lifetime_production`) drop
   to `unknown` every night, causing gaps and incorrect readings in the
   Energy Dashboard.
2. Automations that depend on these sensors break every evening.

The inverter being in night/standby mode is **not** an error condition – it is
expected daily behavior.

**Fix:** Cache the last known good data and return it during standby:

```python
async def _async_update_data(self) -> ApSystemsSensorData:
    try:
        output_data = await self.api.get_output_data()
        alarm_info = await self.api.get_alarm_info()
    except InverterReturnedError:
        if self._last_good_data is not None:
            return self._last_good_data  # serve cached data during standby
        raise UpdateFailed(...) from None
    result = ApSystemsSensorData(output_data=output_data, alarm_info=alarm_info)
    self._last_good_data = result
    return result
```

**Related issues:** #140891


===============================================================================
ISSUE 4 – output_fault_status fires every night
===============================================================================

Title: [apsystems] output_fault_status binary sensor incorrectly shows PROBLEM every evening

**Description:**

The `output_fault_status` binary sensor is defined as:

```python
is_on=lambda c: not c.operating,
device_class=BinarySensorDeviceClass.PROBLEM,
```

This means the sensor shows "Problem detected" every time the inverter
legitimately shuts down at dusk – which is normal daily behavior, not a fault.

Users receive false problem alerts every evening, leading to notification fatigue
and confusion.

**Fix:** Change the sensor semantics to reflect actual inverter state:

```python
ApsystemsLocalApiBinarySensorDescription(
    key="inverter_active",
    translation_key="inverter_active",
    device_class=BinarySensorDeviceClass.RUNNING,
    entity_category=EntityCategory.DIAGNOSTIC,
    is_on=lambda c: c.operating,  # True = running, False = standby
),
```

Note: This is a breaking change for users who use `output_fault_status` in
automations. A migration note in the release changelog is recommended.
