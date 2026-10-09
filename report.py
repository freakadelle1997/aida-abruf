"""Baut uebersicht.html aus data/aida.db: Verlauf freier Kabinen und Preise je Reise."""
import hashlib
import json
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import store

OUT = Path(__file__).parent / "uebersicht.html"

# Kabinentypen in fester Reihenfolge; J und S laufen zusammen als Suiten.
GROUPS = [("I", "Innen"), ("M", "Meerblick"), ("B", "Balkon"), ("V", "Veranda"),
          ("K", "Veranda Komfort"), ("P", "Panorama"), ("D", "Veranda Deluxe"), ("S", "Suiten")]
GROUP_OF = {"J": "S"}


def _group(g):
    return GROUP_OF.get(g, g)


def _all_runs(con, journey):
    """Alle erfolgreichen Abrufe (03:30/09:00/14:00) aufsteigend -> Tagesverlauf."""
    return con.execute(
        "SELECT * FROM runs WHERE journey=? AND ok=1 ORDER BY fetched_at", (journey,)).fetchall()


def _cats(con, run_id):
    return {c["code"]: dict(c) for c in con.execute("SELECT * FROM categories WHERE run_id=?", (run_id,))}


def _cabin_set(con, run_id):
    return {(r["code"], r["number"]) for r in con.execute("SELECT code, number FROM cabins WHERE run_id=?", (run_id,))}


NACHSCHUB_MIN = 5   # gleiche Schwelle wie in run.py


def _ship_master(journey):
    """Kabinen-Stammdaten des Schiffs (daten/ships/<code>.json): Nummer -> kompakte Infos."""
    f = store.DB.parent.parent / "daten" / "ships" / f"{journey[:2].lower()}.json"
    if not f.exists():
        return {}
    out = {}
    for c in json.loads(f.read_text()).get("cabins", []):
        near = [n for k, n in (("close_to_restaurant", "Restaurant"), ("close_to_wellness", "Wellness"),
                               ("close_to_stairs", "Treppe")) if c.get(k)]
        beds = "Doppelbett" if c.get("double_bed") else (
            f"{c.get('quantity_of_separate_beds') or 2} Einzelbetten" if c.get("separate_beds") else None)
        extra = (c.get("quantity_of_sofabeds") or 0) + (c.get("quantity_wall_pullmans") or 0) \
            + (c.get("quantity_ceiling_pullmans") or 0)
        plan = c.get("cabin_floor_plan") or (c.get("cabin_category_specification") or {}).get("cabin_floor_plan")
        out[str(c["cabin_number"])] = {k: v for k, v in {
            "m2": c.get("cabin_size") or None,
            "bal": ({"glass": "Glasbrüstung", "steel": "Stahlbrüstung"}.get(c.get("balcony_type"), "Balkon")
                    + (f" {c['balcony_size']:.1f} m²".replace(".", ",") if c.get("balcony_size") else ""))
            if c.get("balcony") else None,
            "occ": c.get("maximum_occupancy"), "beds": beds, "extra": extra or None,
            "dir": True if c.get("bed_in_direction_of_travel") else None,
            "near": near or None, "conn": True if c.get("connecting_door") else None,
            "bath2": True if c.get("two_bathrooms") else None,
            "wardrobe": True if c.get("walk_in_wardrobe") else None,
            "plan": plan or None}.items() if v is not None}
    return out


def _refills(con, runs, days=7):
    """Nachschub-Ereignisse der letzten Tage: Anstieg freier Kabinen je Kategorie."""
    cutoff = (date.fromisoformat(runs[-1]["fetched_at"][:10]) - timedelta(days=days)).isoformat()
    rs = [r for r in runs if r["fetched_at"][:10] >= cutoff]
    events, prev = [], None
    for r in rs:
        cur = {c["code"]: c["free"] for c in con.execute("SELECT code, free FROM categories WHERE run_id=?", (r["id"],))}
        if prev is not None:
            for code, n in cur.items():
                if code in prev and n - prev[code] >= NACHSCHUB_MIN:
                    events.append({"ts": r["fetched_at"][:16], "code": code, "plus": n - prev[code]})
        prev = cur
    return events[::-1]


def _changes(con, runs, days=14):
    """Änderungsprotokoll einer Reise: zwischen aufeinanderfolgenden Abrufen je Kategorie
    Preis (ALL IN/PREMIUM) rauf/runter, ausgebucht, wieder buchbar, Nachschub."""
    cutoff = (date.fromisoformat(runs[-1]["fetched_at"][:10]) - timedelta(days=days)).isoformat()
    rs = [r for r in runs if r["fetched_at"][:10] >= cutoff]
    out, prev = [], None
    for r in rs:
        cur = _cats(con, r["id"])
        if prev is not None:
            ts = r["fetched_at"][:16]
            for code, c in cur.items():
                o = prev.get(code)
                if not o:
                    continue
                if o["free"] > 0 and c["free"] == 0 and not c["guarantee"]:
                    out.append({"ts": ts, "code": code, "kind": "ausgebucht"})
                elif o["free"] == 0 and c["free"] > 0:
                    out.append({"ts": ts, "code": code, "kind": "wieder buchbar", "val": c["free"]})
                elif c["free"] - o["free"] >= NACHSCHUB_MIN:
                    out.append({"ts": ts, "code": code, "kind": "Nachschub", "val": c["free"] - o["free"]})
                for f, lab in (("price_premium_ai", "ALL IN"), ("price_premium", "PREMIUM")):
                    if o[f] and c[f] and abs(c[f] - o[f]) >= 1:
                        out.append({"ts": ts, "code": code, "kind": "Preis", "tariff": lab,
                                    "old": o[f], "new": c[f]})
        prev = cur
    return out[::-1]


def trip_data(con, journey, focus=None):
    runs = _all_runs(con, journey)
    if not runs:
        return None
    latest = runs[-1]
    series = []
    for r in runs:
        cats = _cats(con, r["id"])
        free, prem, clas, allin = {}, {}, {}, {}
        for c in cats.values():
            g = _group(c["grp"])
            free[g] = free.get(g, 0) + c["free"]
            if c["price_premium"]:
                prem[g] = min(prem.get(g, 1e9), c["price_premium"])
            if c["price_classic"]:
                clas[g] = min(clas.get(g, 1e9), c["price_classic"])
            if c["price_premium_ai"]:
                allin[g] = min(allin.get(g, 1e9), c["price_premium_ai"])
        fc = cats.get(focus) or {}
        series.append({"day": r["fetched_at"][:10], "ts": r["fetched_at"][:16],
                       "free_total": r["free_total"],
                       "free": free, "premium": prem, "classic": clas, "allin": allin,
                       "focus_free": fc.get("free"), "focus_allin": fc.get("price_premium_ai")})

    cur = _cats(con, latest["id"])
    prev = _cats(con, runs[-2]["id"]) if len(runs) > 1 else {}
    week_day = (date.fromisoformat(latest["fetched_at"][:10]) - timedelta(days=7)).isoformat()
    week_run = next((r for r in reversed(runs) if r["fetched_at"][:10] <= week_day), None)
    week = _cats(con, week_run["id"]) if week_run else {}

    latest_cabins, cabin_attrs = {}, {}
    for r in con.execute("SELECT code, number, attrs FROM cabins WHERE run_id=? ORDER BY number", (latest["id"],)):
        latest_cabins.setdefault(r["code"], []).append(r["number"])
        if r["attrs"]:
            cabin_attrs[r["number"]] = r["attrs"].split(",")
    master = _ship_master(journey)
    LAGE = {"Front": "vorne", "Mid": "Mitte", "Back": "hinten", "Backbord": "Backbord", "Steuerbord": "Steuerbord"}
    cabin_info = {}
    for nums in latest_cabins.values():
        for n in nums:
            i = dict(master.get(n, {}))
            lage = [LAGE.get(a, a) for a in cabin_attrs.get(n, [])]
            if lage:
                i["lage"] = lage
            if i:
                cabin_info[n] = i

    table = []
    for code, c in sorted(cur.items(), key=lambda kv: ([g for g, _ in GROUPS].index(_group(kv[1]["grp"]))
                                                      if _group(kv[1]["grp"]) in dict(GROUPS) else 99, kv[0])):
        if not c["free"] and not c["guarantee"] and not c["price_premium"] and not c["price_classic"] \
                and not prev.get(code, {}).get("free"):
            continue
        table.append({
            "code": code, "group": dict(GROUPS).get(_group(c["grp"]), c["grp"]), "name": c["name"],
            "guarantee": bool(c["guarantee"]), "free": c["free"], "cabins": latest_cabins.get(code, []),
            "d1": c["free"] - prev[code]["free"] if code in prev else None,
            "d7": c["free"] - week[code]["free"] if code in week else None,
            "premium": c["price_premium"], "classic": c["price_classic"],
            "dp": (c["price_premium"] - prev[code]["price_premium"])
            if code in prev and c["price_premium"] and prev[code]["price_premium"] else None,
            "dc": (c["price_classic"] - prev[code]["price_classic"])
            if code in prev and c["price_classic"] and prev[code]["price_classic"] else None,
            "allin": c["price_premium_ai"],
            "light": c["price_light"] if "price_light" in c else None,
            "da": (c["price_premium_ai"] - prev[code]["price_premium_ai"])
            if code in prev and c["price_premium_ai"] and prev[code]["price_premium_ai"] else None,
        })

    booked = released = None
    if len(runs) > 1:
        a, b = _cabin_set(con, runs[-2]["id"]), _cabin_set(con, latest["id"])
        booked, released = len(a - b), len(b - a)

    last_error = con.execute("SELECT fetched_at, error FROM runs WHERE journey=? AND ok=0 AND fetched_at>?"
                             " ORDER BY fetched_at DESC LIMIT 1", (journey, latest["fetched_at"])).fetchone()
    return {
        "journey": journey, "label": latest["label"], "title": latest["title"], "ship": latest["ship"],
        "start": latest["start"], "end": latest["end"], "from": latest["from_city"],
        "fetched_at": latest["fetched_at"], "ship_total": latest["ship_total"],
        "free_total": latest["free_total"], "first_day": runs[0]["fetched_at"][:10],
        "first_free": runs[0]["free_total"],
        "prev_free": runs[-2]["free_total"] if len(runs) > 1 else None,
        "booked": booked, "released": released, "series": series, "table": table,
        "error": dict(last_error) if last_error else None,
        "map_url": latest["map_url"] if "map_url" in latest.keys() else None,
        "focus": next(({"code": focus, "name": r["name"], "group": r["group"]}
                       for r in table if r["code"] == focus), None),
        # Freie Kabinennummer -> Kabinentyp-Gruppe (für den 3D-Deckplan)
        "free_cabins": {num: _group(c["grp"]) for code2, c in cur.items()
                        for num in latest_cabins.get(code2, [])},
        "free_cat": {num: code2 for code2 in latest_cabins for num in latest_cabins[code2]},
        "cabin_info": cabin_info,
        "offer": json.loads(latest["offer"]) if "offer" in latest.keys() and latest["offer"] else None,
        "refills": _refills(con, runs),
        "changes": _changes(con, runs),
        "tariffs": [dict(t, flights=json.loads(t["flights"]) if t["flights"] else None) for t in con.execute(
            "SELECT cabin_type, tariff, cabin, flight, flight_from, rabat, notes, campaigns, flights FROM tariffs"
            " WHERE run_id=? ORDER BY cabin_type, cabin", (latest["id"],))],
        # Preise der hinterlegten Wunschkategorie (z. B. IA) je Tarif, für die Tarif-Tabelle
        "focus_prices": ({"type": (cur[focus]["grp"] or "")[:1], "code": focus, "prices": {k: v for k, v in {
            "PREMIUM": cur[focus]["price_premium"], "PREMIUM ALL IN": cur[focus]["price_premium_ai"],
            "LIGHT": cur[focus].get("price_light") if hasattr(cur[focus], "get") else None,
            "CLASSIC ALL IN": cur[focus].get("price_classic_ai") if hasattr(cur[focus], "get") else None,
            "COMFORT ALL IN": cur[focus].get("price_comfort_ai") if hasattr(cur[focus], "get") else None}.items() if v}}
            if focus and focus in cur else None),
        "itinerary": json.loads(latest["itinerary"]) if "itinerary" in latest.keys() and latest["itinerary"] else None,
        "region": latest["region"] if "region" in latest.keys() else None,
        "cabin_choice": None if "cabin_choice" not in latest.keys() or latest["cabin_choice"] is None
        else bool(latest["cabin_choice"]),
    }


def _fetch_maps(data, dest_dir):
    """Hält die Routenkärtchen in einem persistenten Cache (runtime/maps) und kopiert sie
    neben die erzeugte HTML. So hängt der Deploy nicht am Netz und nichts fehlt sporadisch."""
    import shutil
    cache = store.DB.parent.parent / "maps"   # persistenter Cache im Laufzeitordner
    cache.mkdir(parents=True, exist_ok=True)
    out = dest_dir / "maps"
    for t in data:
        t["map_file"] = None
        if not t.get("map_url"):
            continue
        cf = cache / f"{t['journey']}.jpg"
        if not cf.exists() or cf.stat().st_size < 1000:
            # Bildname aus der URL ableiten und immer vom scene7-CDN laden (die /content/dam-
            # Variante liegt hinter aida.de/Akamai und ist nicht direkt abrufbar).
            name = t["map_url"].split("?")[0].rstrip("/").split("/")[-1].rsplit(".", 1)[0]
            url = f"https://s7e5a.scene7.com/is/image/aida/{name}?wid=1000&fmt=jpeg"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data_bytes = r.read()
                if len(data_bytes) < 1000:
                    raise ValueError(f"Antwort zu klein ({len(data_bytes)} Bytes)")
                cf.write_bytes(data_bytes)
            except Exception as e:
                print(f"Routenbild {t['journey']} nicht geladen: {e}")
                continue
        out.mkdir(parents=True, exist_ok=True)
        dst = out / f"{t['journey']}.jpg"
        if cf.resolve() != dst.resolve():
            shutil.copy(cf, dst)
        # Versions-Query gegen alte Browser-/Edge-Caches (ändert sich nur bei neuem Bild)
        t["map_file"] = f"maps/{t['journey']}.jpg?v={cf.stat().st_size}"


