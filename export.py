"""Exportiert die komplette Historie aus data/aida.db als JSON ins Daten-Repo.

Pro Reise eine Datei data/<journey>.json (volle Historie, zum Rebuild der DB geeignet)
plus latest/<journey>.json (nur der letzte Stand, kompakt zum schnellen Lesen).

    .venv/bin/python export.py <ziel-verzeichnis>
"""
import json
import sys
from pathlib import Path

import store


def _run_dict(con, r):
    cats = [dict(c) for c in con.execute(
        "SELECT code, grp, name, guarantee, free, price_premium, price_classic, price_premium_ai, price_light,"
        " price_classic_ai, price_comfort_ai"
        " FROM categories WHERE run_id=? ORDER BY code", (r["id"],))]
    cabins, attrs = {}, {}
    for c in con.execute("SELECT code, number, attrs FROM cabins WHERE run_id=? ORDER BY code, number", (r["id"],)):
        cabins.setdefault(c["code"], []).append(c["number"])
        if c["attrs"]:
            attrs[c["number"]] = c["attrs"]
    for c in cats:
        c["guarantee"] = bool(c["guarantee"])
        c["cabins"] = cabins.get(c["code"], [])
    out = {"fetched_at": r["fetched_at"], "ship_total": r["ship_total"],
           "free_total": r["free_total"], "categories": cats}
    if attrs:
        out["cabin_attrs"] = attrs
    if r["offer"]:
        out["offer"] = json.loads(r["offer"])
    for k in ("itinerary",):
        if r[k]:
            out[k] = json.loads(r[k])
    if r["region"]:
        out["region"] = r["region"]
    if r["cabin_choice"] is not None:
        out["cabin_choice"] = bool(r["cabin_choice"])
    tar = [dict(t) for t in con.execute("SELECT cabin_type, tariff, cabin, flight, flight_from, rabat, notes,"
                                        " campaigns, flights FROM tariffs WHERE run_id=?", (r["id"],))]
    for t in tar:
        t["flights"] = json.loads(t["flights"]) if t["flights"] else None
    if tar:
        out["tariffs"] = tar
    return out


def export(dest):
    dest = Path(dest)
    (dest / "data").mkdir(parents=True, exist_ok=True)
    (dest / "latest").mkdir(parents=True, exist_ok=True)
    con = store.connect()
    journeys = con.execute("SELECT DISTINCT journey FROM runs WHERE ok=1 ORDER BY journey").fetchall()
    written = []
    for j in journeys:
        jid = j["journey"]
        runs = con.execute("SELECT * FROM runs WHERE journey=? AND ok=1 ORDER BY fetched_at", (jid,)).fetchall()
        meta = runs[-1]
        hist = {"journey": jid, "label": meta["label"], "title": meta["title"], "ship": meta["ship"],
                "start": meta["start"], "end": meta["end"], "from": meta["from_city"], "tariff": meta["tariff"],
                "map_url": next((r["map_url"] for r in reversed(runs) if r["map_url"]), None),
                "runs": [_run_dict(con, r) for r in runs],
                # fehlgeschlagene Abrufe (z. B. AIDA sperrt die Kabinenauswahl) – für die Mustersuche
                "errors": [{"fetched_at": e["fetched_at"], "error": e["error"]} for e in con.execute(
                    "SELECT fetched_at, error FROM runs WHERE journey=? AND ok=0 ORDER BY fetched_at", (jid,))]}
        (dest / "data" / f"{jid}.json").write_text(json.dumps(hist, ensure_ascii=False, indent=1))
        latest = dict(hist, runs=[hist["runs"][-1]])
        (dest / "latest" / f"{jid}.json").write_text(json.dumps(latest, ensure_ascii=False, indent=1))
        written.append(jid)
    return written


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print("exportiert:", ", ".join(export(sys.argv[1])))
