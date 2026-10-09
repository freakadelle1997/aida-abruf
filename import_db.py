"""Baut data/aida.db aus dem Daten-Repo (data/<journey>.json, von export.py) neu auf.

Für Läufe ohne lokale Datenbank (GitHub Actions): die volle Historie liegt als JSON im
Daten-Repo, daraus entsteht vor jedem Lauf wieder dieselbe SQLite-Datenbank.

    .venv/bin/python import_db.py <daten-verzeichnis>
"""
import json
import sys
from pathlib import Path

import store


def import_all(src):
    src = Path(src)
    if store.DB.exists():
        store.DB.unlink()
    con = store.connect()
    n = 0
    for f in sorted((src / "data").glob("*.json")):
        h = json.loads(f.read_text())
        for r in h["runs"]:
            cats = r["categories"]
            attrs = r.get("cabin_attrs") or {}
            snap = {
                "journey": h["journey"], "fetched_at": r["fetched_at"], "title": h.get("title"),
                "ship": h.get("ship"), "start": h.get("start"), "end": h.get("end"), "from": h.get("from"),
                "tariff": h.get("tariff"), "ship_total": r["ship_total"], "free_total": r["free_total"],
                "map_url": h.get("map_url"), "offer": r.get("offer"), "itinerary": r.get("itinerary"),
                "region": r.get("region"), "cabin_choice": r.get("cabin_choice"),
                "tariffs": [dict(t, notes=(t.get("notes") or "").split(",") if t.get("notes") else [],
                                 campaigns=(t.get("campaigns") or "").split(",") if t.get("campaigns") else [])
                            for t in r.get("tariffs") or []],
                "categories": [{
                    "code": c["code"], "group": c["grp"], "name": c["name"], "guarantee": c["guarantee"],
                    "free": c["free"], "cabins": c.get("cabins") or [],
                    "attrs": {num: attrs[num].split(",") for num in c.get("cabins") or [] if num in attrs},
                    "prices": {k: v for k, v in (("IND", c.get("price_premium")), ("CLA", c.get("price_classic")),
                                                 ("INDAI", c.get("price_premium_ai")), ("LIG", c.get("price_light")),
                                                 ("CLAAI", c.get("price_classic_ai")),
                                                 ("COMAI", c.get("price_comfort_ai")))
                               if v is not None}} for c in cats]}
            store.save_snapshot(con, h.get("label"), snap)
            n += 1
        for e in h.get("errors") or []:
            store.save_error(con, h["journey"], h.get("label"), e["fetched_at"], e["error"])
    print(f"import: {n} Abrufe aus {src}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    import_all(sys.argv[1])
