#!/usr/bin/env python3
"""
APsystems Migration Script
Überträgt Statistiken und Verlauf der Sonnenladen-Integration (apsystemsapi_local) auf die shopf Community Enhanced Integration (apsystems), indem die IDs in der Home-Assistant-Datenbank umbenannt werden.

Wie es funktioniert
  * Alte Entities werden an ihrer Struktur erkannt (z.B. sensor.wr17_lifetime_production), nicht an einem Domain-Namen.
  * Die neuen Entity-IDs werden so berechnet, wie Home Assistant sie für shopf vergibt: <Gerätename>_<übersetzter Entity-Name>. Die Namen stammen aus den Übersetzungsdateien der installierten shopf-Integration und der in HA eingestellten Sprache.

Verwendung (im Terminal & SSH Add-on/App, Add-on-Konfiguration: apks: [python3])
  Analyse, nur lesend (HA darf laufen): python3 /config/migrate_apsystems.py --analyze
  Migration, HA wird automatisch gestoppt und wieder gestartet (empfohlen): python3 /config/migrate_apsystems.py --auto
  Migration, HA ist bereits gestoppt (echte SSH-Sitzung nötig): python3 /config/migrate_apsystems.py --manual

Optionen
  DB-Pfad als Argument, sonst automatische Suche (/homeassistant/home-assistant_v2.db) --rename alt=neu Wenn shopf einen anderen Gerätenamen bekommt als bisher, z.B. --rename wr17=balkon_sued (mehrfach verwendbar)
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ============================================================
# KONFIGURATION
# ============================================================

OLD_DOMAIN = "apsystemsapi_local"
NEW_DOMAIN = "apsystems"

MIN_DB_SIZE_BYTES = 1 * 1024 * 1024  # 1 MB – kleinere DBs sind vermutlich leer/falsch

# Sonnenladen-Schlüssel → (Plattform, shopf translation_key)
# Schlüssel = wie der Text in der Sonnenladen-ENTITY-ID wirklich lautet (aus dem
# Anzeigenamen "sensor_name" abgeleitet, NICHT aus dem internen "sensor_id".
KEY_MAP: dict[str, tuple[str, str]] = {
    "total_power":            ("sensor", "total_power"),
    "power_p1":               ("sensor", "total_power_p1"),
    "power_p2":               ("sensor", "total_power_p2"),
    "today_production":       ("sensor", "today_production"),
    "today_production_p1":    ("sensor", "today_production_p1"),
    "today_production_p2":    ("sensor", "today_production_p2"),
    "lifetime_production":    ("sensor", "lifetime_production"),
    "lifetime_production_p1": ("sensor", "lifetime_production_p1"),
    "lifetime_production_p2": ("sensor", "lifetime_production_p2"),
    "power_status":           ("switch", "inverter_status"),
    "max_output_power":       ("number", "max_output"),
}

_KEYS = "|".join(sorted(KEY_MAP, key=len, reverse=True))

# unique_id-Format alter Sonnenladen-Versionen mit funktionierender unique_id:
# apsystemsapi_{Gerätename (roh, wie eingegeben)}_{sensor_id}
OLD_UNIQUE_ID_PREFIX = "apsystemsapi_"

# Entity-ID Textmuster – wird nur verwendet, wenn kein Registry-Eintrag (unique_id)
# vorliegt. Die Muster unten sind bewusst strukturell und decken die real
# beobachteten Varianten ab. (Muster, "starke" Evidenz?) – Reihenfolge = Priorität
PATTERNS: list[tuple[re.Pattern, bool]] = [
    # number/switch mit has_entity_name + gerätenamenhaltigem Namenstext:
    # <name>_apsystems_<name>_<key>  (z.B. switch.wr17_apsystems_wr17_power_status)
    (re.compile(rf"^(?P<platform>number|switch)\.(?P<prefix>.+?)_apsystems_(?P=prefix)_(?P<key>{_KEYS})$"), True),
    # number/switch ohne has_entity_name: apsystems_<name>_<key>
    (re.compile(rf"^(?P<platform>number|switch)\.apsystems_(?P<prefix>.+?)_(?P<key>{_KEYS})$"), True),
    # Sonnenladen sensor: <name>_<key>  (schwache Evidenz, siehe MIN_WEAK_KEYS)
    (re.compile(rf"^(?P<platform>sensor|number|switch)\.(?P<prefix>.+?)_(?P<key>{_KEYS})$"), False),
]
MIN_WEAK_KEYS = 2  # Ohne starke Evidenz müssen mind. so viele verschiedene Keys pro Gerät vorkommen


# ============================================================
# HILFSFUNKTIONEN
# ============================================================

def log(msg: str = "") -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def find_db() -> Path | None:
    for p in (
        Path("/homeassistant/home-assistant_v2.db"),
        Path("/config/home-assistant_v2.db"),
        Path.home() / ".homeassistant" / "home-assistant_v2.db",
    ):
        if p.exists():
            return p
    return None


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / 1024 / 1024:.1f} MB"


def open_readonly(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def backup_db(db_path: Path) -> Path:
    """Konsistentes Backup via SQLite-Backup-API (berücksichtigt die WAL-Datei)."""
    backup_path = db_path.parent / f"{db_path.name}.backup_{datetime.now():%Y%m%d_%H%M%S}"
    src, dst = sqlite3.connect(db_path), sqlite3.connect(backup_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return backup_path


def slugify(text: str) -> str:
    """Entspricht homeassistant.util.slugify (python-slugify, Trennzeichen '_')."""
    text = text.replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def uses_external_db(config_dir: Path) -> bool:
    try:
        text = (config_dir / "configuration.yaml").read_text(encoding="utf-8")
    except Exception:
        return False
    return re.search(r"^\s*db_url\s*:", text, re.MULTILINE) is not None


def load_registry(config_dir: Path) -> dict[str, str]:
    """entity_id → Plattform (Integration) aus core.entity_registry."""
    data = read_json(config_dir / ".storage" / "core.entity_registry") or {}
    return {
        e.get("entity_id", ""): e.get("platform", "")
        for e in data.get("data", {}).get("entities", [])
    }


def parse_old_unique_id(unique_id: str) -> tuple[str, str] | None:
    """apsystemsapi_{Gerätename}_{sensor_id} → (Gerätename roh, sensor_id)."""
    if not unique_id or not unique_id.startswith(OLD_UNIQUE_ID_PREFIX):
        return None
    rest = unique_id[len(OLD_UNIQUE_ID_PREFIX):]
    for key in sorted(KEY_MAP, key=len, reverse=True):
        suffix = f"_{key}"
        if rest.endswith(suffix):
            return rest[: -len(suffix)], key
    return None


def load_old_entities(config_dir: Path) -> dict[str, tuple[str, str]]:
    """entity_id → (Präfix, key) für Sonnenladen-Entities via Registry/unique_id.

    Robuster als Text-Muster auf der entity_id, weil unique_id sich auch dann
    nicht ändert, wenn der Nutzer die entity_id manuell umbenannt hat.
    """
    data = read_json(config_dir / ".storage" / "core.entity_registry") or {}
    result: dict[str, tuple[str, str]] = {}
    for e in data.get("data", {}).get("entities", []):
        if e.get("platform") != OLD_DOMAIN:
            continue
        parsed = parse_old_unique_id(e.get("unique_id", ""))
        entity_id = e.get("entity_id", "")
        if parsed and entity_id:
            result[entity_id] = (slugify(parsed[0]), parsed[1])
    return result


def configured_domains(config_dir: Path) -> set[str]:
    data = read_json(config_dir / ".storage" / "core.config_entries") or {}
    return {e.get("domain", "") for e in data.get("data", {}).get("entries", [])}


def ha_language(config_dir: Path) -> str:
    data = read_json(config_dir / ".storage" / "core.config") or {}
    return data.get("data", {}).get("language") or "en"


def load_shopf_names(config_dir: Path, language: str) -> tuple[dict, list[str], str | None]:
    """Liest die Entity-Namen der installierten shopf-Integration.

    Rückgabe: ({(plattform, key): name}, verwendete_sprachen, shopf_version)
    Fehlende Übersetzungen fallen – wie in HA – auf Englisch zurück.
    """
    base = config_dir / "custom_components" / NEW_DOMAIN
    if not base.is_dir():
        return {}, [], None

    version = (read_json(base / "manifest.json") or {}).get("version")
    lang_chain = []
    for cand in (language, re.split(r"[-_]", language)[0], "en"):
        if cand not in lang_chain:
            lang_chain.append(cand)

    names: dict[tuple[str, str], str] = {}
    used: list[str] = []
    # Zuerst Englisch/strings.json als Basis, dann die Wunschsprache darüberlegen
    sources = [("strings", base / "strings.json")]
    sources += [(lang, base / "translations" / f"{lang}.json") for lang in reversed(lang_chain)]
    for label, path in sources:
        data = read_json(path)
        if not data:
            continue
        used.append(label)
        for platform, ents in data.get("entity", {}).items():
            for key, val in ents.items():
                if isinstance(val, dict) and val.get("name"):
                    names[(platform, key)] = val["name"]
    return names, used, version


# ============================================================
# PLAN: was wird wohin umbenannt?
# ============================================================

@dataclass
class Item:
    table: str          # statistics_meta | states_meta
    row_id: int
    old_id: str
    new_id: str | None
    prefix: str
    key: str


@dataclass
class Plan:
    language: str = "en"
    shopf_version: str | None = None
    languages_used: list[str] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    prefixes: dict[str, str] = field(default_factory=dict)  # alt → neu


def classify(entity_id: str) -> tuple[str, str, bool] | None:
    """entity_id → (prefix, key, starke_evidenz) oder None."""
    for pattern, strong in PATTERNS:
        m = pattern.match(entity_id)
        if m and KEY_MAP[m["key"]][0] == m["platform"]:
            return m["prefix"], m["key"], strong
    return None


def build_plan(conn: sqlite3.Connection, config_dir: Path, renames: dict[str, str]) -> Plan:
    plan = Plan(language=ha_language(config_dir))
    names, plan.languages_used, plan.shopf_version = load_shopf_names(config_dir, plan.language)
    if not names:
        plan.errors.append(
            "shopf ist nicht installiert (Ordner custom_components/apsystems fehlt). "
            "Bitte zuerst per HACS herunterladen – aber noch NICHT einrichten."
        )
    registry = load_registry(config_dir)
    old_via_registry = load_old_entities(config_dir)  # entity_id -> (Präfix, key), sehr sicher

    tables = (
        ("statistics_meta", "SELECT id, statistic_id FROM statistics_meta"),
        ("states_meta", "SELECT metadata_id, entity_id FROM states_meta"),
    )
    existing: dict[str, set[str]] = {}
    groups: dict[str, dict] = {}

    for table, sql in tables:
        rows = conn.execute(sql).fetchall()
        existing[table] = {r[1] for r in rows}
        for row_id, entity_id in rows:
            if entity_id in old_via_registry:
                prefix, key = old_via_registry[entity_id]
                strong = True
            else:
                found = classify(entity_id)
                if not found:
                    continue
                reg_platform = registry.get(entity_id)
                if reg_platform and reg_platform != OLD_DOMAIN:
                    plan.skipped.append(f"{entity_id} (gehört zu '{reg_platform}')")
                    continue
                prefix, key, strong = found
            g = groups.setdefault(prefix, {"keys": set(), "strong": False, "rows": []})
            g["keys"].add(key)
            g["strong"] |= strong
            g["rows"].append((table, row_id, entity_id, key))

    for prefix, g in sorted(groups.items()):
        if not g["strong"] and len(g["keys"]) < MIN_WEAK_KEYS:
            plan.skipped.append(
                f"{prefix}_… (nur {len(g['keys'])} passender Eintrag, zu unsicher)"
            )
            continue
        new_prefix = slugify(renames.get(prefix, prefix))
        plan.prefixes[prefix] = new_prefix
        for table, row_id, old_id, key in g["rows"]:
            platform, new_key = KEY_MAP[key]
            name = names.get((platform, new_key))
            new_id = f"{platform}.{new_prefix}_{slugify(name)}" if name else None
            if names and not new_id:
                plan.errors.append(f"Kein shopf-Name für {platform}/{new_key} gefunden.")
            plan.items.append(Item(table, row_id, old_id, new_id, prefix, key))

    # Konflikte: Ziel-ID schon belegt oder doppelt vergeben
    seen: dict[tuple[str, str], str] = {}
    for it in plan.items:
        if not it.new_id:
            continue
        if it.new_id != it.old_id and it.new_id in existing[it.table]:
            plan.conflicts.append(
                f"{it.table}: Ziel '{it.new_id}' existiert bereits (von '{it.old_id}')"
            )
        other = seen.get((it.table, it.new_id))
        if other:
            plan.conflicts.append(
                f"{it.table}: '{it.old_id}' und '{other}' ergeben beide '{it.new_id}'"
            )
        seen[(it.table, it.new_id)] = it.old_id
    return plan


def print_plan(plan: Plan) -> None:
    print("\n" + "=" * 60)
    print("ERKANNTE ENTITIES UND GEPLANTE UMBENENNUNG")
    print("=" * 60)
    print(f"\nHA-Sprache:        {plan.language}")
    if plan.shopf_version:
        print(f"shopf installiert: Version {plan.shopf_version} "
              f"(Namen aus: {', '.join(plan.languages_used)})")

    if not plan.items:
        print(f"\n⚠️  Keine Sonnenladen-Entities gefunden.")
    for prefix, new_prefix in plan.prefixes.items():
        items = [i for i in plan.items if i.prefix == prefix]
        print(f"\n🔌 Gerät '{prefix}'  →  neuer Präfix '{new_prefix}'  ({len(items)} Einträge)")
        for it in sorted(items, key=lambda i: (i.table, i.old_id)):
            tag = "Statistik" if it.table == "statistics_meta" else "Verlauf  "
            print(f"   [{tag}] {it.old_id}")
            print(f"              → {it.new_id or '???'}")

    if plan.skipped:
        print("\nℹ️  Bewusst nicht angefasst:")
        for s in plan.skipped:
            print(f"   {s}")
    if plan.errors:
        print("\n❌ Probleme:")
        for e in plan.errors:
            print(f"   {e}")
    if plan.conflicts:
        print("\n❌ Konflikte – Migration nicht möglich:")
        for c in plan.conflicts:
            print(f"   {c}")
        print("   Meist war shopf schon eingerichtet. Siehe MIGRATION.md → Fehlerbehebung.")
    if plan.prefixes and not plan.errors and not plan.conflicts:
        print("\n💡 Wichtig: shopf muss danach mit genau diesem Gerätenamen eingerichtet werden,")
        print("   damit die Entity-IDs zu den umbenannten Einträgen passen:")
        for new_prefix in plan.prefixes.values():
            print(f"   Gerätename, der '{new_prefix}' ergibt (z.B. '{new_prefix.upper()}')")
    total = len(plan.items)
    print(f"\n➡️  Gesamt: {total} Einträge")


# ============================================================
# MIGRATION
# ============================================================

def apply_plan(conn: sqlite3.Connection, plan: Plan) -> int:
    conn.execute("BEGIN")
    try:
        for it in plan.items:
            col = "statistic_id" if it.table == "statistics_meta" else "entity_id"
            key = "id" if it.table == "statistics_meta" else "metadata_id"
            conn.execute(f"UPDATE {it.table} SET {col} = ? WHERE {key} = ?", (it.new_id, it.row_id))
        conn.execute("COMMIT")
    except sqlite3.Error:
        conn.execute("ROLLBACK")
        raise
    return len(plan.items)


def verify_plan(conn: sqlite3.Connection, plan: Plan) -> bool:
    for it in plan.items:
        col = "statistic_id" if it.table == "statistics_meta" else "entity_id"
        key = "id" if it.table == "statistics_meta" else "metadata_id"
        row = conn.execute(f"SELECT {col} FROM {it.table} WHERE {key} = ?", (it.row_id,)).fetchone()
        if not row or row[0] != it.new_id:
            return False
    return True


def update_energy_prefs(config_dir: Path, plan: Plan) -> int:
    """Passt die Energie-Dashboard-Einstellungen (.storage/energy) an die neuen IDs an."""
    path = config_dir / ".storage" / "energy"
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8")
    count = 0
    for old, new in {(i.old_id, i.new_id) for i in plan.items}:
        token = f'"{old}"'
        n = text.count(token)
        if n:
            text = text.replace(token, f'"{new}"')
            count += n
    if count:
        shutil.copy2(path, path.with_name(f"energy.backup_{datetime.now():%Y%m%d_%H%M%S}"))
        path.write_text(text, encoding="utf-8")
    return count


def ha_cli(*args: str, timeout: int = 300) -> tuple[bool, str]:
    try:
        r = subprocess.run(["ha", *args], capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return False, "'ha' CLI nicht gefunden – Script im Terminal & SSH Add-on ausführen"
    except subprocess.TimeoutExpired:
        return False, f"Timeout bei 'ha {' '.join(args)}'"


def wait_for_db_free(db_path: Path, seconds: int) -> bool:
    """Wartet, bis niemand mehr in die DB schreibt, und leert die WAL-Datei."""
    deadline = time.time() + seconds
    while True:
        try:
            c = sqlite3.connect(db_path, timeout=2, isolation_level=None)
            c.execute("BEGIN EXCLUSIVE")
            c.execute("ROLLBACK")
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.close()
            return True
        except sqlite3.OperationalError:
            if time.time() >= deadline:
                return False
            time.sleep(2)


def run_job(db_path: Path, renames: dict[str, str], control_ha: bool) -> None:
    """Der eigentliche Ablauf. Mit control_ha=True wird HA gestoppt und wieder gestartet."""
    result = "FEHLGESCHLAGEN – Datenbank wurde nicht verändert"
    backup_path = None
    config_dir = db_path.parent
    try:
        if control_ha:
            log("Stoppe Home Assistant Core (kann 1-2 Minuten dauern)...")
            ok, out = ha_cli("core", "stop")
            log(f"ha core stop: {'OK' if ok else 'FEHLER'} {out}")
            if not ok:
                return
        if not wait_for_db_free(db_path, 90 if control_ha else 5):
            log("Datenbank ist gesperrt – läuft Home Assistant noch? Abbruch ohne Änderungen.")
            return

        conn = sqlite3.connect(db_path)
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
        conn.close()
        if check != "ok":
            log(f"Datenbank-Prüfung fehlgeschlagen: {check} – Abbruch ohne Änderungen.")
            return
        log("Datenbank-Prüfung: OK")

        backup_path = backup_db(db_path)
        log(f"Backup erstellt: {backup_path}")

        conn = sqlite3.connect(db_path)
        plan = build_plan(conn, config_dir, renames)
        if plan.errors or plan.conflicts:
            for msg in plan.errors + plan.conflicts:
                log(f"PROBLEM: {msg}")
            conn.close()
            return
        if not plan.items:
            log("Keine Sonnenladen-Entities gefunden – nichts zu tun.")
            conn.close()
            result = "NICHTS ZU TUN – Datenbank unverändert"
            return

        changed = apply_plan(conn, plan)
        ok = verify_plan(conn, plan)
        conn.close()
        log(f"{changed} Einträge umbenannt, Verifikation: {'OK' if ok else 'FEHLER'}")
        n = update_energy_prefs(config_dir, plan)
        log(f"Energie-Dashboard-Einstellungen angepasst: {n} Verweise")
        result = "ERFOLG" if ok else f"UNSICHER – bitte prüfen (Backup: {backup_path})"
    except Exception as e:  # noqa: BLE001 – HA muss in jedem Fall wieder gestartet werden
        log(f"Unerwarteter Fehler: {e!r}")
        result = f"FEHLGESCHLAGEN (Backup: {backup_path})"
    finally:
        if control_ha:
            log("Starte Home Assistant Core...")
            ok, out = ha_cli("core", "start")
            log(f"ha core start: {'OK' if ok else 'FEHLER – bitte HA manuell starten'} {out}")
        log(f"ERGEBNIS: {result}")


def start_worker(db_path: Path, renames: dict[str, str]) -> Path:
    """Startet den Job vom Terminal losgelöst; er überlebt den Verbindungsabbruch."""
    log_path = db_path.parent / "migrate_apsystems.log"
    logf = open(log_path, "w", encoding="utf-8")
    cmd = [sys.executable, os.path.abspath(__file__), "--worker", str(db_path)]
    for old, new in renames.items():
        cmd += ["--rename", f"{old}={new}"]
    subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
        start_new_session=True, close_fds=True, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    return log_path


# ============================================================
# EINSTIEGSPUNKT
# ============================================================

def main() -> None:
    ap = argparse.ArgumentParser(description="APsystems Migration: Sonnenladen → shopf")
    ap.add_argument("db", nargs="?", help="Pfad zur home-assistant_v2.db")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--analyze", action="store_true", help="nur analysieren, nichts ändern")
    mode.add_argument("--auto", action="store_true", help="HA stoppen, migrieren, HA starten")
    mode.add_argument("--manual", action="store_true", help="HA ist bereits gestoppt")
    ap.add_argument("--rename", action="append", default=[], metavar="ALT=NEU",
                    help="Gerätename-Präfix ändern, z.B. wr17=balkon_sued")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    renames: dict[str, str] = {}
    for r in args.rename:
        if "=" not in r:
            ap.error(f"--rename erwartet ALT=NEU, bekam '{r}'")
        old, new = r.split("=", 1)
        renames[slugify(old)] = new

    db_path = Path(args.db) if args.db else find_db()

    if args.worker:                       # interner Aufruf durch --auto
        run_job(db_path, renames, control_ha=True)
        return

    print("=" * 60)
    print("APsystems Migration: Sonnenladen → shopf")
    print("=" * 60)

    if not (args.analyze or args.auto or args.manual):
        print("\nBitte einen Modus wählen: --analyze, --auto oder --manual (siehe --help).")
        sys.exit(2)
    if not db_path or not db_path.exists():
        print("\n❌ Datenbank nicht gefunden. Pfad als Argument übergeben, z.B.:")
        print("   python3 migrate_apsystems.py --analyze /homeassistant/home-assistant_v2.db")
        sys.exit(1)

    size = db_path.stat().st_size
    print(f"\n✅ Datenbank: {db_path} ({format_size(size)})")
    if uses_external_db(db_path.parent):
        print("\n❌ In configuration.yaml ist eine externe Datenbank (db_url) konfiguriert.")
        print("   Dieses Skript unterstützt nur die Standard-SQLite-Datenbank.")
        sys.exit(1)
    if size < MIN_DB_SIZE_BYTES:
        print(f"\n⚠️  Datenbank ungewöhnlich klein (< {format_size(MIN_DB_SIZE_BYTES)}) – "
              "falsche Datei? Abbruch.")
        sys.exit(1)

    conn = open_readonly(db_path)
    plan = build_plan(conn, db_path.parent, renames)
    conn.close()
    print_plan(plan)

    if args.analyze:
        sys.exit(1 if plan.errors or plan.conflicts else 0)
    if plan.errors or plan.conflicts or not plan.items:
        print("\nKeine Änderungen vorgenommen.")
        sys.exit(1)

    domains = configured_domains(db_path.parent)
    if OLD_DOMAIN in domains:
        print(f"\n⚠️  '{OLD_DOMAIN}' ist noch in HA eingerichtet. Empfohlen: zuerst entfernen.")
    if NEW_DOMAIN in domains:
        print(f"\n⚠️  '{NEW_DOMAIN}' ist bereits eingerichtet. Empfohlen: zuerst entfernen.")

    print("\n" + "=" * 60)
    if args.auto:
        print("Ablauf: HA wird gestoppt → Migration → HA wird wieder gestartet.")
        print("Oberfläche und Terminal sind dabei ca. 2-5 Minuten nicht erreichbar.")
        if input("Migration jetzt starten? [j/N]: ").strip().lower() != "j":
            print("Abgebrochen. Keine Änderungen vorgenommen.")
            sys.exit(0)
        log_path = start_worker(db_path, renames)
        print(f"\n🚀 Migration läuft im Hintergrund. Log: {log_path}")
        print("   Die Verbindung darf jetzt abbrechen. Nach dem HA-Start das Log")
        print("   (per Samba: config/migrate_apsystems.log) auf 'ERGEBNIS: ERFOLG' prüfen.")
    else:
        print("Manueller Modus: Home Assistant muss bereits gestoppt sein (ha core stop).")
        if input("Migration jetzt durchführen? [j/N]: ").strip().lower() != "j":
            print("Abgebrochen. Keine Änderungen vorgenommen.")
            sys.exit(0)
        run_job(db_path, renames, control_ha=False)
        print("\nWeiter: ha core start, danach shopf einrichten.")


if __name__ == "__main__":
    main()
