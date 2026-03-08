# Changelog

## [1.1.0] – 2026

### Fixed

#### `coordinator.py`
- **Python 3 Syntaxfehler** – `except ConnectionError, TimeoutError` ist Python 2 Syntax und führt in Python 3 zu einem `SyntaxError`. Korrigiert zu `except (ConnectionError, TimeoutError)`.
- **`KeyError` bei neueren Firmware-Versionen** – `device_info.maxPower` und `device_info.minPower` fehlen bei Firmware `1.1.2_b` und `2.0.1_B` komplett. Der direkte Zugriff (`device_info.maxPower`) führte zum Absturz der gesamten Integration beim Start. Abgesichert mit `getattr()` und sicheren Fallback-Werten (30W / 800W).
- **Alle Sensoren werden nachts `unknown`** – bei nightly shutdown wirft der Inverter einen `InverterReturnedError`, die offizielle Integration propagiert das sofort als `UpdateFailed` und markiert alle Entitäten als `unavailable`. Behoben durch Cache-Mechanismus (`_last_good_data`): bei Fehler werden die zuletzt bekannten Werte geliefert.
- **Fehlermeldungen ohne Fehlertext** – Python Built-in Exceptions wie `TimeoutError` haben keinen Message-String, was zu `Error: TimeoutError:` mit leerem Suffix führte. Behoben durch `_fmt_err()` Hilfsfunktion.
- **Startup-Fehler wenn Inverter beim HA-Start noch offline** – keine eigene Retry-Logik mehr, HA's eingebauter Retry-Mechanismus wird korrekt genutzt.

#### `number.py`
- **`Leistungsbegrenzung` zeigt immer 800W** – die offizielle Integration liest `maxPower` aus `get_device_info()`, was auf vielen Firmware-Versionen keinen Wert liefert und auf den Fallback 800W zurückfällt. Behoben durch direkten Aufruf des dedizierten `get_max_power()` Endpoints.
- **Python 3 Syntaxfehler** – `except TimeoutError, ClientConnectorError` ist Python 2 Syntax. Korrigiert.
- **`maxPower` nicht verfügbar wenn Inverter beim Setup noch nicht bereit** – falls `get_max_power()` beim Setup fehlschlägt, wird der Wert beim nächsten erfolgreichen Poll automatisch nachgeholt.
- **Keine Validierung beim Setzen** – `async_set_native_value()` fing keinen `ValueError` von der Library ab. Jetzt mit `HomeAssistantError` und aussagekräftiger Fehlermeldung.

#### `binary_sensor.py`
- **`output_fault_status` zeigt jeden Abend fälschlich ein Problem** – `not c.operating` mit `device_class=PROBLEM` triggert jeden Abend beim normalen Herunterfahren des Inverters eine Problemwarnung. Ersetzt durch `inverter_active` mit `device_class=RUNNING` und korrekter Semantik: `In Betrieb` / `Ausgeschaltet`.
- **`inverter_active` blieb nachts auf „In Betrieb"** – der Status wurde aus dem Cache gelesen, obwohl der Inverter physikalisch ausgeschaltet war. Behoben durch `inverter_reachable` Flag im Coordinator: `inverter_active` zeigt `Ausgeschaltet` sobald der Inverter nicht mehr erreichbar ist.

#### `sensor.py`
- **`native_value` abstürzen wenn `coordinator.data` noch `None`** – direkte Attribute-Zugriff auf `coordinator.data.output_data` ohne None-Check. Abgesichert.
- **`state is not strictly increasing` Warnungen** – der Inverter liefert gelegentlich minimal kleinere Lifetime-Werte durch Gleitkomma-Rundung (z.B. `176.58319` → `176.58315`). Behoben durch Tracking des letzten ausgegebenen Werts (`_te1_last_out`) – der Wert kann nie kleiner werden als der vorherige.

### Added

#### `coordinator.py`
- **Umfassendes Logging** mit sinnvollen Log-Leveln (`ERROR` / `WARNING` / `INFO` / `DEBUG`) für einfachere Fehlersuche. Sichtbar unter Einstellungen → System → Protokolle, Filter: `apsystems`.
- **Firmware-Version** wird beim Setup geloggt (`INFO`).
- **Consecutive-Error-Zähler** – beim ersten Fehler `WARNING`, danach `DEBUG`, nach 10 aufeinanderfolgenden Fehlern erneut `WARNING`.
- **Inverter back online** – `INFO`-Meldung wenn der Inverter nach Fehlern wieder erreichbar ist.
- **EZ1-M Lifetime Energy Counter Overflow Kompensation** – bekannter Firmware-Bug: bei ~540 kWh setzt der interne Zähler auf 0 zurück (Integer Overflow). Die Integration erkennt den Reset automatisch und kompensiert mit einem akkumulierten Offset, sodass HA einen kontinuierlich steigenden Wert sieht. Ein `WARNING` wird mit exakten Werten geloggt.

#### `sensor.py`
- **Firmware-Version als Diagnosesensor** – `devVer` wurde im Coordinator gespeichert aber nie in der UI angezeigt. Jetzt als `EntityCategory.DIAGNOSTIC` Sensor sichtbar.
- **`today_production` Sensoren** (`e1`, `e2`) – getrennte Sensoren pro PV-Eingang.

#### `number.py`
- **Hardcoded Konstanten** `HARDWARE_MIN_POWER = 30` und `HARDWARE_MAX_POWER = 800` mit Kommentar zum EZ1-D (bis 1800W).
- **`_attr_native_step = 1.0`** – nur ganzzahlige Watt-Werte, vermeidet `30.0` Anzeige in der HA-UI. Fehlermeldung zeigt `30 - 800` statt `30.0 - 800.0`.

#### `__init__.py`
- **Custom device name** – frei wählbarer Gerätename beim Setup (z.B. „Balkonkraftwerk").

### Changed

#### `coordinator.py`
- Polling-Intervall bleibt bei 12 Sekunden (übernommen aus offizieller Integration, in der Praxis bewährt).
- `_async_setup()` mit vollständiger Fehlerbehandlung und Logging statt stiller Fehler.

#### `binary_sensor.py`
- `output_fault_status` (PROBLEM, `not c.operating`) → `inverter_active` (RUNNING, `c.operating`).

---

## [1.0.0] – Baseline

- Basierend auf der offiziellen HA APsystems Integration (HA 2024.6)