def _fetch_decks(t, dest_dir):
    """Lädt die Deckplan-SVGs des Schiffs (scene7) in einen persistenten Cache und neben die
    HTML. Schiffscode = die ersten zwei Buchstaben der Reise-ID (CO->co, NO->no)."""
    import shutil
    code = t["journey"][:2].lower()
    t["deck_code"] = code
    t["decks"] = []
    # Deckpläne liegen versioniert im Daten-Repo (Archiv aller Schiffe), sonst im Laufzeit-Cache
    daten = store.DB.parent.parent / "daten"
    cache = (daten / "decks" / code) if daten.is_dir() else (store.DB.parent.parent / "decks" / code)
    cache.mkdir(parents=True, exist_ok=True)
    manifest = cache / "decks.json"
    if manifest.exists():
        decks = json.loads(manifest.read_text())
    else:
        decks = []
        for n in range(1, 21):
            url = f"https://s7e5a.scene7.com/is/content/aida/deck-{n}-{code}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    b = r.read()
            except Exception:
                continue
            if b[:5] == b"<?xml" and len(b) > 5000:
                (cache / f"deck-{n}.svg").write_bytes(b)
                decks.append(n)
        manifest.write_text(json.dumps(decks))
    out = dest_dir / "decks" / code
    out.mkdir(parents=True, exist_ok=True)
    for n in decks:
        src = cache / f"deck-{n}.svg"
        if src.exists():
            dst = out / f"deck-{n}.svg"
            if src.resolve() != dst.resolve():
                shutil.copy(src, dst)
    t["decks"] = decks
    # Deck-Bilder aus den PDF-Plänen + Bild-Seitenverhältnis
    import struct
    img_manifest = cache / "img.json"
    t["deck_img"], t["deck_aspect"] = {}, {}
    if img_manifest.exists():
        imgs = json.loads(img_manifest.read_text())
        for n, fn in imgs.items():
            src = cache / fn
            if src.exists():
                dst = out / fn
                if src.resolve() != dst.resolve():
                    shutil.copy(src, dst)
                # Versions-Tag aus dem Inhalt: sonst zeigt der Browser ein altes Bild aus dem
                # Cache, das nicht mehr zu den Kabinenpositionen passt
                ver = hashlib.md5(src.read_bytes()).hexdigest()[:8]
                t["deck_img"][int(n)] = f"decks/{code}/{fn}?v={ver}"
                with open(src, "rb") as fh:
                    fh.read(16); w, h = struct.unpack(">II", fh.read(8))
                t["deck_aspect"][int(n)] = round(w / h, 4)
    # Positionen der freien Kabinen aus dem PDF (gleiche Quelle wie das Bild -> exakt deckungsgleich)
    cp_file = cache / "cabinpos.json"
    t["cabin_pos"] = {}
    if cp_file.exists():
        cp = json.loads(cp_file.read_text())
        free = t.get("free_cabins", {})
        for deck, cabs in cp.items():
            sel = {num: uv for num, uv in cabs.items() if num in free}
            if sel:
                t["cabin_pos"][int(deck)] = sel


def _people_map():
    """E-Mail -> Anzeigename, aus daten/people.json (im Daten-Repo pflegbar)."""
    f = store.DB.parent.parent / "daten" / "people.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    return {}


def _display_name(email, people):
    if not email:
        return people.get("_default", "Ohne Zuordnung")   # Name für Reisen ohne Ersteller (people.json)
    if email in people:
        return people[email]
    local = email.split("@")[0].replace(".", " ").replace("_", " ")
    return local.title()


def build(con=None):
    con = con or store.connect()
    journeys = [r["journey"] for r in con.execute(
        "SELECT journey FROM runs GROUP BY journey ORDER BY min(start)")]
    focus, added_by = {}, {}
    trips_file = store.live_trips_file()
    if trips_file.exists():
        import aida
        for t in json.loads(trips_file.read_text()):
            jid = aida.parse_trip_url(t["url"])["journey"]
            focus[jid] = t.get("focus")
            added_by[jid] = t.get("added_by")
    if focus:   # nur Reisen der aktiven Liste zeigen (entfernte bleiben in der Historie)
        journeys = [j for j in journeys if j in focus]
    people = _people_map()
    data = [d for d in (trip_data(con, j, focus.get(j)) for j in journeys) if d]
    for t in data:
        t["added_by"] = _display_name(added_by.get(t["journey"]), people)
    _fetch_maps(data, OUT.parent)
    for t in data:
        _fetch_decks(t, OUT.parent)
        vf = store.DB.parent.parent / "daten" / "venues" / f"{t['deck_code']}.json"
        t["venues"] = json.loads(vf.read_text())["decks"] if vf.exists() else {}
    daten = store.DB.parent.parent / "daten"
    # Häfen: Koordinaten für die Routenkarten (eigene Reisen eingebettet, Katalog lädt nach)
    pf = daten / "ports.json"
    ports = json.loads(pf.read_text()) if pf.exists() else {}
    for t in data:
        t["port_coords"] = {p["code"]: [ports[p["code"]]["lat"], ports[p["code"]]["lon"]]
                            for p in t.get("itinerary") or [] if "lat" in ports.get(p["code"], {})}
    # Katalog (wöchentlich) als eigene Datei neben die Seite, wird erst bei Bedarf geladen
    cat_meta, cat_changes = None, None
    cf = daten / "catalog.json"
    if cf.exists():
        import shutil
        cat = json.loads(cf.read_text())
        cat_meta = {"fetched_at": cat["fetched_at"], "count": len(cat["trips"])}
        # Freie Kabinen der nächsten 4 Monate (fleet/<id>.json) in die Katalogdatei der Seite
        fdir = daten / "fleet"
        nfleet = 0
        for t in cat["trips"]:
            ff = fdir / f"{t['id']}.json"
            if not ff.exists():
                continue
            h = json.loads(ff.read_text())
            days = sorted(h.get("counts") or {})
            if not days:
                continue
            if h.get("choice") is False:
                t["fleet"] = {"date": days[-1], "choice": False}
                nfleet += 1
                continue
            cur_c = h["counts"][days[-1]]
            prev_c = h["counts"][days[-2]] if len(days) > 1 else None
            mv = (h.get("moves") or {}).get(days[-1]) or {}
            t["fleet"] = {"date": days[-1], "free": sum(cur_c.values()), "cats": cur_c,
                          "prev": sum(prev_c.values()) if prev_c else None,
                          "plus": len(mv.get("+") or []), "minus": len(mv.get("-") or []),
                          "capped": [k for k, v in cur_c.items() if v >= 100],
                          "series": [[dd, sum(h["counts"][dd].values())] for dd in days[-30:]]}
            nfleet += 1
        cat_meta["fleet"] = nfleet
        (OUT.parent / "catalog.json").write_text(json.dumps(cat, ensure_ascii=False, separators=(",", ":")))
        if pf.exists():
            (OUT.parent / "ports.json").write_text(json.dumps(
                {c: [v["lat"], v["lon"]] for c, v in ports.items() if "lat" in v}, separators=(",", ":")))
        chs = sorted((daten / "changes").glob("katalog-*.json")) if (daten / "changes").exists() else []
        if chs:
            cat_changes = json.loads(chs[-1].read_text())
            byid = {t["id"]: t for t in cat["trips"]}
            for k in ("new", "gone"):
                cat_changes[k] = [{"id": j, "title": byid.get(j, {}).get("title"), "ship": byid.get(j, {}).get("ship"),
                                   "start": byid.get(j, {}).get("start")} for j in cat_changes[k]]
            for p in cat_changes["price"]:
                b = byid.get(p["id"], {})
                p.update(title=b.get("title"), ship=b.get("ship"), start=b.get("start"))
    (OUT.parent / "tracked.json").write_text(json.dumps(list(focus) or journeys))
    html = TEMPLATE.replace("__DATA__", json.dumps({"trips": data, "groups": GROUPS, "tracked": list(focus) or journeys,
                                                    "catalog": cat_meta, "catalog_changes": cat_changes},
                                                   ensure_ascii=False))
    OUT.write_text(html)
    return OUT


TEMPLATE = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AIDA Buchungslage</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<style>
:root {
  color-scheme: light;
  --bg: #f4f3f0; --surface: #fcfcfb; --border: #e3e1db;
  --text: #0b0b0b; --text-2: #52514e; --muted: #8a8984; --grid: #ecebe7;
  --up: #b42318; --down: #067647;
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s4: #eda100;
  --s5: #e87ba4; --s6: #008300; --s7: #4a3aa7; --s8: #e34948;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #111110; --surface: #1a1a19; --border: #2e2e2c;
    --text: #ffffff; --text-2: #c3c2b7; --muted: #8f8e88; --grid: #2a2a28;
    --up: #f97066; --down: #47cd89;
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
    --s5: #d55181; --s6: #008300; --s7: #9085e9; --s8: #e66767;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #111110; --surface: #1a1a19; --border: #2e2e2c;
  --text: #ffffff; --text-2: #c3c2b7; --muted: #8f8e88; --grid: #2a2a28;
  --up: #f97066; --down: #47cd89;
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
  --s5: #d55181; --s6: #008300; --s7: #9085e9; --s8: #e66767;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
main { max-width: 1400px; margin: 0 auto; padding: 28px 16px 60px; }
h1 { font-size: 22px; margin: 0 0 4px; }
.sub { color: var(--text-2); margin: 0; }
.topbar { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; margin-bottom: 24px; }
.timer { text-align: right; border: 1px solid var(--border); border-radius: 10px; padding: 8px 14px;
  background: var(--surface); white-space: nowrap; }
.timer .tlabel { color: var(--text-2); font-size: 12px; }
.timer .tval { font-size: 20px; font-weight: 650; font-variant-numeric: tabular-nums; }
.persongroup { margin-bottom: 10px; }
.persongroup > h2 { font-size: 15px; color: var(--text-2); font-weight: 600; margin: 22px 0 12px;
  display: flex; align-items: center; gap: 8px; }
.persongroup > h2::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--s1); }
.card .by { position: absolute; top: 12px; right: 12px; background: rgba(0,0,0,.55); color: #fff;
  font-size: 11.5px; font-weight: 600; padding: 2px 8px; border-radius: 20px; }
.card { position: relative; }
.card .rm { position: absolute; top: 12px; left: 12px; width: 30px; height: 30px; border-radius: 50%;
  background: rgba(0,0,0,.55); color: #fff; display: grid; place-items: center; font-size: 15px;
  line-height: 1; opacity: 0; transition: opacity .15s; }
