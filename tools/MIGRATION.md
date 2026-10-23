# APsystems Migration: Sonnenladen APsystems-EZ1-API-HomeAssistant → shopf APsystems EZ1 – Community Enhanced Integration

Dieses Skript migriert Home Assistant Statistiken und Entitäts-Verläufe von der
**Sonnenladen GmbH Integration** (`apsystemsapi_local`) zur
**shopf Community Integration** (`apsystems`) – ohne Datenverlust.

Statt fester Annahmen über Entity-Namen erkennt das Skript deine alten Sensoren an ihrem Muster in der Datenbank und berechnet die neuen shopf-Namen direkt aus den Übersetzungsdateien deiner installierten shopf-Version. Das macht es robuster gegenüber unterschiedlichen Sonnenladen-Versionen und Sprachen. Lies unbedingt den Abschnitt **Grenzen dieses Skripts** unten.

---

## Was wird migriert?

- ✅ Alle Energiestatistiken (Energie-Dashboard bleibt vollständig erhalten)
- ✅ Entitäts-Verlauf (Zustände, Graphen)
- ✅ Lifetime-Energy- und Leistungswerte
- ❌ Konfiguration (muss neu eingerichtet werden)
- ❌ Automationen, Dashboards, Templates mit den alten Entity-IDs (siehe unten)

---

## Voraussetzungen

**Terminal & SSH App/Add-on**, mit Python 3. Home Assistant OS bringt kein Python mit, es muss im Add-on nachinstalliert werden:

Add-on-Konfiguration (YAML-Ansicht) → folgende Zeile ergänzen:

```yaml
apks:
  - python3
```

Add-on danach neu starten.

**Samba Share** (empfohlen), um Skript und Log-Datei bequem vom PC aus zu kopieren bzw. zu lesen.

> ℹ️ Beide Add-ons laufen auch nach `ha core stop` weiter. Das **Web-Terminal in der HA-Oberfläche** dagegen nicht – die Oberfläche wird vom Core ausgeliefert und ist während des Core-Stopps nicht erreichbar. Für eine Migration, die HA selbst stoppt (`--auto`, siehe unten), ist das kein Problem: Du startest das Skript, während HA noch läuft, und es macht den Rest allein weiter, auch wenn die Verbindung währenddessen abbricht.

---

## Schritt-für-Schritt Anleitung

### Schritt 1: HA-Backup erstellen und extern sichern

