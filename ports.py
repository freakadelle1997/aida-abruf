"""Hafen-Koordinaten für die Routenkarte: daten/ports.json {UN/LOCODE: {name, lat, lon}}.

AIDA liefert im Reiseverlauf UN/LOCODEs (z. B. DEHAM, NOSVG). Koordinaten kommen aus der
offenen UN/LOCODE-Liste, ersatzweise aus OpenStreetMap/Nominatim (höflich, 1 Abfrage/Sek.).
Bereits bekannte Häfen werden nicht erneut gesucht.
"""
import csv
import io
import json
import time
import urllib.parse
import urllib.request

UNLOCODE_CSV = "https://raw.githubusercontent.com/datasets/un-locode/main/data/code-list.csv"
UA = {"User-Agent": "aida-buchungslage (privat)"}


def _coord(s):
    """'5333N 00959E' -> (53.55, 9.983)"""
    try:
        lat, lon = s.split()
        la = int(lat[:2]) + int(lat[2:4]) / 60
        lo = int(lon[:3]) + int(lon[3:5]) / 60
        return (round(-la if lat[-1] == "S" else la, 4), round(-lo if lon[-1] == "W" else lo, 4))
    except (ValueError, IndexError):
        return None


def _nominatim(name):
    q = urllib.parse.urlencode({"q": name, "format": "json", "limit": 1})
    req = urllib.request.Request(f"https://nominatim.openstreetmap.org/search?{q}", headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        hits = json.loads(r.read())
    time.sleep(1.1)
    return (round(float(hits[0]["lat"]), 4), round(float(hits[0]["lon"]), 4)) if hits else None


def update(path, wanted):
    """wanted: {code: name}. Ergänzt fehlende Häfen in path (JSON), gibt Anzahl neuer zurück."""
    known = json.loads(path.read_text()) if path.exists() else {}
    missing = {c: n for c, n in wanted.items()
               if c and c != "SEE" and not c.startswith("XX") and c not in known}
    if not missing:
        return 0
    try:
        with urllib.request.urlopen(urllib.request.Request(UNLOCODE_CSV, headers=UA), timeout=60) as r:
            rows = csv.DictReader(io.TextIOWrapper(r, encoding="utf-8"))
            un = {(row["Country"] + row["Location"]): row.get("Coordinates") for row in rows}
    except Exception as e:
        print(f"ports: UN/LOCODE nicht ladbar: {e}")
        un = {}
    added = 0
    for code, name in sorted(missing.items()):
        c = _coord(un.get(code) or "")
        if not c:
            try:
                c = _nominatim(f"{name}, {code[:2]}") if name else None
            except Exception:
                c = None
        known[code] = {"name": name, "lat": c[0], "lon": c[1]} if c else {"name": name}
        added += 1
    path.write_text(json.dumps(known, ensure_ascii=False, indent=0, sort_keys=True))
    print(f"ports: {added} Häfen ergänzt ({sum(1 for v in known.values() if 'lat' in v)} mit Koordinaten)")
    return added