.card:hover .rm, .card:focus-within .rm { opacity: 1; }
.card .rm:hover { background: #c0392b; }
@media (hover: none) { .card .rm { opacity: .85; } }
.trip { background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  padding: 22px; margin-bottom: 28px; }
.trip h2 { font-size: 19px; margin: 0 0 2px; }
.meta { color: var(--text-2); font-size: 14px; margin-bottom: 18px; }
.err { background: color-mix(in srgb, var(--up) 12%, transparent); border-left: 3px solid var(--up);
  padding: 8px 12px; border-radius: 6px; margin-bottom: 16px; font-size: 14px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 22px; }
.tile { border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
.tile .k { color: var(--text-2); font-size: 13px; }
.tile .v { font-size: 26px; font-weight: 650; font-variant-numeric: tabular-nums; }
.tile .d { font-size: 13px; color: var(--muted); }
.charts { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-bottom: 22px; }
@media (max-width: 760px) { .charts { grid-template-columns: 1fr; } }
.chart h3, .tbl h3 { font-size: 14px; font-weight: 600; margin: 0 0 8px; }
.chart .box { position: relative; height: 260px; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 13px; color: var(--text-2); margin-top: 8px; }
.legend span::before { content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 3px;
  background: var(--c); margin-right: 5px; vertical-align: -1px; }
.tbl { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 14px; font-variant-numeric: tabular-nums; }
th, td { padding: 6px 8px; border-bottom: 1px solid var(--border); text-align: right; white-space: nowrap; }
th { color: var(--text-2); font-weight: 600; font-size: 13px; }
th:nth-child(-n+3), td:nth-child(-n+3) { text-align: left; }
td.code { font-weight: 600; }
.up { color: var(--up); } .down { color: var(--down); } .mut { color: var(--muted); }
.note { color: var(--muted); font-size: 13px; margin-top: 10px; }
.focus { border: 2px solid var(--s1); border-radius: 12px; padding: 16px 18px; margin-bottom: 22px; }
.focus h3 { font-size: 16px; margin: 0 0 12px; }
.focus .tiles { margin-bottom: 14px; }
.focus .chart .box { height: 200px; }
.sect { font-size: 15px; font-weight: 600; margin: 4px 0 12px; }
tr.hl td { background: color-mix(in srgb, var(--s1) 10%, transparent); }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 16px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px;
  cursor: pointer; text-align: left; font: inherit; color: inherit; transition: border-color .12s, transform .12s; }
.card:hover { border-color: var(--s1); transform: translateY(-2px); }
.card h2 { font-size: 18px; margin: 0 0 3px; }
.card .meta { font-size: 13px; margin-bottom: 14px; }
.card .cstat { display: flex; justify-content: space-between; align-items: baseline;
  padding: 7px 0; border-top: 1px solid var(--border); }
.card .cstat .k { color: var(--text-2); font-size: 13px; }
.card .cstat .v { font-weight: 650; font-variant-numeric: tabular-nums; }
.card .focusbadge { display: inline-block; background: color-mix(in srgb, var(--s1) 12%, transparent);
  color: var(--s1); border-radius: 6px; padding: 2px 8px; font-size: 12.5px; font-weight: 600; margin-bottom: 12px; }
.card .arrow { color: var(--muted); font-size: 13px; margin-top: 12px; }
.back { background: none; border: none; color: var(--s1); font: inherit; cursor: pointer;
  padding: 0; margin-bottom: 14px; display: inline-flex; align-items: center; gap: 4px; }
.back:hover { text-decoration: underline; }
.cardmap { width: 100%; aspect-ratio: 4/3; object-fit: cover; border-radius: 8px;
  background: #eef2f7; margin-bottom: 14px; }
.routemap { display: block; width: 100%; max-width: 560px; margin: 0 0 22px; border-radius: 10px;
  border: 1px solid var(--border); background: #eef2f7; }
.routecap { color: var(--text-2); font-size: 13px; margin: -16px 0 22px; }
.addcard { display: flex; align-items: center; justify-content: center; gap: 8px;
  border: 2px dashed var(--border); border-radius: 12px; padding: 20px; cursor: pointer;
  background: none; font: inherit; color: var(--text-2); min-height: 120px; }
.addcard:hover { border-color: var(--s1); color: var(--s1); }
.addcard .plus { font-size: 22px; line-height: 1; }
.dlg { border: none; border-radius: 12px; padding: 0; max-width: 440px; width: calc(100% - 32px);
  background: var(--surface); color: var(--text); box-shadow: 0 10px 40px rgba(0,0,0,.25); }
.dlg::backdrop { background: rgba(0,0,0,.45); }
.dlg form { padding: 22px; }
.dlg h3 { margin: 0 0 4px; font-size: 17px; }
.dlg p { margin: 0 0 16px; color: var(--text-2); font-size: 13px; }
.dlg label { display: block; font-size: 13px; color: var(--text-2); margin: 12px 0 4px; }
.dlg input { width: 100%; padding: 9px 11px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--bg); color: var(--text); font: inherit; }
.dlg .row { display: flex; gap: 10px; justify-content: flex-end; margin-top: 20px; }
.dlg button { padding: 9px 16px; border-radius: 8px; border: 1px solid var(--border); font: inherit;
  cursor: pointer; background: var(--bg); color: var(--text); }
.dlg button.primary { background: var(--s1); border-color: var(--s1); color: #fff; }
.dlg .msg { font-size: 13px; margin-top: 12px; min-height: 18px; }
.pending { background: color-mix(in srgb, var(--s4) 14%, transparent); border-radius: 10px;
  padding: 12px 14px; margin-bottom: 18px; font-size: 14px; }
.tabs { display: flex; gap: 4px; margin: -6px 0 20px; border-bottom: 1px solid var(--border); }
.tabs a { padding: 9px 14px; color: var(--text-2); text-decoration: none; font-weight: 600; font-size: 14px;
  border-bottom: 2px solid transparent; margin-bottom: -1px; }
.tabs a.on { color: var(--s1); border-bottom-color: var(--s1); }
.maps { display: grid; grid-template-columns: 1fr; gap: 14px; margin: 0 0 18px; }
.maps figure { margin: 0; min-width: 0; }
.maps .routemap { max-width: none; margin: 0 0 6px; aspect-ratio: 4/3; object-fit: cover; }
.maps .leafmap { max-width: none; height: auto; aspect-ratio: 4/3; margin: 0 0 6px; }
.maps .routecap { margin: 0; }
@media (min-width: 900px) { .maps { grid-template-columns: 1fr 1fr; } }
.leafmap { width: 100%; max-width: 760px; height: 380px; border-radius: 10px; border: 1px solid var(--border);
  margin: 0 0 8px; z-index: 0; }
.daymark { background: var(--s1); color: #fff; border-radius: 11px; font-size: 11px; font-weight: 700;
  padding: 2px 6px; white-space: nowrap; box-shadow: 0 1px 4px rgba(0,0,0,.35); text-align: center; }
.warnbox { background: color-mix(in srgb, var(--up) 12%, transparent); border-radius: 10px; padding: 10px 14px;
  margin: 0 0 18px; font-size: 14px; }
.tmx { border-collapse: collapse; font-size: 13px; margin: 0 0 6px; }
.tmx th, .tmx td { padding: 6px 9px; border-top: 1px solid var(--border); text-align: right; white-space: nowrap; }
.tmx th:first-child, .tmx td:first-child { text-align: left; }
.tmx td .f { display: block; color: var(--text-2); font-size: 11.5px; font-weight: 400; }
.tmx td .f.ff { color: var(--s4); }
.tmx td.best { font-weight: 700; color: var(--down); }
.tmxwrap { overflow-x: auto; margin-bottom: 18px; }
.filters { display: flex; flex-wrap: wrap; gap: 10px; margin: 0 0 14px; align-items: end; }
.filters label { display: flex; flex-direction: column; font-size: 12px; color: var(--text-2); gap: 3px; }
.filters input, .filters select { padding: 7px 9px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface); color: var(--text); font: inherit; font-size: 14px; }
.filters input[type=search] { min-width: 220px; }
.filters input.num { width: 90px; }
.krow { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px;
  margin-bottom: 8px; }
.krow .top { display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; cursor: pointer; }
.krow .top h3 { margin: 0; font-size: 15.5px; flex: 1 1 260px; }
.krow .m { color: var(--text-2); font-size: 13px; }
.krow .p { font-weight: 700; font-variant-numeric: tabular-nums; }
.krow .more { margin-top: 10px; }
.mark { border: 1px solid var(--s1); color: var(--s1); background: none; border-radius: 8px; padding: 5px 10px;
  font: inherit; font-size: 13px; font-weight: 600; cursor: pointer; white-space: nowrap; }
.mark.on { background: var(--s1); color: #fff; cursor: default; }
.kmeta { color: var(--text-2); font-size: 13px; margin: 0 0 12px; }
.chg { width: auto; max-width: 100%; border-collapse: collapse; font-size: 13.5px; margin: 0 0 22px; }
.chg td { padding-right: 18px !important; }
.chg td { padding: 6px 8px; border-top: 1px solid var(--border); vertical-align: top; }
.chg td.t { color: var(--text-2); white-space: nowrap; font-variant-numeric: tabular-nums; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 14px; }
.chip { display: inline-block; border-radius: 6px; padding: 2px 8px; font-size: 12.5px; font-weight: 600;
  background: color-mix(in srgb, var(--s4) 16%, transparent); color: var(--text); }
.chip.camp { background: color-mix(in srgb, var(--s2) 18%, transparent); }
.chip.refill { background: color-mix(in srgb, var(--down) 18%, transparent); color: var(--down); }
.offer { display: flex; flex-wrap: wrap; gap: 6px 18px; align-items: baseline; margin: 0 0 10px; font-size: 14px; }
.offer .from { font-size: 18px; font-weight: 700; }
.itin { width: 100%; max-width: 560px; border-collapse: collapse; font-size: 13.5px; margin: 0 0 22px; }
.itin td { padding: 5px 8px; border-top: 1px solid var(--border); }
.itin td.t { color: var(--text-2); font-variant-numeric: tabular-nums; white-space: nowrap; }
.itin tr.sea td { color: var(--muted); font-style: italic; }
.refills { margin: 0 0 22px; font-size: 14px; }
.refills li { margin: 2px 0; }
.cabgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 6px; margin-top: 8px; }
.cab { border: 1px solid var(--border); border-radius: 8px; padding: 6px 8px; font-size: 12.5px; background: var(--bg); }
.cab b { font-size: 13.5px; }
.cab .x { color: var(--text-2); }
.cab a { color: var(--s1); }
.freebtn { background: none; border: none; color: var(--s1); font: inherit; cursor: pointer;
  padding: 0; font-variant-numeric: tabular-nums; }
.freebtn:hover { text-decoration: underline; }
tr.cabrow td { background: color-mix(in srgb, var(--s1) 6%, transparent); white-space: normal;
  line-height: 1.7; font-size: 13px; }
@media (min-width: 1000px) { .charts { grid-template-columns: 1fr 1fr; } }
@media (min-width: 1500px) { .cards { grid-template-columns: repeat(3, 1fr); } }
.deckbtn { background: #0a1020; border: 1px solid var(--s1); color: var(--s1); border-radius: 8px;
  font: inherit; font-weight: 600; cursor: pointer; padding: 8px 14px; margin: 0 0 18px 12px; }
.deckbtn:hover { background: var(--s1); color: #fff; }
.rmbtn { background: none; border: 1px solid var(--border); color: var(--text-2); border-radius: 8px;
  font: inherit; cursor: pointer; padding: 8px 14px; margin: 0 0 18px 12px; float: right; }
.rmbtn:hover { border-color: var(--up); color: var(--up); }
.deck3d { position: fixed; inset: 0; z-index: 100; background: #05070d; display: flex; flex-direction: column; }
.deck3d[hidden] { display: none; }
.d3d-bar { display: flex; align-items: center; gap: 16px; padding: 10px 16px; color: #cfe6ff;
  background: #080b14; border-bottom: 1px solid #16304d; flex-wrap: wrap; }
.d3d-title { font-weight: 650; }
.d3d-legend { display: flex; flex-wrap: wrap; gap: 6px 12px; font-size: 13px; }
.d3d-legend button { background: none; border: none; color: #cfe6ff; font: inherit; cursor: pointer;
  display: inline-flex; align-items: center; gap: 5px; opacity: 1; }
.d3d-legend button.off { opacity: .35; text-decoration: line-through; }
.d3d-legend .dot { width: 11px; height: 11px; border-radius: 3px; box-shadow: 0 0 6px currentColor; }
.d3d-close { margin-left: auto; background: #122038; border: 1px solid #27496e; color: #cfe6ff;
  border-radius: 8px; padding: 7px 14px; font: inherit; cursor: pointer; }
.d3d-close:hover { background: #1b2f4f; }
.d3d-main { flex: 1; display: flex; min-height: 0; }
.d3d-canvas { flex: 1; position: relative; min-height: 0; overflow: hidden; }
.d3d-canvas canvas { display: block; }
.d3d-places { width: 290px; overflow-y: auto; background: #080d16; border-left: 1px solid #16304d;
  padding: 12px 14px; color: #cfe6ff; font-size: 13px; }
.d3d-places h4 { margin: 0 0 10px; font-size: 13px; color: #7fa8d8; text-transform: uppercase; letter-spacing: .04em; }
.d3d-places .deck { margin-bottom: 12px; }
.d3d-places .deck b { color: #eaf4ff; }
.d3d-places .deck .v { color: #9fc0e4; line-height: 1.6; }
.d3d-places .deck .v a { color: #9fc0e4; cursor: pointer; }
.d3d-places .deck .v a:hover { color: #ffd36b; text-decoration: underline; }
.d3d-places.hidden-panel { display: none; }
.d3d-decklabel { position: absolute; transform: translate(-50%, -50%); color: #bfe6ff; font-size: 11px;
  font-weight: 600; background: rgba(6,12,22,.6); border: 1px solid #1f4a6e; padding: 1px 6px;
  border-radius: 10px; pointer-events: none; white-space: nowrap; }
.d3d-decklabel.deck { pointer-events: auto; cursor: pointer; }
.d3d-decklabel.deck.sel { color: #fff; border-color: #6d7cff; background: rgba(40,50,140,.75); }
@media (max-width: 760px) { .d3d-places { display: none; } }
.d3d-tip { position: fixed; pointer-events: none; background: rgba(8,14,26,.95); color: #eaf4ff;
  border: 1px solid #27496e; border-radius: 8px; padding: 7px 10px; font-size: 13px; z-index: 110;
  box-shadow: 0 6px 20px rgba(0,0,0,.5); }
.d3d-hint { position: fixed; bottom: 12px; left: 50%; transform: translateX(-50%); color: #6f8bb0;
  font-size: 12.5px; background: rgba(8,14,26,.7); padding: 5px 12px; border-radius: 20px; }
.d3d-loading { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  color: #7fa8d8; font-size: 15px; }
</style>
</head>
<body>
<main>
<div class="topbar">
  <div><h1>AIDA Buchungslage</h1><p class="sub" id="sub"></p></div>
  <div class="timer" id="timer" title="Zeit bis zur nächsten automatischen Abfrage">
    <div class="tlabel">Nächste Abfrage in</div><div class="tval" id="timerval">–</div></div>
</div>
<nav class="tabs">
  <a href="#" data-v="list">Meine Reisen</a>
  <a href="#katalog" data-v="katalog">Katalog</a>
  <a href="#aenderungen" data-v="aend">Änderungen</a>
</nav>
<div id="list"></div>
<div id="detail"></div>
<div id="katalog" style="display:none"></div>
<div id="aend" style="display:none"></div>
</main>
<div id="deck3d" class="deck3d" hidden>
  <div class="d3d-bar">
    <span class="d3d-title" id="d3d-title"></span>
    <span class="d3d-legend" id="d3d-legend"></span>
    <button class="d3d-close" onclick="closeDeck3d()">✕ Schließen</button>
  </div>
  <div class="d3d-main">
    <div class="d3d-canvas" id="d3d-canvas"></div>
  </div>
  <div class="d3d-tip" id="d3d-tip" hidden></div>
  <div class="d3d-hint">Ziehen = drehen · Scrollen = zoomen · Deck anklicken = herausfahren · Kabine antippen = Info</div>
</div>
<script>
const DATA = __DATA__;
window.AIDA_DATA = DATA;
window.AIDA_GROUPS = DATA.groups;
const GROUPS = DATA.groups;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const eur = v => v == null ? "–" : v.toLocaleString("de-DE", {maximumFractionDigits: 0}) + " €";
const d = s => s.split("-").reverse().join(".");
const sign = (v, unit = "", invert = false) => {
  if (v == null) return '<span class="mut">–</span>';
  if (v === 0) return '<span class="mut">±0' + unit + '</span>';
  const cls = (v > 0) !== invert ? "up" : "down";
  return `<span class="${cls}">${v > 0 ? "+" : "−"}${Math.abs(Math.round(v)).toLocaleString("de-DE")}${unit}</span>`;
};
const charts = [];

function itinerary(t) {
  if (t.itinerary && t.itinerary.length) return itineraryFromDetail(t.itinerary);
  const ports = (t.offer && t.offer.ports) || [];
  if (!ports.length) return "";
  const hm = s => s ? s.slice(11, 16) : "";
  const rows = [];
  let sea = 0;
  const flush = () => { if (sea) rows.push(`<tr class="sea"><td class="t"></td><td>${sea > 1 ? sea + " Seetage" : "Seetag"}</td><td></td><td></td></tr>`); sea = 0; };
  ports.forEach(p => {
    if (p.code === "SEE" || !p.name) { sea++; return; }
    flush();
    const day = (p.arr || p.dep || "").slice(0, 10);
    const name = p.name.toLowerCase().replace(/(^|[\s/(-])\S/g, m => m.toUpperCase());
    rows.push(`<tr><td class="t">${day ? d(day).slice(0, 6) : ""}</td><td>${name}</td>
      <td class="t">${p.arr ? "an " + hm(p.arr) : ""}</td><td class="t">${p.dep ? "ab " + hm(p.dep) : ""}</td></tr>`);
  });
  flush();
  return `<div class="sect">Reiseverlauf</div><table class="itin">${rows.join("")}</table>`;
}
function titleCase(n) { return (n || "").toLowerCase().replace(/(^|[\s/(-])\S/g, m => m.toUpperCase()); }
function itineraryFromDetail(it) {
  const hm = s => s ? s.slice(11, 16) : "";
  const rows = [];
  let sea = 0;
  const flush = () => { if (sea) rows.push(`<tr class="sea"><td class="t"></td><td>${sea > 1 ? sea + " Seetage" : "Seetag"}</td><td></td><td></td></tr>`); sea = 0; };
  it.forEach(p => {
    if (p.code === "SEE") { sea++; return; }
    flush();
    const day = (p.arr || p.dep || "").slice(0, 10);
    const tag = p.scenic ? ' <span class="mut">(Panoramafahrt)</span>' : (p.tender ? ' <span class="mut">(Tenderhafen)</span>' : "");
    rows.push(`<tr><td class="t">${day ? d(day).slice(0, 6) : "Tag " + p.day}</td><td>${titleCase(p.name)}${tag}</td>
      <td class="t">${p.arr ? "an " + hm(p.arr) : ""}</td><td class="t">${p.dep ? "ab " + hm(p.dep) : ""}</td></tr>`);
  });
  flush();
  return `<div class="sect">Reiseverlauf</div><table class="itin">${rows.join("")}</table>`;
}
// Routenkarte (Leaflet/OpenStreetMap): Häfen in Reihenfolge, Marker mit Tagesnummer
function drawRoute(el, it, coords) {
  if (!window.L || !el) return;
  const stops = [];
  (it || []).forEach(p => {
    const c = coords[p.code]; if (!c) return;
    const last = stops[stops.length - 1];
    if (last && last.code === p.code) { last.days.push(p.day); return; }
    stops.push({code: p.code, name: titleCase(p.name), c, days: [p.day]});
  });
  if (!stops.length) { el.remove(); return; }
  const map = L.map(el, {scrollWheelZoom: false});
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    {maxZoom: 12, attribution: "© OpenStreetMap"}).addTo(map);
  L.polyline(stops.map(s => s.c), {color: "#2a78d6", weight: 2.5, dashArray: "6 6"}).addTo(map);
  const seen = {};
  stops.forEach(s => {
    const k = s.code; seen[k] = (seen[k] || []).concat(s.days);
  });
  Object.entries(seen).forEach(([k, days]) => {
    const s = stops.find(x => x.code === k);
    L.marker(s.c, {icon: L.divIcon({className: "", html: `<div class="daymark">${days.join("/")}</div>`, iconSize: null})})
      .bindTooltip(s.name).addTo(map);
  });
  map.fitBounds(L.latLngBounds(stops.map(s => s.c)).pad(0.15));
}
const TARIFF_ORDER = ["LIGHT", "CLASSIC", "CLASSIC ALL IN", "PREMIUM", "PREMIUM ALL IN", "COMFORT ALL IN"];
const CABIN_TYPES = {I: "Innen", M: "Meerblick", B: "Balkon", V: "Veranda", K: "Veranda Komfort", P: "Panorama", D: "Veranda Deluxe", J: "Junior-Suite", S: "Suite"};
const TNAME = {CLASSICAI: "CLASSIC ALL IN", PREMIUMAI: "PREMIUM ALL IN", COMFORTAI: "COMFORT ALL IN",
  PAUSCHALAI: "PAUSCHAL ALL IN", SEEREISEAI: "SEEREISE ALL IN"};
const FLY_FROM = ["CGN", "FRA"];          // bevorzugte Abflughäfen in dieser Reihenfolge
const FF_CRUISE = 0.10, FF_FLIGHT = 0.07;  // Family & Friends: −10 % Reisepreis, −7 % Flug
function pickFlight(c) {
  const fl = c.flights || {};
  for (const ap of FLY_FROM) if (fl[ap]) return {ap, part: fl[ap]};
  return null;
}
// Detail-Tabelle einer hinterlegten Reise: Flug ab CGN (sonst FRA), F&F-Preise, Wunschkategorie
function tariffMatrixDetail(t) {
  const fp = t.focus_prices;
  return tariffMatrix(t.tariffs, true, {ff: true, fly: true, focus: fp});
}
function tariffMatrix(tariffs, withFlight = true, opt = {}) {
  if (!tariffs || !tariffs.length) return "";
  tariffs = tariffs.map(t => Object.assign({}, t, {tariff: TNAME[t.tariff] || t.tariff}));
  const tarifs = TARIFF_ORDER.filter(x => tariffs.some(t => t.tariff === x))
    .concat([...new Set(tariffs.map(t => t.tariff))].filter(x => !TARIFF_ORDER.includes(x)));
  const TORD = "IMBVKPDJS";
  const types = [...new Set(tariffs.map(t => t.cabin_type))].sort((a, b) => { const o = x => TORD.includes(x) ? TORD.indexOf(x) : 99; return o(a) - o(b); });
  const foc = opt.focus;
  const rows = types.map(ct => {
    const isFoc = foc && foc.type === ct;
    const cells = tarifs.map(tf => {
      const c = tariffs.find(t => t.cabin_type === ct && t.tariff === tf);
      if (!c) return null;
      // Zeile der Wunschkategorie: deren Preis je Tarif statt des günstigsten der Kabinenart
      const own = isFoc && foc.prices[tf];
      return Object.assign({}, c, {cabin: own || c.cabin, own: !!own});
    });
    const min = Math.min(...cells.filter(Boolean).map(c => c.cabin || 1e9));
    return `<tr><td>${CABIN_TYPES[ct] || ct}${isFoc ? ` <b style="color:var(--s1)">${foc.code}</b>` : ""}</td>${cells.map(c => {
      if (!c) return "<td>–</td>";
      let fl = "";
      if (withFlight && opt.fly) {
        const f = pickFlight(c);
        fl = f ? `<span class="f">mit Flug ${eur(c.cabin + f.part)} ab ${f.ap}</span>` : `<span class="f">kein Flug ab ${FLY_FROM.join("/")}</span>`;
      } else if (withFlight && c.flight) {
        fl = `<span class="f">mit Flug ${eur(c.flight)}${c.flight_from ? " ab " + c.flight_from : ""}</span>`;
      }
      let ff = "";
      if (opt.ff && c.cabin) {
        const f = pickFlight(c);
        const cr = c.cabin * (1 - FF_CRUISE);
        ff = `<span class="f ff">F&amp;F ${eur(cr)}${f ? ` · mit Flug ${eur(cr + f.part * (1 - FF_FLIGHT))}` : ""}</span>`;
      }
      const tag = isFoc && !c.own && c.tariff === "CLASSIC" ? ' <span class="mut" title="Garantiekabine, keine Kabinenwahl">G</span>' : "";
      return `<td class="${c.cabin === min ? "best" : ""}">${eur(c.cabin)}${tag}${fl}${ff}</td>`;
    }).join("")}</tr>`;
  }).join("");
  return `<div class="tmxwrap"><table class="tmx"><thead><tr><th>Kabinenart</th>${tarifs.map(x => `<th${x === "PREMIUM ALL IN" ? ' style="color:var(--s1)"' : ""}>${x}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>`;
}
function changeText(e) {
  if (e.kind === "Preis") return `${e.tariff} ${eur(e.old)} → ${eur(e.new)} <span class="${e.new > e.old ? "up" : "down"}">(${e.new > e.old ? "+" : ""}${eur(e.new - e.old)})</span>`;
  if (e.kind === "Nachschub") return `Nachschub +${e.val} Kabinen`;
  if (e.kind === "wieder buchbar") return `wieder buchbar (${e.val} frei)`;
  return e.kind;
}
function changesBlock(t) {
  const ev = t.changes || [];
  return `<div class="sect">Änderungen (letzte 14 Tage)</div>` + (ev.length
    ? `<table class="chg">${ev.slice(0, 40).map(e => `<tr><td class="t">${d(e.ts.slice(0, 10)).slice(0, 6)} ${e.ts.slice(11, 16)}</td><td><b>${e.code}</b></td><td>${changeText(e)}</td></tr>`).join("")}</table>`
    : `<p class="refills mut">Keine Änderungen an Preisen oder Verfügbarkeit.</p>`);
}
function refillBlock(t) {
  const ev = t.refills || [];
  return `<div class="sect">Nachschub (letzte 7 Tage)</div>` + (ev.length
    ? `<ul class="refills">${ev.slice(0, 12).map(e => `<li>${d(e.ts.slice(0, 10))} ${e.ts.slice(11, 16)} – <b>${e.code}</b> +${e.plus} Kabinen freigegeben</li>`).join("")}</ul>`
    : `<p class="refills mut">Kein Nachschub: keine Kategorie ist um 5 oder mehr freie Kabinen gestiegen.</p>`);
}

function focusBlock(t, ti) {
  if (!t.focus) return "";
  const s = t.series, n = s.length, last = s[n - 1], prev = n > 1 ? s[n - 2] : null, first = s.find(x => x.focus_allin != null) || s[0];
  const df = prev && prev.focus_free != null ? last.focus_free - prev.focus_free : null;
  const dp = prev && prev.focus_allin != null && last.focus_allin != null ? last.focus_allin - prev.focus_allin : null;
  const dpAll = first.focus_allin != null && last.focus_allin != null ? last.focus_allin - first.focus_allin : null;
  const assigned = t.cabin_choice === false;   // keine Kabinenwahl -> keine Freizahl
  const low = !assigned && last.focus_free != null && last.focus_free <= 20;
  return `<div class="focus">
    <h3>Deine Kabine: ${t.focus.group}, ${t.focus.name} (${t.focus.code}) · PREMIUM ALL IN</h3>
    <div class="tiles">
      <div class="tile"><div class="k">Frei</div><div class="v">${assigned ? "–" : (last.focus_free ?? "–")}</div>
        <div class="d">${assigned ? "Kabine wird zugeteilt" : `${low ? '<span class="up">wird knapp</span> · ' : ""}zuletzt ${sign(df, "", true)}`}</div></div>
      <div class="tile"><div class="k">Preis ALL IN</div><div class="v">${eur(last.focus_allin)}</div>
        <div class="d">zuletzt ${sign(dp, " €")}</div></div>
      <div class="tile"><div class="k">Preis seit Beginn</div><div class="v">${sign(dpAll, " €")}</div>
        <div class="d">seit ${d(first.day)}</div></div>
    </div>
    <div class="charts" style="margin-bottom:0">
      <div class="chart"><h3>Freie Kabinen ${t.focus.code}</h3><div class="box"><canvas id="ff${ti}"></canvas></div></div>
      <div class="chart"><h3>Preis ${t.focus.code} PREMIUM ALL IN</h3><div class="box"><canvas id="fp${ti}"></canvas></div></div>
    </div>
  </div>`;
}

const $ = id => document.getElementById(id);

function subText() {
  const last = DATA.trips.map(t => t.fetched_at).sort().pop();
  $("sub").textContent = "Letzter Abruf: " + d(last.slice(0, 10)) + ", " + last.slice(11, 16) + " Uhr";
}

function cabinLine(n, i) {
  if (!i) return "";
  const bits = [];
  if (i.m2) bits.push(String(i.m2).replace(".", ",") + " m²");
  if (i.lage) bits.push(i.lage.join(" · "));
  if (i.beds) bits.push(i.beds);
  if (i.extra) bits.push("+" + i.extra + " Zustellbett" + (i.extra > 1 ? "en" : ""));
  if (i.occ) bits.push("bis " + i.occ + " Pers.");
  if (i.bal) bits.push(i.bal);
  if (i.dir) bits.push("Bett in Fahrtrichtung");
  if (i.near) bits.push("nahe " + i.near.join(", "));
  if (i.conn) bits.push("Verbindungstür");
  if (i.bath2) bits.push("2 Bäder");
  if (i.wardrobe) bits.push("begehbarer Schrank");
  return bits.join(" · ");
}
function recentRefill(t) {
  const lim = new Date(Date.now() - 24 * 3600e3).toISOString().slice(0, 16);
  return (t.refills || []).filter(e => e.ts >= lim);
}
function offerChips(t) {
  const o = t.offer; if (!o) return "";
  const c = (o.campaigns || []).map(x => `<span class="chip camp">${x.name}${x.until ? " bis " + d(x.until) : ""}</span>`);
  const n = (o.notes || []).map(x => `<span class="chip">${x}</span>`);
  return c.concat(n).join("");
}

function tripCard(t, ti) {
  const occ = 100 * (1 - t.free_total / t.ship_total);
  const now = t.series.at(-1), prev = t.series.at(-2);
  const dFree = t.focus && prev && now.focus_free != null && prev.focus_free != null
    ? sign(now.focus_free - prev.focus_free, "", true) : "";
  const dPrice = t.focus && prev && now.focus_allin != null && prev.focus_allin != null
    ? sign(now.focus_allin - prev.focus_allin, " €") : "";
  const assigned = t.cabin_choice === false;
  const low = !assigned && t.focus && now.focus_free != null && now.focus_free <= 20;
  const fb = t.focus
    ? `<div class="focusbadge">Deine Kabine ${t.focus.code}: ${assigned ? "wird zugeteilt" : `${now.focus_free ?? "–"} frei ${dFree}${low ? " · wird knapp" : ""}`}</div>`
    : "";
  const rf = recentRefill(t);
  const badges = (rf.length ? `<span class="chip refill">Nachschub: ${rf.map(e => e.code + " +" + e.plus).join(", ")}</span>` : "")
    + offerChips(t);
  return `<button class="card" data-j="${t.journey}" onclick="location.hash='t=${ti}'">
    ${t.map_file ? `<img class="cardmap" src="${t.map_file}" alt="Route ${t.title}" loading="lazy">` : ""}
    ${t.added_by ? `<span class="by">${t.added_by}</span>` : ""}
    ${badges ? `<div class="chips">${badges}</div>` : ""}
    <span class="rm" role="button" title="Reise entfernen" aria-label="Reise entfernen"
      onclick="event.stopPropagation(); removeTrip('${t.journey}')">✕</span>
    <h2>${t.title}</h2>
    <div class="meta">${t.ship} · ${d(t.start)} bis ${d(t.end)} ab ${t.from}</div>
    ${fb}
    ${t.focus ? `<div class="cstat"><span class="k">${t.focus.group} (${t.focus.code}) ALL IN</span>
      <span class="v">${eur(now.focus_allin)} ${dPrice}</span></div>` : ""}
    ${t.cabin_choice === false
      ? `<div class="cstat"><span class="k">Kabinen</span><span class="v">AIDA teilt zu</span></div>
         <div class="cstat"><span class="k">Belegt</span><span class="v">unbekannt</span></div>`
      : `<div class="cstat"><span class="k">Frei gesamt</span><span class="v">${t.free_total.toLocaleString("de-DE")} / ${t.ship_total.toLocaleString("de-DE")}</span></div>
         <div class="cstat"><span class="k">Belegt</span><span class="v">${occ.toFixed(0)} %</span></div>`}
    ${t.error ? '<div class="arrow" style="color:var(--up)">⚠ letzter Abruf fehlgeschlagen</div>' : ""}
    <div class="arrow">Details ansehen →</div>
  </button>`;
}

// Entfernte Reisen sofort ausblenden, bis der nächste Lauf sie wirklich aus der Liste nimmt
// (die KV-Liste von Cloudflare zeigt neue Einträge erst mit Verzögerung).
function removedLocal() {
  try {
    const m = JSON.parse(localStorage.getItem("aida-removed") || "{}"), now = Date.now();
    Object.keys(m).forEach(j => { if (now - m[j] > 3 * 3600e3) delete m[j]; });
    return m;
  } catch { return {}; }
}
function markRemoved(jid) {
  const m = removedLocal(); m[jid] = Date.now();
  try { localStorage.setItem("aida-removed", JSON.stringify(m)); } catch {}
}

function renderList() {
  charts.splice(0).forEach(c => c.destroy());
  $("detail").innerHTML = "";
  $("detail").style.display = "none";
  const list = $("list");
  list.style.display = "";
  // Nach Ersteller gruppieren, Reihenfolge nach erstem Auftreten
  const groups = [], gone = removedLocal();
  DATA.trips.forEach((t, i) => {
    if (gone[t.journey]) return;
    const key = t.added_by || "Ohne Zuordnung";
    let g = groups.find(x => x.name === key);
    if (!g) { g = {name: key, items: []}; groups.push(g); }
    g.items.push(i);
  });
  const sections = groups.map(g => `<div class="persongroup">
    <h2>${g.name}</h2>
    <div class="cards">${g.items.map(i => tripCard(DATA.trips[i], i)).join("")}</div>
  </div>`).join("");
  list.innerHTML = `<div id="pending"></div>${sections}
    <div class="cards" style="margin-top:16px">
      <button class="addcard" onclick="openAdd()"><span class="plus">+</span> Reise hinzufügen <span class="mut">(${Math.max(0, MAX_TRIPS - slotsUsed())} von ${MAX_TRIPS} frei)</span></button></div>`
    + addDialogHTML();
  loadPending();
}

function addDialogHTML() {
  return `<dialog class="dlg" id="adddlg"><form onsubmit="return submitTrip(event)">
    <h3>Reise zur Prüfung hinzufügen</h3>
    <p>Link aus der AIDA-Buchungsstrecke einfügen (aida.de/finden/…). Name und Route übernehmen wir
       automatisch von AIDA. Die Reise wird bei der nächsten Abfrage mit erfasst.</p>
    <label>AIDA-Link</label>
    <input name="url" type="url" placeholder="https://aida.de/finden/CO07270106/CLASSIC?pax[adults]=2" required>
    <label>Wunschkabine (Kürzel, Standard IA)</label>
    <input name="focus" type="text" value="IA" maxlength="4">
    <div class="msg" id="addmsg"></div>
    <div class="row"><button type="button" onclick="adddlg.close()">Abbrechen</button>
      <button type="submit" class="primary">Einreichen</button></div>
  </form></dialog>`;
}
const MAX_TRIPS = 8;
function slotsUsed() { return (DATA.tracked || []).length + (typeof KMARKED !== "undefined" ? KMARKED.size : 0); }
function openAdd() {
  if (slotsUsed() >= MAX_TRIPS) { alert(`Es sind bereits ${MAX_TRIPS} Reisen hinterlegt – bitte erst eine entfernen.`); return; }
  document.getElementById("adddlg").showModal();
}
function toggleCabins(key) {
  const row = document.getElementById("cab_" + key);
  if (row) row.hidden = !row.hidden;
}
async function submitTrip(e) {
  e.preventDefault();
  const f = e.target, msg = document.getElementById("addmsg");
  msg.style.color = "var(--text-2)"; msg.textContent = "Wird gesendet…";
  try {
    const r = await fetch("/api/submit", {method: "POST", headers: {"content-type": "application/json"},
      credentials: "include", body: JSON.stringify({url: f.url.value, focus: f.focus.value})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Fehler");
    msg.style.color = "var(--down)";
    msg.textContent = "Eingereicht – wird bei der nächsten Abfrage erfasst.";
    f.reset(); f.focus.value = "IA";
    setTimeout(() => { document.getElementById("adddlg").close(); loadPending(); }, 1400);
  } catch (err) {
    msg.style.color = "var(--up)"; msg.textContent = err.message;
  }
  return false;
}
async function removeTrip(jid) {
  const t = (window.AIDA_DATA.trips || []).find(x => x.journey === jid);
  if (!confirm(`„${t ? t.title : jid}“ wirklich entfernen?\nDie Reise wird nicht mehr abgefragt; die bisherige Historie bleibt im Daten-Repo erhalten.`)) return;
  try {
    const r = await fetch("/api/remove", {method: "POST", headers: {"content-type": "application/json"},
      credentials: "include", body: JSON.stringify({journey: jid})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "Fehler");
    markRemoved(jid);
    if (location.hash) location.hash = ""; else renderList();
  } catch (err) { alert("Entfernen fehlgeschlagen: " + err.message); }
}
async function loadPending() {
  try {
    const r = await fetch("/api/pending", {credentials: "include"});
    if (!r.ok) return;
    const d = await r.json();
    const box = document.getElementById("pending");
    if (!box) return;
    const items = d.items || [];
    const adds = items.filter(i => i.action !== "remove"), rms = items.filter(i => i.action === "remove");
    rms.forEach(i => document.querySelectorAll(`.card[data-j="${i.journey}"]`).forEach(c => c.remove()));
    box.innerHTML = (adds.length
      ? `<div class="pending">⏳ ${adds.length} eingereichte Reise${adds.length > 1 ? "n" : ""} wartet auf die nächste Abfrage: ${adds.map(i => i.journey).join(", ")}</div>`
      : "") ;
  } catch {}
}

// Countdown bis zur nächsten automatischen Abfrage (jede volle Stunde, XX:00).
// Automatische Abfragen: stündlich 8–20 Uhr (deutsche Zeit, GitHub Actions)
function nextQuery() {
  const now = new Date();
  for (let add = 0; add < 2; add++) for (let h = 8; h <= 20; h++) {
    const t = new Date(now); t.setDate(t.getDate() + add); t.setHours(h, 0, 0, 0);
    if (t > now) return t;
  }
}
function tickTimer() {
  const el = document.getElementById("timerval");
  if (!el) return;
  const diff = nextQuery() - new Date();
  const s = Math.max(0, Math.floor(diff / 1000));
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  const p = n => String(n).padStart(2, "0");
  el.textContent = `${hh}:${p(mm)}:${p(ss)}`;
}
tickTimer();
setInterval(tickTimer, 1000);

function renderDetail(ti) {
  const t = DATA.trips[ti];
  if (!t) { location.hash = ""; return; }
  charts.splice(0).forEach(c => c.destroy());
  $("list").style.display = "none";
  const root = $("detail");
  root.style.display = "";
  const occ = 100 * (1 - t.free_total / t.ship_total);
  const used = GROUPS.map((g, i) => ({key: g[0], name: g[1], color: `--s${i + 1}`}))
    .filter(g => t.series.some(s => (s.free[g.key] || 0) > 0 || s.premium[g.key]));
  root.innerHTML = `
    <button class="back" onclick="location.hash=''">← Alle Reisen</button>
    ${t.decks && t.decks.length && t.free_cabins ? `<button class="deckbtn" onclick="openDeck3d(${ti})">🛳 3D-Deckplan</button>` : ""}
    <button class="rmbtn" onclick="removeTrip('${t.journey}')">Reise entfernen</button>
    <section class="trip">
      <h2>${t.title}</h2>
      <div class="meta">${t.ship} · ${d(t.start)} bis ${d(t.end)} ab ${t.from} · Reise ${t.journey} · 2 Erwachsene · Stand ${d(t.fetched_at.slice(0,10))} ${t.fetched_at.slice(11,16)}</div>
      ${t.error ? `<div class="err">Letzter Abruf am ${d(t.error.fetched_at.slice(0,10))} fehlgeschlagen: ${t.error.error}</div>` : ""}
      ${t.offer ? `<div class="offer">${t.offer.from_pp ? `<span><span class="from">ab ${eur(t.offer.from_pp)}</span> p. P. (${({IND: "PREMIUM", INDAI: "PREMIUM ALL IN", CLA: "CLASSIC", LIG: "LIGHT", COMAI: "COMFORT ALL IN"})[t.offer.from_tariff] || t.offer.from_tariff})</span>` : ""}
        ${(t.offer.amenities || []).map(a => `<span class="mut">${a}</span>`).join("")}</div>
        <div class="chips">${offerChips(t)}</div>` : ""}
      ${t.cabin_choice === false ? `<div class="warnbox"><b>Kabine wird zugeteilt:</b> Für diese Reise lässt sich keine bestimmte Kabinennummer wählen – AIDA teilt die Kabine bei der Buchung zu.</div>` : ""}
      <div class="maps">
        ${t.map_file ? `<figure><img class="routemap" src="${t.map_file}" alt="Route ${t.title}"><figcaption class="routecap">Reiseroute laut AIDA</figcaption></figure>` : ""}
        ${t.port_coords && Object.keys(t.port_coords).length ? `<figure><div class="leafmap" id="lm${ti}"></div><figcaption class="routecap">Häfen laut Reiseverlauf · Karte © OpenStreetMap</figcaption></figure>` : ""}
      </div>
      ${itinerary(t)}
      ${t.tariffs && t.tariffs.length ? `<div class="sect">Preise je Kabinenart und Tarif</div>${tariffMatrixDetail(t)}<p class="note" style="margin-top:-8px">Kabinenpreis für 2 Erwachsene (günstigste Kabine der Kabinenart${t.focus_prices ? `, in der Zeile ${t.focus_prices.code} der Preis deiner Wunschkategorie ${t.focus_prices.code}; G = CLASSIC-Garantiekabine ohne Kabinenwahl` : ""}). Darunter: inkl. Hin- und Rückflug ab ${FLY_FROM[0]}, sonst ab ${FLY_FROM[1]}; F&amp;F = Family &amp; Friends (−10 % auf den Reisepreis, −7 % auf den Flug). Grün = günstigster Tarif je Kabinenart.</p>` : ""}
      ${changesBlock(t)}
      ${focusBlock(t, ti)}
      <div class="sect">Ganzes Schiff</div>
      ${t.cabin_choice === false ? `<p class="mut" style="margin:0 0 22px">Für diese Reise gibt AIDA keine Kabinenliste heraus (Kabine wird bei der Buchung zugeteilt) – freie Kabinen und Belegung sind deshalb nicht bekannt, nur die Preise.</p>` : `
      <div class="tiles">
        <div class="tile"><div class="k">Freie Kabinen</div><div class="v">${t.free_total.toLocaleString("de-DE")}</div>
          <div class="d">von ${t.ship_total.toLocaleString("de-DE")} an Bord</div></div>
        <div class="tile"><div class="k">Belegt</div><div class="v">${occ.toFixed(1).replace(".", ",")} %</div>
          <div class="d">ganzes Schiff</div></div>
        <div class="tile"><div class="k">Seit letztem Abruf</div><div class="v">${sign(t.prev_free == null ? null : t.free_total - t.prev_free, "", true)}</div>
          <div class="d">${t.booked == null ? "erster Abruf" : `${t.booked} weggebucht, ${t.released} wieder frei`}</div></div>
        <div class="tile"><div class="k">Seit Beobachtungsbeginn</div><div class="v">${sign(t.free_total - t.first_free, "", true)}</div>
          <div class="d">seit ${d(t.first_day)}</div></div>
      </div>
      <div class="chart" style="margin-bottom:18px"><h3>Freie Kabinen gesamt</h3><div class="box"><canvas id="ct${ti}"></canvas></div></div>`}
      <div class="charts">
        <div class="chart"><h3>Freie Kabinen je Kabinentyp</h3><div class="box"><canvas id="cf${ti}"></canvas></div></div>
        <div class="chart"><h3>Günstigster Kabinenpreis PREMIUM ALL IN je Typ</h3><div class="box"><canvas id="cp${ti}"></canvas></div></div>
      </div>
      <div class="legend">${used.map(g => `<span style="--c:var(${g.color})">${g.name}</span>`).join("")}</div>
      <div class="tbl" style="margin-top:22px"><h3>Unterkategorien heute</h3>
        <table><thead><tr><th>Kat.</th><th>Typ</th><th>Bezeichnung</th><th>frei</th><th>Δ zuletzt</th><th>Δ 7 Tage</th>
          <th>ALL IN</th><th>Δ</th><th>PREMIUM</th><th>CLASSIC</th><th>Δ</th><th>LIGHT</th></tr></thead>
        <tbody>${t.table.map(r => `<tr class="${t.focus && r.code === t.focus.code ? "hl" : ""}">
          <td class="code">${r.code}</td><td>${r.group}</td><td>${r.name}</td>
          <td>${r.cabins && r.cabins.length
            ? `<button class="freebtn" onclick="toggleCabins('${ti}_${r.code}')">${r.free} ▾</button>`
            : (r.guarantee && !r.free ? '<span class="mut">Garantie</span>' : r.free)}</td>
          <td>${sign(r.d1, "", true)}</td><td>${sign(r.d7, "", true)}</td>
          <td>${eur(r.allin)}</td><td>${sign(r.da, " €")}</td><td>${eur(r.premium)}</td>
          <td>${eur(r.classic)}</td><td>${sign(r.dc, " €")}</td><td>${eur(r.light)}</td></tr>
          ${r.cabins && r.cabins.length ? `<tr class="cabrow" id="cab_${ti}_${r.code}" hidden>
            <td colspan="12"><b>${r.code} – freie Kabinen (${r.cabins.length}):</b>
              <div class="cabgrid">${r.cabins.map(n => { const i = (t.cabin_info || {})[n];
                return `<div class="cab"><b>${n}</b>${i && i.plan ? ` · <a href="${i.plan}" target="_blank" rel="noopener">Grundriss</a>` : ""}<div class="x">${cabinLine(n, i)}</div></div>`; }).join("")}</div></td></tr>` : ""}`).join("")}</tbody></table>
        <p class="note">Preise pro Kabine für 2 Erwachsene ohne Flug. ALL IN = PREMIUM ALL IN mit Getränkepaket. CLASSIC = Garantiekabine (AIDA teilt die Kabine zu), daher ohne Stückzahl. LIGHT = günstigster Tarif mit eingeschränkten Leistungen. Rot = schlechter für dich (weniger frei bzw. teurer), grün = besser. Klick auf eine Zahl in „frei" zeigt die Kabinennummern.</p>
      </div>
    </section>`;
  window.scrollTo(0, 0);
  if (t.port_coords) drawRoute($("lm" + ti), t.itinerary, t.port_coords);

  const labels = t.series.map(s => d(s.day).slice(0, 6) + " " + (s.ts ? s.ts.slice(11, 16) : ""));
  const base = (yfmt) => ({
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: {mode: "index", intersect: false},
    plugins: {legend: {display: false},
      tooltip: {callbacks: {title: items => items.length ? "Stand " + items[0].label : "",
        label: c => ` ${c.dataset.label}: ${yfmt(c.parsed.y)}`}}},
    scales: {
      x: {grid: {display: false}, ticks: {color: css("--text-2")}, border: {color: css("--border")}},
      y: {grid: {color: css("--grid")}, border: {display: false},
        ticks: {color: css("--text-2"), maxTicksLimit: 8,
          callback: v => Math.abs(v - Math.round(v)) < 1e-9 ? yfmt(v) : ""}}
    },
    elements: {line: {borderWidth: 2, tension: 0}, point: {radius: t.series.length > 20 ? 0 : 4, hoverRadius: 5}}
  });
  const intFmt = v => Math.round(v).toLocaleString("de-DE");
  if ($("ct" + ti)) charts.push(new Chart($("ct" + ti), {type: "line", data: {labels, datasets: [{label: "Frei gesamt",
    borderColor: css("--s1"), backgroundColor: css("--s1"), data: t.series.map(s => s.free_total), spanGaps: true}]},
    options: base(intFmt)}));
  const ds = key => used.map(g => ({label: g.name, borderColor: css(g.color), backgroundColor: css(g.color),
    data: t.series.map(s => key === "free" ? (s.free[g.key] || 0) : (s.allin[g.key] ?? null)), spanGaps: true}));
  charts.push(new Chart($("cf" + ti), {type: "line",
    data: {labels, datasets: ds("free")}, options: base(intFmt)}));
  if (t.focus) {
    const one = (id, label, key, fmt, color) => {
      const o = base(fmt); o.scales.y.beginAtZero = key === "focus_free";
      charts.push(new Chart($(id), {type: "line", data: {labels, datasets: [{label,
        borderColor: css(color), backgroundColor: css(color), data: t.series.map(s => s[key] ?? null), spanGaps: true}]}, options: o}));
    };
    one("ff" + ti, "frei", "focus_free", v => Math.round(v).toLocaleString("de-DE"), "--s1");
    one("fp" + ti, "PREMIUM ALL IN", "focus_allin", v => eur(v), "--s1");
  }
  const po = base(v => eur(v));
  po.scales.y.beginAtZero = false;
  charts.push(new Chart($("cp" + ti), {type: "line",
    data: {labels, datasets: ds("premium").filter(x => x.data.some(v => v != null))}, options: po}));
}

// ---------- Katalog (wöchentlich, ~2.000 Reisen) ----------
let KAT = null, KPORTS = {}, KSHOW = 50;
const KF = {q: "", region: "", ship: "", from: "", to: "", nmin: "", nmax: "", pmax: "", camp: false, sort: "start"};
try { Object.assign(KF, JSON.parse(localStorage.getItem("aida-kf") || "{}")); } catch {}
const KMARKED = new Set();
const tripUrl = id => `https://aida.de/finden/${id}/PREMIUM?pax[adults]=2&pax[juveniles]=0&pax[children]=0&pax[babies]=0`;
async function renderKatalog() {
  const root = $("katalog");
  if (!DATA.catalog) { root.innerHTML = `<p class="mut">Noch kein Katalog abgerufen – der kommt täglich um 8 Uhr.</p>`; return; }
  if (!KAT) {
    root.innerHTML = `<p class="mut">Katalog wird geladen …</p>`;
    try {
      KAT = (await (await fetch("catalog.json?v=" + encodeURIComponent(DATA.catalog.fetched_at))).json()).trips;
      try { KPORTS = await (await fetch("ports.json?v=" + encodeURIComponent(DATA.catalog.fetched_at))).json(); } catch {}
    } catch (e) { root.innerHTML = `<p class="err">Katalog nicht ladbar: ${e}</p>`; return; }
  }
  const opts = (vals, cur) => `<option value="">alle</option>` + vals.map(v => `<option${v === cur ? " selected" : ""}>${v}</option>`).join("");
  const regions = [...new Set(KAT.map(t => t.region).filter(Boolean))].sort();
  const ships = [...new Set(KAT.map(t => t.ship).filter(Boolean))].sort();
  root.innerHTML = `<p class="kmeta">${DATA.catalog.count.toLocaleString("de-DE")} Reisen im AIDA-Katalog · Stand ${d(DATA.catalog.fetched_at.slice(0, 10))} (täglich um 8 Uhr aktualisiert, montags komplett). ${DATA.catalog.fleet ? `Freie Kabinen täglich für ${DATA.catalog.fleet} Reisen der nächsten 4 Monate. ` : ""}„Hinterlegen“ nimmt eine Reise in die laufende Beobachtung auf (${Math.max(0, MAX_TRIPS - slotsUsed())} von ${MAX_TRIPS} Plätzen frei).</p>
    <div class="filters">
      <label>Suche<input type="search" data-k="q" value="${KF.q}" placeholder="Titel oder Hafen"></label>
      <label>Fahrgebiet<select data-k="region">${opts(regions, KF.region)}</select></label>
      <label>Schiff<select data-k="ship">${opts(ships, KF.ship)}</select></label>
      <label>Abreise ab<input type="month" data-k="from" value="${KF.from}"></label>
      <label>bis<input type="month" data-k="to" value="${KF.to}"></label>
      <label>Nächte min.<input class="num" type="number" min="1" data-k="nmin" value="${KF.nmin}"></label>
      <label>max.<input class="num" type="number" min="1" data-k="nmax" value="${KF.nmax}"></label>
      <label>max. € p. P.<input class="num" type="number" min="0" step="50" data-k="pmax" value="${KF.pmax}"></label>
      <label>Sortierung<select data-k="sort">
        <option value="start"${KF.sort === "start" ? " selected" : ""}>Abreise</option>
        <option value="price"${KF.sort === "price" ? " selected" : ""}>Preis p. P.</option>
        <option value="nights"${KF.sort === "nights" ? " selected" : ""}>Dauer</option>
        <option value="free"${KF.sort === "free" ? " selected" : ""}>wenig frei zuerst</option></select></label>
      <label style="flex-direction:row;align-items:center;gap:6px"><input type="checkbox" data-k="camp"${KF.camp ? " checked" : ""}>nur mit Aktion</label>
    </div><div id="klist"></div>`;
  root.querySelectorAll("[data-k]").forEach(el => el.addEventListener(el.type === "search" || el.type === "number" ? "input" : "change", () => {
    KF[el.dataset.k] = el.type === "checkbox" ? el.checked : el.value;
    try { localStorage.setItem("aida-kf", JSON.stringify(KF)); } catch {}
    KSHOW = 50; drawKList();
  }));
  drawKList();
}
function kFiltered() {
  const q = KF.q.trim().toLowerCase();
  let r = KAT.filter(t =>
    (!q || (t.title + " " + (t.itinerary || []).map(p => p.name).join(" ") + " " + t.id).toLowerCase().includes(q)) &&
    (!KF.region || t.region === KF.region) && (!KF.ship || t.ship === KF.ship) &&
    (!KF.from || (t.start || "") >= KF.from) && (!KF.to || (t.start || "").slice(0, 7) <= KF.to) &&
    (!KF.nmin || t.nights >= +KF.nmin) && (!KF.nmax || t.nights <= +KF.nmax) &&
    (!KF.pmax || (t.from_pp && t.from_pp <= +KF.pmax)) && (!KF.camp || (t.campaigns && t.campaigns.length)));
  const key = {start: t => t.start, price: t => t.from_pp || 1e9, nights: t => t.nights,
               free: t => t.fleet && t.fleet.free != null ? t.fleet.free : 1e9}[KF.sort] || (t => t.start);
  return r.sort((a, b) => key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0);
}
function drawKList() {
  const list = kFiltered(), box = $("klist");
  const tracked = new Set(DATA.tracked || []);
  box.innerHTML = `<p class="kmeta">${list.length.toLocaleString("de-DE")} Treffer</p>` + list.slice(0, KSHOW).map(t => {
    const on = tracked.has(t.id), pend = KMARKED.has(t.id);
    return `<div class="krow" id="kr_${t.id}">
      <div class="top" onclick="toggleK('${t.id}')">
        <h3>${t.title}</h3>
        <span class="m">${t.ship} · ${d(t.start)} – ${d(t.end)} · ${t.nights} Nächte${t.region ? " · " + t.region : ""}${t.flight ? " · inkl. Flug" : ""}</span>
        ${t.fleet && t.fleet.choice === false ? `<span class="m">Kabine wird zugeteilt</span>` : ""}
        ${t.fleet && t.fleet.choice !== false ? `<span class="m" title="freie Kabinen laut AIDA (je Kategorie max. 100 angezeigt), Stand ${d(t.fleet.date)}"><b>${t.fleet.free.toLocaleString("de-DE")}${t.fleet.capped.length ? "+" : ""} frei</b>${t.fleet.prev != null ? ` (${t.fleet.free - t.fleet.prev >= 0 ? "+" : ""}${t.fleet.free - t.fleet.prev})` : ""}</span>` : ""}
        <span class="p">${t.from_pp ? "ab " + eur(t.from_pp) + " p. P." : ""}</span>
        <button class="mark${on || pend ? " on" : ""}" onclick="event.stopPropagation(); markK('${t.id}')">${on ? "✓ hinterlegt" : pend ? "✓ vorgemerkt" : "+ hinterlegen"}</button>
      </div>
      ${(t.campaigns || []).length || (t.notes || []).length ? `<div class="chips" style="margin:8px 0 0">${(t.campaigns || []).map(c => `<span class="chip camp">${c.name}${c.until ? " bis " + d(c.until) : ""}</span>`).join("")}${(t.notes || []).map(n => `<span class="chip">${n}</span>`).join("")}</div>` : ""}
      <div class="more" hidden></div></div>`;
  }).join("") + (list.length > KSHOW ? `<button class="mark" onclick="KSHOW += 50; drawKList()">Weitere anzeigen</button>` : "");
}
function toggleK(id) {
  const more = document.querySelector(`#kr_${id} .more`);
  if (!more) return;
  if (!more.hidden) { more.hidden = true; return; }
  const t = KAT.find(x => x.id === id);
  more.hidden = false;
  more.innerHTML = `<div class="leafmap" id="km_${id}" style="height:300px"></div>
    ${t.itinerary ? itineraryFromDetail(t.itinerary.map((p, i) => Object.assign({day: i + 1}, p))) : ""}
    ${t.fleet && t.fleet.choice !== false ? `<div class="sect">Freie Kabinen je Kategorie (Stand ${d(t.fleet.date)})</div>
      <p class="kmeta">${Object.entries(t.fleet.cats).sort().map(([k, v]) => `<b>${k}</b> ${v}${v >= 100 ? "+" : ""}`).join(" · ")}
      ${t.fleet.prev != null ? `<br>Seit dem Vortag: ${t.fleet.plus} Kabinen neu frei, ${t.fleet.minus} weg` : ""}</p>` : ""}
    ${tariffMatrix(t.tariffs)}
    <a href="${tripUrl(id)}" target="_blank" rel="noopener">Bei AIDA ansehen ↗</a>`;
  const it = (t.itinerary || []).map((p, i) => Object.assign({day: i + 1}, p));
  drawRoute($("km_" + id), it, KPORTS);
}
async function markK(id) {
  if ((DATA.tracked || []).includes(id) || KMARKED.has(id)) return;
  if (slotsUsed() >= MAX_TRIPS) { alert(`Es sind bereits ${MAX_TRIPS} Reisen hinterlegt – bitte erst eine entfernen.`); return; }
  try {
    const r = await fetch("/api/submit", {method: "POST", headers: {"content-type": "application/json"},
      credentials: "include", body: JSON.stringify({url: tripUrl(id), focus: "IA"})});
    const dj = await r.json();
    if (!r.ok) throw new Error(dj.error || "Fehler");
    KMARKED.add(id); drawKList();
  } catch (e) { alert("Hinterlegen fehlgeschlagen: " + e.message); }
}

// ---------- Änderungsprotokoll ----------
function renderAend() {
  const root = $("aend");
  const ev = [];
  DATA.trips.forEach((t, ti) => (t.changes || []).forEach(e => ev.push(Object.assign({ti, title: t.title, ship: t.ship, start: t.start}, e))));
  ev.sort((a, b) => a.ts < b.ts ? 1 : -1);
  const cc = DATA.catalog_changes;
  const trip = r => `${r.title || r.id} <span class="mut">· ${r.ship || ""} · ${r.start ? d(r.start) : ""}</span>`;
  root.innerHTML = `<div class="sect">Hinterlegte Reisen (letzte 14 Tage)</div>` + (ev.length
    ? `<table class="chg">${ev.slice(0, 150).map(e => `<tr><td class="t">${d(e.ts.slice(0, 10)).slice(0, 6)} ${e.ts.slice(11, 16)}</td>
        <td><a href="#t=${e.ti}">${e.title}</a> <span class="mut">${d(e.start)}</span></td><td><b>${e.code}</b></td><td>${changeText(e)}</td></tr>`).join("")}</table>`
    : `<p class="mut">Keine Änderungen.</p>`)
    + `<div class="sect">Katalog${cc ? " – Vergleich vom " + d(cc.date) : ""}</div>` + (!cc ? `<p class="mut">Der erste Vergleich entsteht beim nächsten Katalog-Abruf (täglich 8 Uhr).</p>` : `
      <p class="kmeta">${cc.new.length} neu · ${cc.gone.length} entfallen · ${cc.price.length} Preisänderungen („ab“-Preis p. P.)</p>
      ${cc.new.length ? `<h3 style="font-size:15px">Neu im Katalog</h3><table class="chg">${cc.new.map(r => `<tr><td>${trip(r)}</td></tr>`).join("")}</table>` : ""}
      ${cc.price.length ? `<h3 style="font-size:15px">Preisänderungen</h3><table class="chg">${cc.price.slice().sort((a, b) => (a.new - a.old) - (b.new - b.old)).slice(0, 100)
        .map(r => `<tr><td>${trip(r)}</td><td class="t">${eur(r.old)} → ${eur(r.new)} <span class="${r.new > r.old ? "up" : "down"}">(${r.new > r.old ? "+" : ""}${eur(r.new - r.old)})</span></td></tr>`).join("")}</table>` : ""}
      ${cc.gone.length ? `<h3 style="font-size:15px">Entfallen (ausgebucht oder abgesagt)</h3><table class="chg">${cc.gone.map(r => `<tr><td>${r.id}</td></tr>`).join("")}</table>` : ""}`);
}

function showView(v) {
  ["list", "detail", "katalog", "aend"].forEach(id => { $(id).style.display = id === v ? "" : "none"; });
  document.querySelectorAll(".tabs a").forEach(a => a.classList.toggle("on", a.dataset.v === v || (v === "detail" && a.dataset.v === "list")));
}
function route() {
  if (DATA.trips.length) subText();
  if (location.hash === "#katalog") { charts.splice(0).forEach(c => c.destroy()); showView("katalog"); renderKatalog(); return; }
  if (location.hash === "#aenderungen") { charts.splice(0).forEach(c => c.destroy()); showView("aend"); renderAend(); return; }
  const m = location.hash.match(/t=(\d+)/);
  if (m && DATA.trips[+m[1]]) { showView("detail"); renderDetail(+m[1]); } else { showView("list"); renderList(); }
}
window.addEventListener("hashchange", route);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", route);
route();
</script>

<script type="importmap">
{"imports": {
  "three": "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js",
  "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"
}}
</script>
<script type="module">
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { SVGLoader } from "three/addons/loaders/SVGLoader.js";
import { mergeGeometries } from "three/addons/utils/BufferGeometryUtils.js";

const APP3D = window.AIDA_DATA || { trips: [], groups: [] };
const TRIPS3D = APP3D.trips || [];
const GROUPCOL = { I: 0x6d7cff, M: 0xe060f0, B: 0xffffff, K: 0xa070ff, V: 0xff8fc8,
                   P: 0x9aa8bd, D: 0xc89060, S: 0xd8c8ff, J: 0xd8c8ff };   // bewusst keine Ampelfarben
const GROUPNAME = {}; (APP3D.groups || []).forEach(g => GROUPNAME[g[0]] = g[1]);

let d3d = null;
window.openDeck3d = async function (ti) {
  const t = TRIPS3D[ti];
  if (!t) return;
  const wrap = document.getElementById("deck3d");
  const host = document.getElementById("d3d-canvas");
  document.getElementById("d3d-title").textContent = t.title + " – 3D-Deckplan";
  wrap.hidden = false;
  host.innerHTML = '<div class="d3d-loading">Lade Deckpläne…</div>';
  try {
    d3d = await buildScene(t, host);
    buildLegend(t);
  } catch (e) {
    host.innerHTML = '<div class="d3d-loading">Konnte 3D-Ansicht nicht laden: ' + e.message + "</div>";
  }
};
window.closeDeck3d = function () {
  document.getElementById("deck3d").hidden = true;
  document.getElementById("d3d-tip").hidden = true;
  if (d3d) { d3d.dispose(); d3d = null; }
};

async function rasterizeDeck(svg, vb, tint) {
  // Deckplan als Bild rendern und per 'source-in' komplett in einer Farbe einfärben (Hologramm-Look)
  const clone = svg.cloneNode(true);
  clone.setAttribute("width", vb[2]); clone.setAttribute("height", vb[3]);
  const blob = new Blob([new XMLSerializer().serializeToString(clone)], { type: "image/svg+xml" });
  const url = URL.createObjectURL(blob);
  try {
    const img = new Image();
    await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = url; });
    const longest = Math.max(vb[2], vb[3]) || 1;
    const sc = Math.min(1200 / longest, 6);
    const cw = Math.max(2, Math.round(vb[2] * sc)), ch = Math.max(2, Math.round(vb[3] * sc));
    const cv = document.createElement("canvas"); cv.width = cw; cv.height = ch;
    const cx = cv.getContext("2d");
    cx.drawImage(img, 0, 0, cw, ch);
    cx.globalCompositeOperation = "source-in";
    cx.fillStyle = tint;
    cx.fillRect(0, 0, cw, ch);
    return cv;
  } finally { URL.revokeObjectURL(url); }
}

async function parseDeck(code, n) {
  const res = await fetch(`decks/${code}/deck-${n}.svg`);
  if (!res.ok) throw new Error("Deck " + n);
  const txt = await res.text();
  const m = txt.match(/viewBox="([^"]+)"/);
  const vb = m ? m[1].trim().split(/\s+/).map(Number) : [0, 0, 100, 100];
  return { n, vb, svgText: txt };
}

async function buildScene(t, host) {
  const decks = [];
  for (const n of t.decks) {
    try { decks.push(await parseDeck(t.deck_code, n)); } catch (e) { /* Deck überspringen */ }
  }
  if (!decks.length) throw new Error("keine Deckpläne");
  host.innerHTML = "";

  // Platzhalter-Decks: Decks mit bekannten Orten, aber ohne Plan-SVG (z.B. Publikumsdecks 6/7)
  const planNums = new Set(decks.map(d => d.n));
  const refVb = decks.slice().sort((a, b) => b.vb[2] * b.vb[3] - a.vb[2] * a.vb[3])[0].vb;
  Object.keys(t.venues || {}).map(Number).forEach(n => {
    if (!planNums.has(n)) decks.push({ n, vb: refVb, placeholder: true });
  });
  decks.sort((a, b) => a.n - b.n);

  // Weltmaßstab: längste Deckseite auf ~120 Einheiten
  let maxDim = 1;
  decks.forEach(dk => { maxDim = Math.max(maxDim, dk.vb[2], dk.vb[3]); });
  const S = 120 / maxDim;
  const GAP = 12;          // vertikaler Deckabstand (Decks klar getrennt)
  const THICK = 1.4;       // Kabinenhöhe

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x02030a);

  const W = host.clientWidth, H = host.clientHeight;
  const camera = new THREE.PerspectiveCamera(50, W / H, 0.1, 2000);
  camera.position.set(135, 115, 180);
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setSize(W, H);
  host.appendChild(renderer.domElement);

  // Bloom-Glow für den Hologramm-Look
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(W, H), 0.22, 0.35, 0.6);
  composer.addPass(bloom);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.autoRotate = true;
  controls.autoRotateSpeed = 0.5;

  const nDecks = decks.length;
  const yOf = i => (i - (nDecks - 1) / 2) * GAP;

  const free = t.free_cabins || {};
  const loader = new SVGLoader();
  const STRUCT_H = 1.6;    // Welt-Höhe der Struktur/Wände
  const CABIN_H = 3.0;     // Welt-Höhe der Kabinen (ragen heraus)

  const meshes = [];                 // für Raycast
  const deckGroups = [];             // je Deck: Gruppe + Ausfahr-Zustand
  const cabinMeshes = [];            // freie Kabinen-Marker (zum Filtern je Gruppe)

  function bake(geo, cx, cy, di) {
    // SVG-XY (Extrude +Z) -> Weltkoordinaten: zentrieren, skalieren, flach legen, auf Deckhöhe
    geo.translate(-cx, -cy, 0); geo.scale(S, S, S);
    geo.rotateX(-Math.PI / 2); geo.translate(0, yOf(di), 0);
    return geo;
  }
  const deckImg = t.deck_img || {};
  const deckAspect = t.deck_aspect || {};
  const cabinPos = t.cabin_pos || {};
  const texLoader = new THREE.TextureLoader();
  // Seitenbeschriftung: Text läuft entlang der Säulenhöhe. Auf den ±x-Flächen der BoxGeometry
  // liegt die Textur-u-Achse entlang der Höhe, auf den ±y-Flächen quer dazu -> dort gedreht.
  const labelCache = {};
  function sideLabel(num, col, axis, ratio) {
    const key = num + axis;
    if (labelCache[key]) return labelCache[key];
    const S = 56, L = Math.round(Math.min(512, Math.max(96, S * ratio))), along = axis === "x";
    const c = document.createElement("canvas");
    c.width = along ? L : S; c.height = along ? S : L;
    const x = c.getContext("2d");
    x.fillStyle = "#" + col.toString(16).padStart(6, "0"); x.fillRect(0, 0, c.width, c.height);
    x.translate(c.width / 2, c.height / 2);
    if (!along) x.rotate(-Math.PI / 2);
    x.fillStyle = "#0b1020"; x.textAlign = "center"; x.textBaseline = "middle";
    let fs = S * 0.8;
    x.font = `700 ${fs}px system-ui, sans-serif`;
    const tw = x.measureText(num).width;
    if (tw > L * 0.92) { fs *= (L * 0.92) / tw; x.font = `700 ${fs}px system-ui, sans-serif`; }
    x.fillText(num, 0, 2);
    const t = new THREE.CanvasTexture(c);
    t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 4;
    return (labelCache[key] = t);
  }


  // Deckboden = PDF-Plan als Bild; freie Kabinen = Marker an ihrer PDF-Position.
  // Boden und Marker liegen in EINER Deck-Gruppe -> immer deckungsgleich.
  decks.forEach((dk, di) => {
    const url = deckImg[dk.n];
    if (!url) return;
    const Hl = dk.vb[3] * S, Wl = Hl * (deckAspect[dk.n] || 0.3);
    const group = new THREE.Group();
    group.position.y = yOf(di);
    group.rotation.x = -Math.PI / 2;
    scene.add(group);

    const tex = texLoader.load(url); tex.colorSpace = THREE.SRGBColorSpace;
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(Wl, Hl),
      new THREE.MeshBasicMaterial({ map: tex, transparent: true, depthWrite: false, side: THREE.DoubleSide }));
    group.add(floor);
    floor.userData = { kind: "floor", di };
    const dg = { di, group, floor, Wl, baseY: yOf(di), out: 0, dim: 1, cabins: [] };
    deckGroups.push(dg);

    const cp = cabinPos[dk.n];
    if (!cp) return;
    const mw = Math.max(Wl * 0.03, 0.8);
    Object.entries(cp).forEach(([num, uv]) => {
      const g = free[num];
      if (!g) return;
      // Säule auf der Kabinennummer im Plan, Nummer längs an allen vier Seiten
      const col = GROUPCOL[g] ?? 0xffffff;
      const mat = o => new THREE.MeshBasicMaterial({ color: o ? 0xffffff : col, map: o ? sideLabel(num, col, o, CABIN_H / mw) : null, transparent: true, opacity: 0.98 });
      const plain = mat(null);
      const box = new THREE.Mesh(new THREE.BoxGeometry(mw, mw, CABIN_H),
        [mat("x"), mat("x"), mat("y"), mat("y"), plain, plain]);
      box.position.set((uv[0] - 0.5) * Wl, (0.5 - uv[1]) * Hl, CABIN_H / 2);
      box.userData = { kind: "free", group: g, num, deckN: dk.n };
      group.add(box); meshes.push(box); cabinMeshes.push(box); dg.cabins.push(box);
    });
  });

  // Orte als leuchtende Säulen an ihrer Position, in die Höhe gezogen
  const venueGroup = new THREE.Group();
  venueGroup.visible = false;   // Orte standardmäßig aus (per Legende/Klick einblenden)
  const venuePillars = [];
  const pillarH = 2.4;
  const venues = t.venues || {};
  Object.keys(venues).forEach(deckStr => {
    const di = decks.findIndex(dk => String(dk.n) === String(deckStr));
    if (di < 0) return;
    const dk = decks[di], w = dk.vb[2] * S, h = dk.vb[3] * S, y = yOf(di);
    venues[deckStr].forEach(v => {
      const px = (v.u - 0.5) * w, pz = (v.v - 0.5) * h;
      const mesh = new THREE.Mesh(new THREE.BoxGeometry(1.4, pillarH, 1.4),
        new THREE.MeshBasicMaterial({ color: 0xffce5a, transparent: true, opacity: 0.85, depthWrite: false }));
      mesh.position.set(px, y + pillarH / 2, pz);
      mesh.userData = { venue: v.name, deck: deckStr };
      venueGroup.add(mesh); venuePillars.push(mesh);
    });
  });
  scene.add(venueGroup);

  const vlabel = document.createElement("div");
  vlabel.className = "d3d-decklabel"; vlabel.style.color = "#ffd36b"; vlabel.style.borderColor = "#8a6a1f";
  vlabel.hidden = true; host.appendChild(vlabel);
  function showVenueLabel(mesh) {
    const w = renderer.domElement.clientWidth, h = renderer.domElement.clientHeight;
    const p = new THREE.Vector3().setFromMatrixPosition(mesh.matrixWorld);
    p.y += pillarH / 2;
    const v = p.project(camera);
    vlabel.hidden = v.z >= 1;
    vlabel.textContent = mesh.userData.venue;
    vlabel.style.left = (v.x * 0.5 + 0.5) * w + "px";
    vlabel.style.top = (-v.y * 0.5 + 0.5) * h + "px";
  }
  let focused = null;
  function setFocus(mesh) {
    if (focused) focused.scale.set(1, 1, 1);
    focused = mesh;
    if (mesh) { mesh.scale.set(2.6, (GAP * 0.8) / pillarH, 2.6); showVenueLabel(mesh); }
  }

  // Deck-Nummern als schwebende Labels (HTML, projiziert)
  const maxWorldW = Math.max(...decks.map(dk => dk.vb[2] * S));
  const deckLabels = decks.map((dk, di) => {
    const el = document.createElement("div");
    el.className = "d3d-decklabel deck";
    el.textContent = "Deck " + dk.n;
    el.addEventListener("click", ev => { ev.stopPropagation(); selectDeck(selDeck === di ? null : di); });
    host.appendChild(el);
    return { el, pos: new THREE.Vector3(maxWorldW / 2 + 10, yOf(di), 0) };
  });
  function updateLabels() {
    const w = renderer.domElement.clientWidth, h = renderer.domElement.clientHeight;
    deckLabels.forEach(L => {
      const dg = deckGroups.find(g => g.di === deckLabels.indexOf(L));
      const v = L.pos.clone().add(new THREE.Vector3(dg ? dg.group.position.x : 0, dg ? dg.group.position.y - dg.baseY : 0, 0)).project(camera);
      L.el.style.display = v.z < 1 ? "block" : "none";
      L.el.style.left = (v.x * 0.5 + 0.5) * w + "px";
      L.el.style.top = (-v.y * 0.5 + 0.5) * h + "px";
    });
  }

  // Hover / Antippen
  const ray = new THREE.Raycaster();
  const mouse = new THREE.Vector2();
  const tip = document.getElementById("d3d-tip");
  function onMove(ev) {
    const r = renderer.domElement.getBoundingClientRect();
    mouse.x = ((ev.clientX - r.left) / r.width) * 2 - 1;
    mouse.y = -((ev.clientY - r.top) / r.height) * 2 + 1;
    ray.setFromCamera(mouse, camera);
    const vh = venueGroup.visible ? ray.intersectObjects(venuePillars, false) : [];
    if (vh.length) {
      tip.hidden = false;
      tip.style.left = (ev.clientX + 14) + "px";
      tip.style.top = (ev.clientY + 14) + "px";
      tip.innerHTML = `<b>${vh[0].object.userData.venue}</b><br>Deck ${vh[0].object.userData.deck}`;
      return;
    }
    const hits = ray.intersectObjects(meshes.filter(m => m.visible && (!m.parent || m.parent.visible)), false);
    const fh = hits.find(h => h.object.userData.kind === "free") || hits[0];
    if (fh) {
      const ud = fh.object.userData;
      tip.hidden = false;
      tip.style.left = (ev.clientX + 14) + "px";
      tip.style.top = (ev.clientY + 14) + "px";
      tip.innerHTML = ud.kind === "free"
        ? (() => { const i = (t.cabin_info || {})[ud.num]; const cat = (t.free_cat || {})[ud.num];
            return `<b>Kabine ${ud.num}</b>${cat ? " · " + cat : ""}<br>${GROUPNAME[ud.group] || ud.group} · Deck ${ud.deckN} · <span style="color:#7fffcf">frei</span>`
              + (i ? `<div style="max-width:260px;margin-top:4px;color:#b9d3ee">${cabinLine(ud.num, i)}</div>` : "")
              + (i && i.plan ? `<img src="${i.plan}?wid=240" style="display:block;margin-top:6px;max-width:240px;border-radius:6px;background:#fff" alt="">` : ""); })()
        : `ausgebucht · Deck ${ud.deckN}`;
    } else { tip.hidden = true; }
  }
  renderer.domElement.addEventListener("pointermove", onMove);

  // Deck anklicken -> fährt seitlich aus dem Stapel heraus, der Rest wird blass
  const OUT_X = Math.max(...deckGroups.map(g => g.Wl), 10) * 1.6 + 12;
  let selDeck = null;
  let camGlide = 0;                  // Frames, in denen die Kamera noch zum Deck gleitet
  controls.addEventListener("start", () => { camGlide = 0; });   // Nutzer übernimmt sofort
  function selectDeck(di) {
    selDeck = di;
    camGlide = 50;
    controls.autoRotate = false;
    deckLabels.forEach((L, i) => L.el.classList.toggle("sel", i === di));
  }
  let downAt = null;
  renderer.domElement.addEventListener("pointerdown", ev => {
    controls.autoRotate = false; downAt = [ev.clientX, ev.clientY];
  });
  renderer.domElement.addEventListener("pointerup", ev => {
    if (!downAt || Math.hypot(ev.clientX - downAt[0], ev.clientY - downAt[1]) > 5) return;
    const r = renderer.domElement.getBoundingClientRect();
    mouse.x = ((ev.clientX - r.left) / r.width) * 2 - 1;
    mouse.y = -((ev.clientY - r.top) / r.height) * 2 + 1;
    ray.setFromCamera(mouse, camera);
    const pick = deckGroups.filter(g => g.group.visible).flatMap(g => [g.floor, ...g.cabins.filter(c => c.visible)]);
    const hit = ray.intersectObjects(pick, false)[0];
    if (!hit) { if (selDeck !== null) selectDeck(null); return; }
    const di = hit.object.userData.di ?? deckGroups.find(g => g.group === hit.object.parent)?.di;
    selectDeck(selDeck === di && hit.object.userData.kind === "floor" ? null : di);
  });
  function onKey(ev) { if (ev.key === "Escape" && selDeck !== null) selectDeck(null); }
  window.addEventListener("keydown", onKey);
  const homeTarget = new THREE.Vector3();
  function stepDecks() {
    deckGroups.forEach(g => {
      const isSel = g.di === selDeck;
      g.out += ((isSel ? 1 : 0) - g.out) * 0.12;
      g.dim += ((selDeck === null || isSel ? 1 : 0.07) - g.dim) * 0.12;
      g.group.position.x = g.out * OUT_X;
      g.group.position.y = g.baseY + g.out * 2;
      g.floor.material.opacity = g.dim;
      g.cabins.forEach(c => c.material.forEach(m => { m.opacity = 0.98 * g.dim; }));
    });
    const sel = deckGroups.find(g => g.di === selDeck);
    const want = sel ? new THREE.Vector3(OUT_X, sel.baseY, 0) : homeTarget;
    if (camGlide > 0) {
      const d = want.clone().sub(controls.target).multiplyScalar(0.12);
      controls.target.add(d); camera.position.add(d);   // Blickwinkel bleibt, nur verschieben
      camGlide--;
    }
  }

  function onResize() {
    const w = host.clientWidth, h = host.clientHeight;
    camera.aspect = w / h; camera.updateProjectionMatrix();
    renderer.setSize(w, h); composer.setSize(w, h);
  }
  window.addEventListener("resize", onResize);

  let raf;
  (function loop() {
    raf = requestAnimationFrame(loop);
    stepDecks(); controls.update(); updateLabels();
    if (focused) showVenueLabel(focused);
    composer.render();
  })();

  return {
    cabinMeshes,
    venueGroup,
    focusVenue(deck, name) {
      const m = venuePillars.find(p => p.userData.deck === String(deck) && p.userData.venue === name);
      if (!m) return;
      venueGroup.visible = true;
      controls.autoRotate = false;
      setFocus(m);
    },
    dispose() {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", onResize);
      window.removeEventListener("keydown", onKey);
      renderer.domElement.removeEventListener("pointermove", onMove);
      deckLabels.forEach(L => L.el.remove());
      vlabel.remove();
      scene.traverse(o => {
        if (!o.isMesh) return;
        o.geometry.dispose();
        [].concat(o.material).forEach(m => { if (m.map) m.map.dispose(); m.dispose(); });
      });
      composer.dispose(); renderer.dispose();
      host.innerHTML = "";
    }
  };
}

function buildLegend(t) {
  const groups = Object.values(t.free_cabins || {});
  const present = [...new Set(groups)];
  const order = (window.AIDA_GROUPS || []).map(g => g[0]);
  present.sort((a, b) => order.indexOf(a) - order.indexOf(b));
  const el = document.getElementById("d3d-legend");
  const freeBtns = present.map(g => {
    const col = "#" + (GROUPCOL[g] ?? 0xffffff).toString(16).padStart(6, "0");
    return `<button data-g="${g}" onclick="toggleGroup3d('${g}',this)">
      <span class="dot" style="background:${col};color:${col}"></span>${GROUPNAME[g] || g}</button>`;
  }).join("");
  const occBtn = "";   // ausgebuchte Kabinen werden nicht mehr angezeigt
  const venBtn = "";   // Orte nicht in der Legende
  el.innerHTML = freeBtns + occBtn + venBtn;
}
function buildPlaces(t) {
  const el = document.getElementById("d3d-places");
  const v = t.venues || {};
  const decks = Object.keys(v).sort((a, b) => Number(b) - Number(a));
  if (!decks.length) { el.classList.add("hidden-panel"); el.innerHTML = ""; return; }
  el.classList.remove("hidden-panel");
  el.innerHTML = "<h4>Decks &amp; Orte</h4>" + decks.map(d => {
    const names = [...new Set(v[d].map(x => x.name))];
    return `<div class="deck"><b>Deck ${d}</b><div class="v">` + names.map(n =>
      `<a onclick="focusVenue('${d}','${n.replace(/'/g, "\\'")}')">${n}</a>`).join(" · ") + "</div></div>";
  }).join("");
}
window.focusVenue = function (deck, name) {
  if (d3d && d3d.focusVenue) d3d.focusVenue(deck, name);
};
window.toggleGroup3d = function (g, btn) {
  if (!d3d) return;
  const on = btn.classList.toggle("off");   // off = ausblenden
  if (g === "__venues") { d3d.venueGroup.visible = !on; return; }
  d3d.cabinMeshes.forEach(m => { if (m.userData.group === g) m.visible = !on; });
};
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print(build())