Einstellungen → System → Backups → Backup erstellen, danach herunterladen oder per Samba (`\\<HA-IP>\backup\`) auf einen externen PC kopieren. Ein Backup, das nur auf demselben System liegt, hilft nicht, wenn das System selbst betroffen ist.

### Schritt 2: shopf herunterladen, aber noch NICHT einrichten

Das Skript liest die Ziel-Entity-Namen direkt aus den Übersetzungsdateien der installierten shopf-Version. shopf muss deshalb bereits im `custom_components`-Ordner liegen, bevor du analysierst oder migrierst:

1. HACS → Integrationen → Drei-Punkte-Menü → Benutzerdefinierte Repositories
2. URL: `https://github.com/shopf/apsystems-ez1-enhanced`, Kategorie: Integration
3. "APsystems (Community Enhanced)" installieren
4. Home Assistant **neu starten**, wenn HACS es verlangt
5. **Noch nicht** über Geräte & Dienste einrichten – erst nach der Migration (Schritt 6)

### Schritt 3: Alte Integration entfernen

Einstellungen → Geräte & Dienste → "APsystems Local API" (Sonnenladen) → Drei-Punkte-Menü → Löschen. Der Verlauf in der Datenbank bleibt davon unberührt, nur die Live-Verbindung wird beendet.

> Falls du das Gerät zuvor **umbenannt** hast (über das Bleistift-Symbol bei einer Entität): Lies vorher den Abschnitt "Wenn du Entity-IDs manuell umbenannt hast" weiter unten.

### Schritt 4: Skript kopieren und analysieren

`migrate_apsystems.py` nach `/config` kopieren (Samba oder `scp`), dann:

```bash
python3 /config/migrate_apsystems.py --analyze
```

Das Skript sucht die Datenbank automatisch (`/homeassistant/home-assistant_v2.db`); bei Bedarf den Pfad als Argument anhängen. Es zeigt:

- **HA-Sprache** und **shopf-Version**, aus denen die Ziel-Namen berechnet werden
- pro erkanntem Gerät: alle gefundenen Sonnenladen-Entities mit der jeweils geplanten neuen ID
- übersprungene Einträge (z.B. Entities anderer Integrationen)
- Konflikte, falls shopf bereits eingerichtet war und Ziel-IDs schon belegt sind

**Wichtig: Prüfe die Vorschau gegen deine tatsächlichen Sensoren**, bevor du weitermachst. Öffne parallel Einstellungen → Geräte & Dienste → APsystems Local API → Entitäten und vergleiche die Anzahl und die Namen. Taucht ein Sensor, den du kennst, in der Vorschau nicht auf, wurde er vom Skript nicht erkannt – dann lies den Abschnitt "Grenzen dieses Skripts", bevor du migrierst.

### Schritt 5: Migration durchführen

```bash
python3 /config/migrate_apsystems.py --auto
```

Ablauf: Das Skript zeigt dieselbe Vorschau wie `--analyze`, fragt danach mit `[j/N]` nach Bestätigung. Nach `j` startet ein Hintergrundprozess, der:

1. Home Assistant stoppt (`ha core stop`)
2. prüft, ob die Datenbank wirklich nicht mehr beschrieben wird
3. ein zusätzliches, zeitgestempeltes Backup der Datenbank anlegt
4. die IDs umbenennt und verifiziert
5. die Verweise im Energie-Dashboard (`.storage/energy`) mit anpasst
6. Home Assistant wieder startet – auch wenn zwischendurch ein Fehler auftrat

Die Verbindung darf währenddessen abbrechen, der Prozess läuft unabhängig davon weiter. Nach maximal 5 Minuten ist die Oberfläche wieder da. Das Protokoll steht in:

```
/config/migrate_apsystems.log
```

Kontrolliere dort die letzte Zeile: `ERGEBNIS: ERFOLG`. Bei `FEHLGESCHLAGEN` oder `UNSICHER` wurde entweder gar nichts verändert oder es liegt zusätzlich ein Backup bereit, siehe "Fehlerbehebung".

### Schritt 6: Neue Integration einrichten

Einstellungen → Geräte & Dienste → Integration hinzufügen → "APsystems (Community Enhanced)" → IP-Adresse und Port (8050) eingeben.

**Der Gerätename muss exakt so eingetragen werden, dass er auf demselben Präfix führt wie in der Migrations-Vorschau angezeigt** (z.B. "WR17" → Präfix `wr17`). Ein abweichender Name erzeugt neue, leere Entities statt die migrierten zu
übernehmen (meist erkennbar an einem `_2`-Suffix in der Entity-ID).

Bei mehreren Wechselrichtern: für jeden einzeln wiederholen.

### Schritt 7: Prüfen

- **Entitäten** (Geräte & Dienste → APsystems (Community Enhanced) → Entitäten): IDs mit den in der Vorschau gezeigten Ziel-Namen vergleichen. Kein `_2`-Suffix.
- **Verlauf** einzelner Sensoren öffnen: Historie sollte nahtlos weitergehen, kein Bruch zum Migrationszeitpunkt.
- **Energie-Dashboard**: mehrere Tage/Wochen zurückblättern, nicht nur den aktuellen Tag ansehen. Zeigt es direkt nach der Migration nichts an, ist das in der Regel keine verlorene Statistik, sondern eine Verzögerung: Das Dashboard zeigt vorverdichtete Langzeitstatistik, die der Recorder erst neu berechnen muss, nachdem sich die `statistic_id` geändert hat. Das kann bis zu einer Stunde dauern. Ein erneuter `ha core start` (bzw. ein normaler HA-Neustart) erzwingt die Neuberechnung sofort. Prüfe zur Kontrolle den Verlauf-Tab des einzelnen Sensors (nicht das Dashboard) – zeigt der die alte Historie bereits korrekt an, sind die Daten vorhanden und es ist nur eine Anzeigeverzögerung.
- **Automationen/Dashboards** auf alte Entity-IDs durchsuchen:
  ```bash
  grep -rn "apsystemsapi_local\|wr17_power_p1\|<weitere alte IDs>" /config --include=*.yaml
  ```
  Diese müssen von Hand auf die neuen IDs angepasst werden, das Skript rührt daran nicht.

---

## Grenzen dieses Skripts

### Entity-IDs sind historisch, nicht aus dem aktuellen Sonnenladen-Code ableitbar

Home Assistant vergibt eine Entity-ID einmalig bei der Erstellung und ändert sie danach nie automatisch, auch nicht bei einem Update der Integration. Wurden deine Sensoren unter einer älteren Sonnenladen-Version angelegt, kann ihre ID-Struktur von der abweichen, die der aktuelle Sonnenladen-Code heute erzeugen würde. Ein bekanntes Beispiel: Bei manchen Installationen lautet die ID des P1-Leistungssensors `sensor.<gerät>_power_p1`, weil der interne Anzeigename nur "Power P1" lautet, nicht "Total Power P1", obwohl der interne Programm-Schlüssel `total_power_p1` heißt. Das Skript sucht deshalb nach dem tatsächlich in der Entity-ID verwendeten Text, nicht nach dem internen Namen der Integration – aber auch diese Liste kann für sehr alte oder sehr neue Sonnenladen-Versionen unvollständig sein.

**Prüfe deshalb bei `--analyze` immer, ob die Anzahl und Namen der gefundenen Sensoren zu dem passt, was du in Geräte & Dienste tatsächlich siehst.** Fehlt einer, wurde er nicht erkannt und muss von Hand ergänzt werden (Vorgehen wie im Abschnitt "Einzelne fehlende Sensoren nachträglich reparieren").

### Wenn du Entity-IDs manuell umbenannt hast

Das Bleistift-Symbol bei einer Entität bietet zwei unabhängige Felder:

- **Name** (Anzeigename): Eine Änderung hier hat **keinen** Einfluss auf die Migration. Das Skript arbeitet ausschließlich mit der technischen ID.
- **Entity-ID**: Eine Änderung hier **kann** die Erkennung beeinflussen – abhängig davon, ob deine Sonnenladen-Version eine `unique_id` je Entität vergibt:
  - **Mit `unique_id`** (erkennbar daran, dass die Entity unter Geräte & Dienste → APsystems Local API → Entitäten aufgeführt ist): Das Skript liest zusätzlich die Entity-Registry aus und erkennt die Entity über ihre `unique_id` wieder, unabhängig vom aktuellen Namenstext. Eine manuelle Umbenennung ist dann unproblematisch.
  - **Ohne `unique_id`**: Das Skript erkennt Entities nur über das Namensmuster. Eine frei vergebene ID, die weder den Gerätenamen noch einen bekannten Sensor-Schlüssel enthält (z.B. `sensor.mein_balkon_ertrag`), wird **stillschweigend übersprungen** – ohne Fehlermeldung, aber auch ohne Migration für diesen einen Sensor.

Im Zweifel: `--analyze` laufen lassen und die Ausgabe mit der tatsächlichen Entitätenliste vergleichen, bevor du migrierst.

### Was das Skript nicht anfasst

- Automationen, Skripte, Dashboards, Templates: alte Entity-IDs müssen dort von Hand ersetzt werden (siehe Schritt 7).
- Die Konfiguration selbst (IP-Adresse, Abfrageintervall): muss in shopf neu eingegeben werden.
- Nur die Standard-SQLite-Datenbank wird unterstützt. Ist in `configuration.yaml` eine externe Datenbank (`db_url`, z.B. MariaDB/Postgres) eingetragen, bricht das Skript kontrolliert ab, ohne etwas zu verändern.

---

## Fehlerbehebung

### "Datenbank nicht gefunden"
Pfad manuell als Argument übergeben:
```bash
python3 /config/migrate_apsystems.py --analyze /homeassistant/home-assistant_v2.db
```

### "shopf ist nicht installiert"
shopf muss vor der Analyse per HACS heruntergeladen sein (Schritt 2), auch wenn es noch nicht eingerichtet ist. Das Skript braucht dessen Übersetzungsdateien, um die Ziel-Namen zu berechnen.

### "Datenbank ungewöhnlich klein" / externe Datenbank
Falscher Pfad, beschädigte Datei, oder eine in `configuration.yaml` konfigurierte externe Datenbank. Unter Einstellungen → System → Speicher prüfen. Oder z.B. im App-Store "SQLite Web" herunterladen und für die weitere Analyse verwenden.

### Konflikte in der Vorschau
Meist weil shopf schon einmal eingerichtet war und dabei eigene, leere Einträge unter den Ziel-Namen angelegt hat. In HA die shopf-Integration entfernen, darunter ggf. die leere Statistik löschen (Einstellungen → Werkzeuge → Statistik → betroffene Entität → Statistik löschen), dann `--analyze` erneut ausführen.

### Einzelne fehlende Sensoren nachträglich reparieren
Ist die Migration bereits gelaufen und danach fällt auf, dass ein einzelner Sensor bei Null anfängt (weil er von `--analyze` nicht erkannt wurde), lässt sich das gezielt nachholen.
Wenn du magst, kann ich dir dafür ein kleines, auf deinen konkreten Sensor zugeschnittenes Skript bauen.

### Etwas ist schiefgelaufen – Backup wiederherstellen
Das Skript legt automatisch ein zeitgestempeltes Backup der Datenbank an, bevor es etwas ändert:
```bash
ha core stop
cp /homeassistant/home-assistant_v2.db.backup_DATUM_UHRZEIT /homeassistant/home-assistant_v2.db
ha core start
```
Hilft das nicht, das vollständige HA-Backup aus Schritt 1 wiederherstellen: Einstellungen → System → Backups → Backup hochladen → Wiederherstellen.

---

## Technischer Hintergrund

Home Assistant speichert Statistiken und Entitäts-Verlauf in einer SQLite-Datenbank, referenziert über `statistic_id` (Tabelle `statistics_meta`) bzw. `entity_id` (Tabelle `states_meta`). Beim Wechsel der Integration entstehen unter der neuen Integration andere IDs; ohne Eingriff würde der bisherige Verlauf bei den alten, nicht mehr existierenden IDs verwaist zurückbleiben.

Das Skript:
1. sucht Entities, die zum Namensmuster oder – falls vorhanden – zur `unique_id` der Sonnenladen-Integration passen,
2. berechnet die IDs, die die installierte shopf-Version für denselben Gerätenamen erzeugen würde (aus deren Übersetzungsdateien, passend zur HA-Sprache),
3. benennt die betroffenen Zeilen in `statistics_meta` und `states_meta` um,
4. passt die Referenzen im Energie-Dashboard (`.storage/energy`) an.

Die neue Integration findet beim ersten Start die bereits vorhandenen Statistiken unter den erwarteten Namen und übernimmt sie nahtlos.

---

## Kompatibilität

| Von | Nach | Unterstützt |
|-----|------|-------------|
| Sonnenladen GmbH (`apsystemsapi_local`) | shopf Community (`apsystems`) | ✅ |
| Sonnenladen GmbH (`apsystemsapi_local`) | Offizielle HA Core Integration (`apsystems`) | ❌ nicht unterstützt |

Die offizielle Core-Integration fragt im eigentlichen Einrichtungsdialog keinen Gerätenamen ab (Standardtitel "Solar"); den Namen vergibt man erst danach über den allgemeinen HA-Dialog "Gerät benennen und zuordnen". Sie leitet ihre `unique_id` – genau wie shopf – von der Seriennummer des Wechselrichters ab, nicht vom Gerätenamen. Das gesamte Namensschema dieses Skripts (Präfix aus dem Gerätenamen berechnen) passt darauf konzeptionell nicht, weshalb Sonnenladen → Core hier nicht unterstützt wird.

---

## Lizenz

MIT – Nutzung auf eigene Gefahr. Immer vorher ein Backup erstellen und extern sichern.
