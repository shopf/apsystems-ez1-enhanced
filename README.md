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

> **Hinweis:** Falls die offizielle APsystems Integration bereits installiert ist, muss diese zuerst entfernt werden – beide verwenden denselben Domain-Namen `apsystems`.

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
| EZ1-D | – | ✅ Unterstützt (maxPower dynamisch) |

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