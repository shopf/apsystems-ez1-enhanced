# 🇩🇪 Deutsch

# APsystems EZ1 – Community Enhanced Integration

[![HACS Custom Repository](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.6%2B-blue.svg)](https://www.home-assistant.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Eine Community-gepflegte, verbesserte Version der offiziellen [APsystems Home Assistant Integration](https://www.home-assistant.io/integrations/apsystems/).

Diese Integration behebt mehrere Bugs der offiziellen Integration, verbessert die Firmware-Kompatibilität und fügt nützliche Verbesserungen und Sensoren hinzu. Die Kommunikation erfolgt ausschließlich über das **lokale Netzwerk** – keine Cloud erforderlich.

---

## ⚠️ Sicherheitshinweis – Firmware 1.12.2

Am 4. März 2026 hat die Jakkaru GmbH eine kritische Sicherheitslücke im EZ1-M veröffentlicht. Angreifer können über den APsystems MQTT-Cloud-Server beliebige Firmware auf den Wechselrichter aufspielen – ohne physischen Zugang. Betroffen sind ca. 100.000 Geräte weltweit.

**Firmware 1.12.2 schließt diese Lücke – ein Update wird dringend empfohlen.**

Nutzer im Local Mode sind weniger exponiert da keine aktive Cloud-Verbindung besteht, aber ein Update bleibt trotzdem empfehlenswert.

Weitere Informationen: [jakkaru.de](https://jakkaru.de/de/artikel/apsystems-remote-firmware-injection)

---

## Warum diese Integration?

Die offizielle APsystems Integration wurde mit HA 2024.6 eingeführt und seitdem kaum weiterentwickelt. Mehrere bekannte Bugs sind unbehoben, neuere Firmware-Versionen brechen die Integration komplett. Dieses Repository bietet eine stabile, community-gepflegte Alternative.

Alle Fixes sind mit Verweisen auf die jeweiligen GitHub Issues dokumentiert und können als PRs in das offizielle Repository eingereicht werden.

---

## Normales Inverter-Verhalten

Der EZ1-M schaltet sich **physikalisch vollständig ab** sobald die PV-Eingangsspannung unter den Mindestschwellenwert fällt – typischerweise bei Einbruch der Dunkelheit oder starker Bewölkung. Das bedeutet: der Inverter ist nicht im Standby, er ist komplett stromlos und verschwindet vollständig aus dem Netzwerk.

**Das ist normales und erwartetes Verhalten.** Diese Integration behandelt es korrekt:

- Beim Herunterfahren liefert der Inverter API-Fehler → gecachte Daten werden geliefert, alle Sensoren behalten ihre letzten Werte
- Nach dem vollständigen Abschalten ist der Inverter netzwerkseitig nicht mehr erreichbar → Cache bleibt aktiv
- `Wechselrichter Aktiv` wechselt auf `Ausgeschaltet` sobald der Inverter nicht mehr erreichbar ist
- Morgens startet der Inverter, verbindet sich mit dem WLAN und die Integration nimmt den normalen Betrieb automatisch wieder auf

Kein Benutzereingriff erforderlich.

---

## Behobene Bugs gegenüber der offiziellen Integration

### 🐛 Fix: Python 3 Syntaxfehler im Exception-Handling
**Betroffene Dateien:** `coordinator.py`, `number.py`
**Details:** Die originale Integration verwendet Python 2 Syntax:
```python
except ConnectionError, TimeoutError:   # SyntaxError in Python 3!
except TimeoutError, ClientConnectorError:  # SyntaxError in Python 3!
```
Dies führt beim Setup zu einem `SyntaxError` statt einem sauberen Fehler.

---

### 🐛 Fix: `KeyError` bei neueren Firmware-Versionen
**Betroffene Firmware:** `1.1.2_b`, `2.0.1_B` und neuer
**HA Issue:** [#136288](https://github.com/home-assistant/core/issues/136288)
**Details:** APsystems entfernte die Felder `maxPower` und `minPower` aus der `getDeviceInfo` API-Antwort in neueren Firmware-Versionen. Der direkte Zugriff `device_info.maxPower` führte zu einem `KeyError` der die Integration beim Start komplett abstürzen ließ. Behoben mit `getattr()` und sicheren Fallback-Werten.

---

### 🐛 Fix: Alle Sensoren werden nachts `unknown`
**Betroffene Versionen:** Alle
**HA Issue:** [#140891](https://github.com/home-assistant/core/issues/140891)
**Details:** Der Inverter gibt beim nächtlichen Abschalten einen Fehler zurück. Die offizielle Integration propagiert dies sofort als `UpdateFailed`, alle Entitäten werden `unavailable`. Behoben durch einen Cache-Mechanismus: bei Fehlern werden die zuletzt bekannten Werte geliefert. Sensoren bleiben bis zum nächsten Morgen stabil.

---

### 🐛 Fix: `output_fault_status` zeigt jeden Abend fälschlich ein Problem
**Betroffene Versionen:** Alle
**Details:** `not c.operating` mit `device_class=PROBLEM` triggert jeden Abend beim normalen Herunterfahren eine Problemwarnung. Ersetzt durch `inverter_active` mit `device_class=RUNNING` – semantisch korrekt: `In Betrieb` / `Ausgeschaltet`.

---

### 🐛 Fix: `Wechselrichter Aktiv` blieb nachts auf „In Betrieb"
**Details:** Der Status wurde aus dem Cache gelesen obwohl der Inverter physikalisch ausgeschaltet war. Behoben durch einen `inverter_reachable` Flag: `Wechselrichter Aktiv` zeigt `Ausgeschaltet` sobald der Inverter nicht mehr erreichbar ist.

---

### 🐛 Fix: `Leistungsbegrenzung` zeigt immer 800W
**Betroffene Versionen:** Alle
**Details:** Die offizielle Integration liest `maxPower` aus `getDeviceInfo()`, was auf vielen Firmware-Versionen keinen Wert liefert. Behoben durch direkten Aufruf des dedizierten `getMaxPower` Endpoints. Falls dieser beim Start fehlschlägt, wird er beim nächsten erfolgreichen Poll automatisch wiederholt.

---

### 🐛 Fix: EZ1-M Lifetime Energy Counter Overflow (Firmware-Bug Workaround)
**Betroffene Versionen:** Alle EZ1-M Geräte
**Details:** Ein bekannter Firmware-Bug setzt den internen Lifetime-Energie-Zähler (`te1`/`te2`) bei ca. **540 kWh** auf 0 zurück (Integer Overflow). Dies ist ein Inverter-Firmware-Problem – HA kann es nicht verhindern.

Diese Integration **erkennt den Reset automatisch** und kompensiert mit einem akkumulierten Offset. Die HA-Sensoren laufen nahtlos weiter ohne Unterbrechung oder Datenverlust. Ein `WARNING` wird mit den genauen Werten vor und nach dem Reset geloggt.

Ohne diesen Fix würde der Reset die HA-Statistikdatenbank für `TOTAL_INCREASING` Sensoren beschädigen (Energie-Dashboard).

---

### 🐛 Fix: `state is not strictly increasing` Warnungen
**Details:** Der Inverter liefert gelegentlich minimal kleinere Lifetime-Werte durch Gleitkomma-Rundung (z.B. `176.58319` → `176.58315`). Dies triggert HA-Warnungen für `TOTAL_INCREASING` Sensoren. Behoben durch Tracking des letzten ausgegebenen Werts – der Sensor-Wert kann nie kleiner werden als der vorherige.

---

### 🐛 Fix: „Heutige Erzeugung" Sensoren springen tagsüber zurück
**Betroffene Versionen:** Alle, besonders auffällig bei Speichersystemen (z.B. EZ1 hinter Marstek B2500) mit geringer, kontinuierlicher Leistung
**Details:** Nach einem Neustart des Wechselrichters oder einem Firmware-Bug kann `e1`/`e2` (heutige Erzeugung pro Eingang) zwischendurch auf einen falschen, zu niedrigen Wert zurücksetzen – nicht zuverlässig auf exakt 0, sondern auf einen beliebigen Zwischenwert (in der Praxis beobachtet: 0,01 kWh).

---

## Neue Features gegenüber der offiziellen Integration

### ✨ Acht neue Sensoren
DC-Spannung und DC-Strom pro PV-Eingang, Wechselrichter Temperatur, Netzfrequenz und Netzspannung als Diagnosesensoren verfügbar.
Die Firmware-Version (`devVer`) ist ebenfalls als Diagnosesensor sichtbar – hilfreich um Probleme mit bestimmten Firmware-Versionen zu korrelieren.

### ✨ Frei wählbarer Gerätename
Beim Setup kann ein eigener Gerätename vergeben werden (z.B. „Balkonkraftwerk Süd"). Dieser wird als Gerätename in HA und als Präfix für alle Entitätsnamen verwendet.

### ✨ Dynamischer Abfrageintervall
Beim Setup kann der Poll Intervall zwischen 12–60 Sekunden eingestellt werden.

### ✨ Umfassendes Logging
Alle relevanten Ereignisse werden mit sinnvollen Log-Leveln protokolliert. Sichtbar unter **Einstellungen → System → Protokolle**, nach `apsystems` filtern.

### ✨ Deutsche Übersetzungen
Alle Entitätsnamen sind auf Deutsch verfügbar.

### ✨ EZ1-D Unterstützung
Der EZ1-D (bis 1800W) wird unterstützt. Die Leistungsgrenze wird dynamisch vom Gerät gelesen – der 800W Fallback gilt nur wenn `getDeviceInfo()` keinen Wert liefert.

### ✨ Automatische Modellerkennung
Das Gerätemodell (EZ1-M, EZ1-SPE, EZ1-LV, EZ1-H, EZ1D-L, EZ1D, EZ1D-H) wird automatisch anhand der vom Gerät gemeldeten maximalen Leistung (`maxPower`) erkannt und im Gerätenamen angezeigt – kein manuelles Eintragen mehr nötig. Unbekannte Modelle werden einmalig geloggt mit der Bitte, ein Issue mit dem `maxPower`-Wert zu eröffnen, damit das Modell ergänzt werden kann.

### ✨ Lifetime-Energie-Offsets
Im Reconfigure-Dialog kann der Lifetime-Energie-Offset pro Eingang eingetragen werden und jederzeit nachträglich korrigiert werden – etwa wenn ein falscher Wert eingetragen wurde. Negative Werte reduzieren den angezeigten Zähler dauerhaft; ein entsprechender Warnhinweis wird im Formular angezeigt.

---

## Verfügbare Entitäten

| Entität | Beschreibung | Einheit |
|---------|-------------|---------|
| `sensor.{name}_gesamtleistung` | Gesamtleistung (kombiniert) | W |
| `sensor.{name}_leistung_eingang_1` | Leistung PV-Eingang 1 | W |
| `sensor.{name}_leistung_eingang_2` | Leistung PV-Eingang 2 | W |
| `sensor.{name}_energie_heute` | Energie heute (kombiniert) | kWh |
| `sensor.{name}_energie_heute_eingang_1` | Energie heute – Eingang 1 | kWh |
| `sensor.{name}_energie_heute_eingang_2` | Energie heute – Eingang 2 | kWh |
| `sensor.{name}_energie_gesamt` | Energie Gesamt (kombiniert) | kWh |
| `sensor.{name}_energie_gesamt_eingang_1` | Energie Gesamt – Eingang 1 | kWh |
| `sensor.{name}_energie_gesamt_eingang_2` | Energie Gesamt – Eingang 2 | kWh |
| `sensor.{name}_dc_spannung_p1` | DC-Spannung PV-Eingang 1 (Diagnose) | V |
| `sensor.{name}_dc_spannung_p2` | DC-Spannung PV-Eingang 2 (Diagnose) | V |
| `sensor.{name}_dc_strom_p1` | DC-Strom PV-Eingang 1 (Diagnose) | A |
| `sensor.{name}_dc_strom_p2` | DC-Strom PV-Eingang 2 (Diagnose) | A |
| `sensor.{name}_wechselrichter_temperatur` | Wechselrichter Temperatur (Diagnose) | °C |
| `sensor.{name}_netzfrequenz` | Netzfrequenz (Diagnose) | Hz |
| `sensor.{name}_netzspannung` | Netzspannung (Diagnose) | V |
| `binary_sensor.{name}_netzausfall` | Netzausfall-Alarm (Diagnose) | – |
| `binary_sensor.{name}_kurzschluss_eingang_1` | Kurzschluss Eingang 1 (Diagnose) | – |
| `binary_sensor.{name}_kurzschluss_eingang_2` | Kurzschluss Eingang 2 (Diagnose) | – |
| `sensor.{name}_firmware_version` | Firmware-Version (Diagnose) | – |
| `binary_sensor.{name}_wechselrichter_aktiv` | In Betrieb / Ausgeschaltet (Diagnose) | – |
| `number.{name}_leistungsbegrenzung` | Maximale Ausgangsleistung (30–800W / 30–1800W beim EZ1-D) | W |
| `switch.{name}_wechselrichter` | Wechselrichter Ein/Aus | – |

---

## Installation

### Via HACS (empfohlen)

1. HACS in Home Assistant öffnen
2. **Integrationen** auswählen
3. Drei-Punkte-Menü → **Benutzerdefinierte Repositories**
4. Repository-URL hinzufügen, Kategorie: **Integration**
5. Nach „APsystems" suchen und installieren
6. Home Assistant neu starten

### Manuell

1. Aktuelles Release-ZIP herunterladen
2. Ordner `custom_components/apsystems` in das HA-Konfigurationsverzeichnis kopieren:
   `<config>/custom_components/apsystems/`
3. Home Assistant neu starten
4. **Einstellungen → Geräte & Dienste → Integration hinzufügen** → „APsystems" suchen

---

## Migration von der offiziellen Integration

Diese Integration ersetzt die offizielle HA APsystems Integration automatisch – ein manuelles Löschen der offiziellen Integration ist **nicht notwendig**.

**So einfach geht es:**

1. Backup erstellen (**Einstellungen → System → Backups**)
2. Diese Integration via HACS als Custom Repository hinzufügen und installieren
3. Home Assistant neu starten
4. Integration über die UI einrichten (IP-Adresse, Port, Gerätename)

HA erkennt automatisch dass beide Integrationen denselben Domain-Namen `apsystems` verwenden und zeigt unsere als Ersatz an. **Entity-IDs, Verlauf und Statistiken bleiben vollständig erhalten** da die `unique_id` auf der Seriennummer des Inverters basiert.

> ℹ️ Die Migration von der **Sonnenladen Community Integration** (`apsystemsapi_local`) ist leider nicht nahtlos möglich da diese einen anderen Domain-Namen verwendet. In diesem Fall gehen Statistiken verloren – ein automatischer Migrationspfad ist für eine zukünftige Version geplant.

---

## Firmware Updates

APsystems veröffentlicht keine öffentliche Firmware-Datenbank. Updates sind ausschließlich über die AP EasyPower App verfügbar. Ab Version 1.9.2 werden Updates auch im Local Mode angeboten.

**Empfehlung:** Vor jedem Update in Community-Foren nach Erfahrungsberichten suchen:
- [photovoltaikforum.com](https://www.photovoltaikforum.com) – deutsche Community, sehr aktiv zu EZ1-Firmware-Themen
- [Home Assistant Community](https://community.home-assistant.io)

**Bekannte Probleme:**
- Firmware `1.9.2` ist für Probleme bekannt – u.a. fehlerhafte Lifetime-Energie-Berechnung
- Nach manchen Updates wird Version `1.0.0` angezeigt und weitere Updates sind nicht mehr möglich
- Ein Downgrade ist nicht offiziell unterstützt und sehr aufwändig

**Firmware `1.12.2`** schließt eine kritische Sicherheitslücke (Remote Firmware Injection) – ein Update wird dringend empfohlen. Siehe Sicherheitshinweis oben.

---

## Voraussetzungen

- Home Assistant 2024.6 oder neuer
- APsystems EZ1-M oder EZ1-D mit aktiviertem Local Mode
- `apsystems-ez1==2.7.0` (wird automatisch installiert)

### Local Mode aktivieren

1. Mit der AP EasyPower App über „Direkte Verbindung" mit dem Inverter verbinden
2. **Einstellungen → Local Mode**
3. Local Mode aktivieren und auf „Continuous" setzen
4. Die angezeigte IP-Adresse notieren – im Router als statische IP eintragen empfohlen

---

## Kompatibilität

| Modell | Firmware | Status |
|--------|----------|--------|
| EZ1-M | 1.6.x | ✅ Sollte funktionieren |
| EZ1-M | 1.7.0 | ✅ Getestet |
| EZ1-M | 1.7.5 | ✅ Getestet |
| EZ1-M | 1.9.0 | ⚠️ Lifetime-Werte fehlerhaft (Firmware-Bug) – Workaround aktiv |
| EZ1-M | 1.10.2 | ✅ Getestet – Firmware-Bug behoben |
| EZ1-M | 1.12.2 | ✅ Getestet – Sicherheitslücke geschlossen, empfohlen |
| EZ1-M | 1.1.2_b | ✅ Behoben (war kaputt in offizieller) |
| EZ1-M | 2.0.1_B | ✅ Behoben (war kaputt in offizieller) |
| EZ1-SPE | – | ✅ Unterstützt (automatische Modellerkennung) |
| EZ1-LV | – | ✅ Unterstützt (automatische Modellerkennung) |
| EZ1-H | – | ✅ Unterstützt (automatische Modellerkennung) |
| EZ1-D | – | ✅ Unterstützt (maxPower dynamisch) |
| EZ1D-L / EZ1D-H | – | ✅ Unterstützt (automatische Modellerkennung) |

---

## Troubleshooting

### Mehr als 24 Stunden Aktivität einsehen

Das „Aktivität"-Fenster in der Geräteübersicht zeigt standardmäßig nur die letzten 24 Stunden. Für längere Zeiträume:

- **Systemprotokoll** – Einstellungen → System → Protokolle, nach `apsystems` filtern. Alle Log-Einträge ohne Zeitbegrenzung.
- **Entitäts-Verlauf** – Auf eine einzelne Entität klicken → „Verlauf" Tab. Zeigt den Zustandsverlauf über mehrere Tage.
- **Dashboard-Karte** – Eine „Aktivität"-Karte im Dashboard hinzufügen und auf die gewünschten Entitäten filtern. Zeitraum frei wählbar.

### Integration zeigt „Einrichtungsfehler"

Wenn der Inverter beim HA-Start physikalisch ausgeschaltet ist (z.B. nachts), kann die Integration beim ersten Setup-Versuch scheitern. HA wiederholt den Versuch automatisch im Hintergrund. Sobald der Inverter morgens hochfährt, wird die Integration automatisch verfügbar.

### Issue melden

Bei einem Bug bitte folgende Informationen mitschicken:
- Firmware-Version (sichtbar als `Firmware Version` Diagnosesensor)
- HA-Log unter **Einstellungen → System → Protokolle**, gefiltert nach `apsystems`
- Beschreibung was erwartet wurde und was stattdessen passierte

---

## Bekannte Inverter-Bugs & Workarounds

### Lifetime Energy Counter Reset bei ~540 kWh
Bestätigter Firmware-Bug im EZ1-M. Der interne Zähler läuft bei ca. 540 kWh über und springt auf 0. APsystems hat den Bug bestätigt, ein Fix ist in zukünftigen Firmware-Versionen angekündigt.

**Diese Integration erkennt und kompensiert den Reset automatisch** – kein Benutzereingriff erforderlich.

---

## Beziehung zur offiziellen Integration

Diese Integration ist nicht mit APsystems oder Sonnenladen GmbH verbunden. Ziel ist es, die Fixes langfristig als Pull Requests in die offizielle HA-Integration einzubringen. Dieses Repository dient als Staging-Umgebung bis dahin.

---

## Community & Support

| | |
|---|---|
| 💬 **Fragen & Ideen** | [GitHub Discussions](https://github.com/shopf/apsystems-ez1-enhanced/discussions) |
| 🐛 **Fehlermeldungen** | [GitHub Issues](https://github.com/shopf/apsystems-ez1-enhanced/issues) |

---

## Lizenz

MIT License – siehe [LICENSE](LICENSE)
---

# 🇬🇧 English

# APsystems EZ1 – Community Enhanced Integration

[![HACS Custom Repository](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.6%2B-blue.svg)](https://www.home-assistant.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A community-maintained, improved version of the official [APsystems Home Assistant Integration](https://www.home-assistant.io/integrations/apsystems/).

This integration fixes several bugs in the official integration, improves firmware compatibility, and adds useful enhancements and sensors. Communication happens exclusively over the **local network** – no cloud required.

---

## ⚠️ Security Notice – Firmware 1.12.2

On March 4, 2026, Jakkaru GmbH disclosed a critical security vulnerability in the EZ1-M. Attackers could push arbitrary firmware onto the inverter via the APsystems MQTT cloud server – without physical access. Roughly 100,000 devices worldwide are affected.

**Firmware 1.12.2 closes this gap – an update is strongly recommended.**

Users running in Local Mode are less exposed since there is no active cloud connection, but an update is still recommended.

More information: [jakkaru.de](https://jakkaru.de/de/artikel/apsystems-remote-firmware-injection)

---

## Why this integration?

The official APsystems integration was introduced with HA 2024.6 and has barely been developed since. Several known bugs remain unfixed, and newer firmware versions break the integration entirely. This repository offers a stable, community-maintained alternative.

All fixes are documented with references to the respective GitHub issues and can be submitted as PRs to the official repository.

---

## Normal Inverter Behavior

The EZ1-M **physically shuts down completely** once the PV input voltage drops below the minimum threshold – typically at dusk or under heavy cloud cover. This means: the inverter is not in standby, it is completely powerless and disappears from the network entirely.

**This is normal, expected behavior.** This integration handles it correctly:

- On shutdown, the inverter returns API errors → cached data is served, all sensors retain their last values
- After fully powering off, the inverter is no longer reachable on the network → cache stays active
- `Inverter Active` switches to `Off` as soon as the inverter is no longer reachable
- In the morning the inverter starts up, connects to WiFi, and the integration automatically resumes normal operation

No user intervention required.

---

## Bugs Fixed Compared to the Official Integration

### 🐛 Fix: Python 3 syntax error in exception handling
**Affected files:** `coordinator.py`, `number.py`
**Details:** The original integration uses Python 2 syntax:
```python
except ConnectionError, TimeoutError:   # SyntaxError in Python 3!
except TimeoutError, ClientConnectorError:  # SyntaxError in Python 3!
```
This causes a `SyntaxError` during setup instead of a clean error.

---

### 🐛 Fix: `KeyError` on newer firmware versions
**Affected firmware:** `1.1.2_b`, `2.0.1_B` and newer
**HA Issue:** [#136288](https://github.com/home-assistant/core/issues/136288)
**Details:** APsystems removed the `maxPower` and `minPower` fields from the `getDeviceInfo` API response in newer firmware versions. Direct access via `device_info.maxPower` caused a `KeyError` that crashed the integration entirely on startup. Fixed using `getattr()` with safe fallback values.

---

### 🐛 Fix: All sensors become `unknown` at night
**Affected versions:** All
**HA Issue:** [#140891](https://github.com/home-assistant/core/issues/140891)
**Details:** The inverter returns an error during nightly shutdown. The official integration immediately propagates this as `UpdateFailed`, making all entities `unavailable`. Fixed with a caching mechanism: on errors, the last known values are served. Sensors remain stable until the next morning.

---

### 🐛 Fix: `output_fault_status` falsely reports a problem every evening
**Affected versions:** All
**Details:** `not c.operating` with `device_class=PROBLEM` triggers a problem warning every evening during normal shutdown. Replaced with `inverter_active` using `device_class=RUNNING` – semantically correct: `Running` / `Off`.

---

### 🐛 Fix: `Inverter Active` stayed `Running` at night
**Details:** The status was read from the cache even though the inverter was physically powered off. Fixed with an `inverter_reachable` flag: `Inverter Active` shows `Off` as soon as the inverter is no longer reachable.

---

### 🐛 Fix: `Power Limit` always shows 800W
**Affected versions:** All
**Details:** The official integration reads `maxPower` from `getDeviceInfo()`, which returns no value on many firmware versions. Fixed by directly calling the dedicated `getMaxPower` endpoint. If this fails at startup, it is automatically retried on the next successful poll.

---

### 🐛 Fix: EZ1-M lifetime energy counter overflow (firmware bug workaround)
**Affected versions:** All EZ1-M devices
**Details:** A known firmware bug resets the internal lifetime energy counter (`te1`/`te2`) to 0 at around **540 kWh** (integer overflow). This is an inverter firmware issue – HA cannot prevent it.

This integration **automatically detects the reset** and compensates with an accumulated offset. The HA sensors continue seamlessly without interruption or data loss. A `WARNING` is logged with the exact values before and after the reset.

Without this fix, the reset would corrupt the HA statistics database for `TOTAL_INCREASING` sensors (Energy Dashboard).

---

### 🐛 Fix: `state is not strictly increasing` warnings
**Details:** The inverter occasionally returns marginally smaller lifetime values due to floating-point rounding (e.g. `176.58319` → `176.58315`). This triggers HA warnings for `TOTAL_INCREASING` sensors. Fixed by tracking the last output value – the sensor value can never decrease below the previous one.

---

### 🐛 Fix: "Today's Production" sensors jump backward during the day
**Affected versions:** All, especially noticeable on battery-backed systems (e.g. EZ1 behind a Marstek B2500) with low, continuous output
**Details:** After an inverter restart or a firmware bug can reset `e1`/`e2` (today's production per input) to an incorrect, lower value mid-day – not reliably to exactly `0.0`, but to an arbitrary intermediate value (observed in the field: `0.01 kWh`).

---

## New Features Compared to the Official Integration

### ✨ Eight new sensors
DC voltage and DC current per PV input, inverter temperature, grid frequency and grid voltage available as diagnostic sensors.
The firmware version (`devVer`) is also visible as a diagnostic sensor – useful for correlating issues with specific firmware versions.

### ✨ Freely chosen device name
A custom device name can be assigned during setup (e.g. "Balcony Power Plant South"). This is used as the device name in HA and as a prefix for all entity names.

### ✨ Dynamic polling interval
The polling interval can be configured between 12–60 seconds during setup.

### ✨ Comprehensive logging
All relevant events are logged with sensible log levels. Visible under **Settings → System → Logs**, filter by `apsystems`.

### ✨ German translations
All entity names are available in German.

### ✨ EZ1-D support
The EZ1-D (up to 1800W) is supported. The power limit is read dynamically from the device – the 800W fallback only applies when `getDeviceInfo()` returns no value.

### ✨ Automatic model detection
The device model (EZ1-M, EZ1-SPE, EZ1-LV, EZ1-H, EZ1D-L, EZ1D, EZ1D-H) is automatically detected based on the maximum power (`maxPower`) reported by the device, and shown in the device name – no manual entry needed. Unknown models are logged once, asking the user to open an issue with the `maxPower` value so the model can be added.

### ✨ Lifetime energy offsets
The lifetime energy offset per input can be entered in the Reconfigure dialog and corrected afterwards at any time – for example if an incorrect value was entered. Negative values permanently reduce the displayed counter; a corresponding warning is shown in the form.

---

## Available Entities

| Entity | Description | Unit |
|---------|-------------|---------|
| `sensor.{name}_gesamtleistung` | Total power (combined) | W |
| `sensor.{name}_leistung_eingang_1` | Power PV input 1 | W |
| `sensor.{name}_leistung_eingang_2` | Power PV input 2 | W |
| `sensor.{name}_energie_heute` | Energy today (combined) | kWh |
| `sensor.{name}_energie_heute_eingang_1` | Energy today – input 1 | kWh |
| `sensor.{name}_energie_heute_eingang_2` | Energy today – input 2 | kWh |
| `sensor.{name}_energie_gesamt` | Total energy (combined) | kWh |
| `sensor.{name}_energie_gesamt_eingang_1` | Total energy – input 1 | kWh |
| `sensor.{name}_energie_gesamt_eingang_2` | Total energy – input 2 | kWh |
| `sensor.{name}_dc_spannung_p1` | DC voltage PV input 1 (diagnostic) | V |
| `sensor.{name}_dc_spannung_p2` | DC voltage PV input 2 (diagnostic) | V |
| `sensor.{name}_dc_strom_p1` | DC current PV input 1 (diagnostic) | A |
| `sensor.{name}_dc_strom_p2` | DC current PV input 2 (diagnostic) | A |
| `sensor.{name}_wechselrichter_temperatur` | Inverter temperature (diagnostic) | °C |
| `sensor.{name}_netzfrequenz` | Grid frequency (diagnostic) | Hz |
| `sensor.{name}_netzspannung` | Grid voltage (diagnostic) | V |
| `binary_sensor.{name}_netzausfall` | Grid outage alarm (diagnostic) | – |
| `binary_sensor.{name}_kurzschluss_eingang_1` | Short circuit input 1 (diagnostic) | – |
| `binary_sensor.{name}_kurzschluss_eingang_2` | Short circuit input 2 (diagnostic) | – |
| `sensor.{name}_firmware_version` | Firmware version (diagnostic) | – |
| `binary_sensor.{name}_wechselrichter_aktiv` | Running / Off (diagnostic) | – |
| `number.{name}_leistungsbegrenzung` | Maximum output power (30–800W / 30–1800W on EZ1-D) | W |
| `switch.{name}_wechselrichter` | Inverter on/off | – |

---

## Installation

### Via HACS (recommended)

1. Open HACS in Home Assistant
2. Select **Integrations**
3. Three-dot menu → **Custom repositories**
4. Add repository URL, category: **Integration**
5. Search for "APsystems" and install
6. Restart Home Assistant

### Manual

1. Download the current release ZIP
2. Copy the `custom_components/apsystems` folder into your HA config directory:
   `<config>/custom_components/apsystems/`
3. Restart Home Assistant
4. **Settings → Devices & Services → Add Integration** → search for "APsystems"

---

## Migration from the Official Integration

This integration automatically replaces the official HA APsystems integration – manually deleting the official integration is **not necessary**.

**It's this simple:**

1. Create a backup (**Settings → System → Backups**)
2. Add this integration via HACS as a custom repository and install it
3. Restart Home Assistant
4. Set up the integration via the UI (IP address, port, device name)

HA automatically detects that both integrations use the same domain name `apsystems` and shows ours as the replacement. **Entity IDs, history, and statistics are fully preserved** since the `unique_id` is based on the inverter's serial number.

> ℹ️ Migration from the **Sonnenladen Community Integration** (`apsystemsapi_local`) is unfortunately not seamless since it uses a different domain name. In this case, statistics are lost – an automatic migration path is planned for a future version.

---

## Firmware Updates

APsystems does not publish a public firmware database. Updates are only available via the AP EasyPower app. As of version 1.9.2, updates are also offered in Local Mode.

**Recommendation:** Before any update, check community forums for experience reports:
- [photovoltaikforum.com](https://www.photovoltaikforum.com) – German community, very active on EZ1 firmware topics
- [Home Assistant Community](https://community.home-assistant.io)

**Known issues:**
- Firmware `1.9.2` is known to cause problems – including incorrect lifetime energy calculation
- After some updates, version `1.0.0` is shown and further updates are no longer possible
- A downgrade is not officially supported and very cumbersome

**Firmware `1.12.2`** closes a critical security vulnerability (remote firmware injection) – an update is strongly recommended. See security notice above.

---

## Requirements

- Home Assistant 2024.6 or newer
- APsystems EZ1-M or EZ1-D with Local Mode enabled
- `apsystems-ez1==2.7.0` (installed automatically)

### Enabling Local Mode

1. Connect to the inverter via the AP EasyPower app using "Direct Connection"
2. **Settings → Local Mode**
3. Enable Local Mode and set it to "Continuous"
4. Note the displayed IP address – setting a static IP in your router is recommended

---

## Compatibility

| Model | Firmware | Status |
|--------|----------|--------|
| EZ1-M | 1.6.x | ✅ Should work |
| EZ1-M | 1.7.0 | ✅ Tested |
| EZ1-M | 1.7.5 | ✅ Tested |
| EZ1-M | 1.9.0 | ⚠️ Lifetime values incorrect (firmware bug) – workaround active |
| EZ1-M | 1.10.2 | ✅ Tested – firmware bug fixed |
| EZ1-M | 1.12.2 | ✅ Tested – security vulnerability closed, recommended |
| EZ1-M | 1.1.2_b | ✅ Fixed (was broken in official integration) |
| EZ1-M | 2.0.1_B | ✅ Fixed (was broken in official integration) |
| EZ1-SPE | – | ✅ Supported (automatic model detection) |
| EZ1-LV | – | ✅ Supported (automatic model detection) |
| EZ1-H | – | ✅ Supported (automatic model detection) |
| EZ1-D | – | ✅ Supported (dynamic maxPower) |
| EZ1D-L / EZ1D-H | – | ✅ Supported (automatic model detection) |

---

## Troubleshooting

### Viewing more than 24 hours of activity

The "Activity" window in the device overview shows only the last 24 hours by default. For longer periods:

- **System log** – Settings → System → Logs, filter by `apsystems`. All log entries without time limit.
- **Entity history** – Click on a single entity → "History" tab. Shows the state history over multiple days.
- **Dashboard card** – Add an "Activity" card to a dashboard and filter by the desired entities. Time range freely selectable.

### Integration shows "Setup error"

If the inverter is physically powered off when HA starts (e.g. at night), the integration may fail on the first setup attempt. HA automatically retries in the background. As soon as the inverter powers up in the morning, the integration becomes available automatically.

### Reporting an issue

When reporting a bug, please include:
- Firmware version (visible as the `Firmware Version` diagnostic sensor)
- HA log from **Settings → System → Logs**, filtered by `apsystems`
- Description of what was expected and what happened instead

---

## Known Inverter Bugs & Workarounds

### Lifetime energy counter reset at ~540 kWh
Confirmed firmware bug in the EZ1-M. The internal counter overflows at approximately 540 kWh and resets to 0. APsystems has confirmed the bug; a fix is announced for future firmware versions.

**This integration automatically detects and compensates for the reset** – no user intervention required.

---

## Relationship to the Official Integration

This integration is not affiliated with APsystems or Sonnenladen GmbH. The goal is to eventually contribute these fixes as pull requests to the official HA integration. This repository serves as a staging ground until then.

---

## Community & Support

| | |
|---|---|
| 💬 **Questions & Ideas** | [GitHub Discussions](https://github.com/shopf/apsystems-ez1-enhanced/discussions) |
| 🐛 **Bug Reports** | [GitHub Issues](https://github.com/shopf/apsystems-ez1-enhanced/issues) |

---

## License

MIT License – see [LICENSE](LICENSE)
