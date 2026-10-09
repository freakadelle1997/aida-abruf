"""SQLite-Ablage der AIDA-Abrufe (data/aida.db)."""
import json
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "data" / "aida.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    journey TEXT NOT NULL,
    label TEXT,
    fetched_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT,
    title TEXT, ship TEXT, start TEXT, end TEXT, from_city TEXT, tariff TEXT,
    ship_total INTEGER, free_total INTEGER, map_url TEXT
);
CREATE TABLE IF NOT EXISTS categories (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    code TEXT NOT NULL, grp TEXT, name TEXT,
    guarantee INTEGER, free INTEGER,
    price_premium REAL, price_classic REAL, price_premium_ai REAL,
    PRIMARY KEY (run_id, code)
);
CREATE TABLE IF NOT EXISTS cabins (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    code TEXT NOT NULL, number TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tariffs (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    cabin_type TEXT NOT NULL, tariff TEXT NOT NULL,
    cabin REAL, flight REAL, flight_from TEXT, rabat REAL, notes TEXT, campaigns TEXT,
    flights TEXT
);
CREATE INDEX IF NOT EXISTS idx_tariffs_run ON tariffs(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_journey ON runs(journey, fetched_at);
CREATE INDEX IF NOT EXISTS idx_cabins_run ON cabins(run_id);
"""


def connect():
    DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = {r["name"] for r in con.execute("PRAGMA table_info(categories)")}
    if "price_premium_ai" not in cols:
        con.execute("ALTER TABLE categories ADD COLUMN price_premium_ai REAL")
    rcols = {r["name"] for r in con.execute("PRAGMA table_info(runs)")}
    if "map_url" not in rcols:
        con.execute("ALTER TABLE runs ADD COLUMN map_url TEXT")
    for col, typ in (("itinerary", "TEXT"), ("region", "TEXT"), ("cabin_choice", "INTEGER")):
        if col not in rcols:
            con.execute(f"ALTER TABLE runs ADD COLUMN {col} {typ}")
    if "offer" not in rcols:
        con.execute("ALTER TABLE runs ADD COLUMN offer TEXT")   # Angebot aus der Suche (JSON)
    for col in ("price_light", "price_classic_ai", "price_comfort_ai"):
        if col not in cols:
            con.execute(f"ALTER TABLE categories ADD COLUMN {col} REAL")
    tcols = {r["name"] for r in con.execute("PRAGMA table_info(tariffs)")}
    if "flights" not in tcols:
        con.execute("ALTER TABLE tariffs ADD COLUMN flights TEXT")   # {Flughafen: Flugpreisanteil}
    kcols = {r["name"] for r in con.execute("PRAGMA table_info(cabins)")}
    if "attrs" not in kcols:
        con.execute("ALTER TABLE cabins ADD COLUMN attrs TEXT")   # Lage/Seite, kommagetrennt
    return con


def save_snapshot(con, label, s):
    cur = con.execute(
        "INSERT INTO runs (journey, label, fetched_at, ok, title, ship, start, end, from_city, tariff,"
        " ship_total, free_total, map_url, offer, itinerary, region, cabin_choice)"
        " VALUES (?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (s["journey"], label, s["fetched_at"], s["title"], s["ship"], s["start"], s["end"],
         s["from"], s["tariff"], s["ship_total"], s["free_total"], s.get("map_url"),
         json.dumps(s["offer"], ensure_ascii=False) if s.get("offer") else None,
         json.dumps(s["itinerary"], ensure_ascii=False) if s.get("itinerary") else None,
         s.get("region"), None if s.get("cabin_choice") is None else int(bool(s["cabin_choice"]))))
    run_id = cur.lastrowid
    for c in s["categories"]:
        con.execute("INSERT INTO categories (run_id, code, grp, name, guarantee, free, price_premium,"
                    " price_classic, price_premium_ai, price_light, price_classic_ai, price_comfort_ai)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, c["code"], c["group"], c["name"], int(c["guarantee"]), c["free"],
                     c["prices"].get("IND"), c["prices"].get("CLA"), c["prices"].get("INDAI"),
                     c["prices"].get("LIG"), c["prices"].get("CLAAI"), c["prices"].get("COMAI")))
        attrs = c.get("attrs") or {}
        con.executemany("INSERT INTO cabins (run_id, code, number, attrs) VALUES (?,?,?,?)",
                        [(run_id, c["code"], n, ",".join(attrs.get(n, [])) or None) for n in c["cabins"]])
    con.executemany("INSERT INTO tariffs (run_id, cabin_type, tariff, cabin, flight, flight_from, rabat,"
                    " notes, campaigns, flights) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(run_id, t["cabin_type"], t["tariff"], t["cabin"], t["flight"], t["flight_from"],
                      t["rabat"], ",".join(t["notes"]) or None, ",".join(t["campaigns"]) or None,
                      json.dumps(t["flights"]) if t.get("flights") else None)
                     for t in s.get("tariffs") or []])
    con.commit()
    return run_id


def save_error(con, journey, label, fetched_at, error):
    con.execute("INSERT INTO runs (journey, label, fetched_at, ok, error) VALUES (?,?,?,0,?)",
                (journey, label, fetched_at, error))
    con.commit()


def last_ok_runs(con, journey, n=2):
    return con.execute("SELECT * FROM runs WHERE journey=? AND ok=1 ORDER BY fetched_at DESC LIMIT ?",
                       (journey, n)).fetchall()


def ran_ok_today(con, journey, today):
    return con.execute("SELECT 1 FROM runs WHERE journey=? AND ok=1 AND substr(fetched_at,1,10)=?",
                       (journey, today)).fetchone() is not None


def live_trips_file():
    """Aktive Reiseliste: bevorzugt das Daten-Repo (daten/trips.json, neben der DB im
    Laufzeitordner), sonst die mitgelieferte trips.json. Wird über die DB aufgelöst, damit
    auch der Deploy aus dem Repo-Ordner die richtige (Laufzeit-)Liste liest."""
    cand = DB.parent.parent / "daten" / "trips.json"
    if cand.exists():
        return cand
    return Path(__file__).parent / "trips.json"
