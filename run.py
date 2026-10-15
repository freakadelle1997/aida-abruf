"""Täglicher Lauf: alle Reisen aus trips.json abrufen, speichern, Übersicht bauen, benachrichtigen.

    .venv/bin/python run.py           # nur Reisen, die heute noch nicht erfolgreich liefen
    .venv/bin/python run.py --force   # alle Reisen neu abrufen
    .venv/bin/python run.py --only DIE4271024   # nur diese Reise(n) abrufen, Rest bleibt
    .venv/bin/python run.py --catalog           # ganzen AIDA-Katalog inkl. Details (wöchentlich)
    .venv/bin/python run.py --catalog --incremental   # Liste + Details nur neuer/geänderter Reisen
    .venv/bin/python run.py --fleet             # freie Kabinen aller Reisen der nächsten 4 Monate
"""
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path

import aida
import report
import store

HERE = Path(__file__).parent
NACHSCHUB_MIN = 5          # ab so vielen zusätzlichen freien Kabinen je Kategorie gilt es als Nachschub
SHIP_CACHE_DAYS = 7        # Kabinen-Stammdaten je Schiff so lange wiederverwenden


def save_ship_cabins(snap):
    """Kabinen-Stammdaten (Größe, Betten, Lage, Grundriss ...) je Schiff ins Daten-Repo."""
    cabins = snap.pop("ship_cabins", None)
    if not cabins:
        return
    f = store.DB.parent.parent / "daten" / "ships" / f"{snap['journey'][:2].lower()}.json"
    if f.exists() and time.time() - f.stat().st_mtime < SHIP_CACHE_DAYS * 86400:
        return
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"ship": snap["ship"], "cabins": cabins}, ensure_ascii=False))


def notify(title, text):
    """macOS-Mitteilung via osascript, Windows-Mitteilung via PowerShell-Balloon."""
    if sys.platform == "darwin":
        script = f"display notification {json.dumps(text)} with title {json.dumps(title)} sound name \"default\""
        subprocess.run(["osascript", "-e", script], check=False)
    elif sys.platform == "win32":
        ps1 = HERE / "notify.ps1"
        if ps1.exists():
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", str(ps1), "-Title", title, "-Text", text], check=False)
    else:
        print(f"[Mitteilung] {title}: {text}")


def describe_change(con, journey, focus=None):
    """Kurztext der Veränderung zum vorherigen erfolgreichen Abruf, oder None."""
    runs = store.last_ok_runs(con, journey, 2)
    if len(runs) < 2:
        return None
    new, old = runs
    cats = {}
    for r, key in ((old, "old"), (new, "new")):
        for c in con.execute("SELECT * FROM categories WHERE run_id=?", (r["id"],)):
            cats.setdefault(c["code"], {})[key] = c
    parts = []
    f = cats.get(focus, {})
    if f.get("old") and f.get("new"):
        o, n = f["old"], f["new"]
        bits = []
        if n["free"] != o["free"]:
            bits.append(f"{n['free']} frei ({n['free'] - o['free']:+d})")
        if n["price_premium_ai"] and o["price_premium_ai"] and n["price_premium_ai"] != o["price_premium_ai"]:
            bits.append(f"ALL IN {n['price_premium_ai']:,.0f} € ({n['price_premium_ai'] - o['price_premium_ai']:+.0f} €)"
                        .replace(",", "."))
        if bits and n["free"] <= 20:
            bits.append("wird knapp")
        if bits:
            parts.append(f"{focus}: " + ", ".join(bits))
    diff = new["free_total"] - old["free_total"]
    if diff:
        parts.append(f"{diff:+d} freie Kabinen ({new['free_total']} frei)")
    # Nachschub: Kategorien, in denen AIDA spürbar Kabinen nachgeschoben hat
    refill = [f"{code} +{v['new']['free'] - v['old']['free']}" for code, v in sorted(cats.items())
              if v.get("old") and v.get("new") and v["new"]["free"] - v["old"]["free"] >= NACHSCHUB_MIN]
    if refill:
        parts.insert(0, "Nachschub: " + ", ".join(refill))
    sold_out = [code for code, v in cats.items()
                if v.get("old") and v.get("new") and v["old"]["free"] > 0 and v["new"]["free"] == 0]
    if sold_out:
        parts.append("ausverkauft: " + ", ".join(sorted(sold_out)))
    price_moves = []
    for code, v in sorted(cats.items()):
        o, n = v.get("old"), v.get("new")
        for field, tag in (("price_premium_ai", " ALL IN"), ("price_classic", " CLASSIC")):
            if o and n and o[field] and n[field] and abs(n[field] - o[field]) >= 1:
                price_moves.append(f"{code}{tag} {n[field] - o[field]:+.0f} €")
    if price_moves:
        parts.append("Preise: " + ", ".join(price_moves[:6]) + (" …" if len(price_moves) > 6 else ""))
    return "; ".join(parts) or None


def daten_dir():
    return store.DB.parent.parent / "daten"


DETAIL_KEYS = ("region", "available", "itinerary", "tariffs", "map")
TNAME = {"CLASSICAI": "CLASSIC ALL IN", "PREMIUMAI": "PREMIUM ALL IN", "COMFORTAI": "COMFORT ALL IN"}
TCODE = {"IND": "PREMIUM", "INDAI": "PREMIUM ALL IN", "CLA": "CLASSIC", "LIG": "LIGHT",
         "CLAAI": "CLASSIC ALL IN", "COMAI": "COMFORT ALL IN"}


def run_catalog(incremental=False):
    """Katalog (~2.000 Reisen) mit Details -> daten/catalog.json, Änderungen gegenüber dem
    letzten Katalog -> daten/changes/katalog-<datum>.json, Hafen-Koordinaten ergänzen.
    incremental: Details nur für neue Reisen und Reisen mit geändertem ab-Preis neu holen,
    alle anderen behalten ihre Details aus dem letzten Katalog."""
    import ports
    d = daten_dir()
    old_f = d / "catalog.json"
    prev = {t["id"]: t for t in json.loads(old_f.read_text())["trips"]} if old_f.exists() else {}
    url = "https://aida.de/finden/NO07261121/PREMIUM?pax[adults]=2"
    trips, total, todo = None, 0, None
    part_f = d / "catalog-partial.json"   # Zwischenstand der Details (Wiederaufnahme nach Abbruch)

    def checkpoint():
        det = {j: {k: t[k] for k in DETAIL_KEYS if k in t} for j, t in (trips or {}).items() if "tariffs" in t}
        write_atomic(part_f, json.dumps({"date": date.today().isoformat(), "details": det},
                                        ensure_ascii=False, separators=(",", ":")))
    for attempt in range(1, 5):          # Browser-Abbruch -> neues Fenster, weiter beim letzten Stand
        try:
            with aida.aida_session(url) as call:
                if trips is None:
                    trips, total = aida.catalog_list(call)
                    todo = sorted(trips)
                    if incremental and prev:
                        todo = []
                        for j, t in trips.items():
                            o = prev.get(j)
                            if o and "tariffs" in o and o.get("from_pp") == t.get("from_pp"):
                                t.update({k: o[k] for k in DETAIL_KEYS if k in o})   # Details übernehmen
                            else:
                                todo.append(j)
                        todo.sort()
                    # Wiederaufnahme: Details aus dem heutigen Zwischenstand übernehmen
                    if part_f.exists():
                        pt = json.loads(part_f.read_text())
                        if pt.get("date") == date.today().isoformat():
                            for j, det in pt["details"].items():
                                if j in trips:
                                    trips[j].update(det)
                            todo = [j for j in todo if j not in pt["details"]]
                    print(f"Katalog: {len(trips)} Reisen, Details für {len(todo)}", flush=True)
                    print(f"  Details 0/{len(todo)}", flush=True)
                done = []

                def log(m, _n=[0]):
                    print(m, flush=True)
                    if len(done) - _n[0] >= 200:      # Zwischenstand alle 200 Reisen sichern
                        _n[0] = len(done)
                        checkpoint()
                try:
                    aida.catalog_details(call, trips, todo, log=log, done=done)
                finally:
                    todo = [j for j in todo if j not in set(done)]
                    checkpoint()
            break
        except Exception as e:
            print(f"Katalog Versuch {attempt}: {e}", file=sys.stderr, flush=True)
            if trips is None and attempt == 4:
                raise
            time.sleep(20)
    if trips is None:
        return
    missing = sum(1 for t in trips.values() if "tariffs" not in t)
    if missing:
        print(f"Katalog: {missing} Reisen ohne Details", flush=True)
    cat = {"fetched_at": datetime.now().isoformat(timespec="seconds"), "total": total,
           "trips": sorted(trips.values(), key=lambda t: (t["start"] or "", t["id"]))}
    old = prev
    new = {t["id"]: t for t in cat["trips"]}
    if old:
        def low(t):
            return t.get("from_pp")
        ch = {"date": cat["fetched_at"][:10],
              "new": sorted(set(new) - set(old)), "gone": sorted(set(old) - set(new)),
              "price": [{"id": j, "old": low(old[j]), "new": low(new[j]),
                         "ot": old[j].get("from_tariff"), "nt": new[j].get("from_tariff")}   # Tarif des ab-Preises
                        for j in sorted(set(new) & set(old))
                        if low(old[j]) and low(new[j]) and low(old[j]) != low(new[j])]}
        (d / "changes").mkdir(exist_ok=True)
        (d / "changes" / f"katalog-{ch['date']}.json").write_text(json.dumps(ch, ensure_ascii=False, indent=1))
        print(f"Katalog-Änderungen: {len(ch['new'])} neu, {len(ch['gone'])} entfallen, {len(ch['price'])} Preise")
    write_atomic(old_f, json.dumps(cat, ensure_ascii=False, separators=(",", ":")))
    if not todo and part_f.exists():
        part_f.unlink()                   # vollständig -> Zwischenstand nicht mehr nötig
    # Tägliche Momentaufnahme für die Zeitreihe (ab-Preis, günstigster PREMIUM ALL IN je
    # Kabinenart, Aktionen) -> daten/catalog-history/<datum>.json, wird nie überschrieben
    hist = {}
    for t in cat["trips"]:
        tp = {}   # Kabinenart -> Tarif -> günstigster Kabinenpreis (alle Tarife)
        for x in t.get("tariffs") or []:
            if x.get("cabin"):
                tp.setdefault(x["cabin_type"], {})[TNAME.get(x["tariff"], x["tariff"])] = x["cabin"]
        hist[t["id"]] = {"pp": t.get("from_pp"), "tarif": t.get("from_tariff"), "t": tp or None,
                         "camp": [c["name"] for c in t.get("campaigns") or []] or None,
                         "notes": t.get("notes") or None}
    (d / "catalog-history").mkdir(exist_ok=True)
    (d / "catalog-history" / f"{cat['fetched_at'][:10]}.json").write_text(
        json.dumps({"fetched_at": cat["fetched_at"], "trips": hist}, ensure_ascii=False, separators=(",", ":")))
    wanted = {p["code"]: p["name"] for t in cat["trips"] for p in t.get("itinerary") or []}
    ports.update(d / "ports.json", wanted)


FLEET_DAYS = 122   # freie Kabinen für alle Reisen, die in den nächsten ~4 Monaten starten


def write_atomic(path, text):
    """Datei vollständig schreiben oder gar nicht (temp + rename): eine laufende Sicherung ins
    Daten-Repo erwischt so nie eine halb geschriebene Datei."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class BotBlocked(Exception):
    pass


def run_fleet(batch=None):
    """Täglich: freie Kabinen je Kategorie für alle Katalog-Reisen der nächsten 4 Monate.
    Pro Reise daten/fleet/<id>.json: counts {datum: {kat: anzahl}}, cabins (aktueller Stand),
    moves {datum: {"+": neu frei, "-": weg}}, prices {datum: {kat: {tarif: preis}}}.
    Jede Gruppe von 20 Reisen wird sofort gespeichert (nichts geht bei Abbruch verloren)."""
    from datetime import timedelta
    d = daten_dir()
    cat = json.loads((d / "catalog.json").read_text())["trips"]
    lim = (date.today() + timedelta(days=FLEET_DAYS)).isoformat()
    today = date.today().isoformat()
    trips = [t for t in cat if t.get("start") and today <= t["start"] <= lim and t.get("var")]
    fd = d / "fleet"
    fd.mkdir(exist_ok=True)
    # heute schon erledigte Reisen überspringen (Wiederaufnahme nach Abbruch)
    def complete_today(h):            # erledigt = heute Kabinen UND Preise vorhanden
        return today in (h.get("counts") or {}) and today in (h.get("prices") or {})
    done_today = {f.stem for f in fd.glob("*.json") if complete_today(json.loads(f.read_text()))}
    todo = [t for t in trips if t["id"] not in done_today]
    if batch:                         # über die Stundenläufe verteilen: je Lauf nur ein Teil
        todo = todo[:batch]
    print(f"Flotte: {len(trips)} Reisen bis {lim}, offen {len(trips) - len(done_today)}, dieser Lauf {len(todo)}", flush=True)
    if not todo:
        return
    aida.SUBCAT_CACHE = str(d / "ships")
    stamp = datetime.now().isoformat(timespec="minutes")
    # Tageszusammenfassung fürs Dashboard: verschwundene/zurückgekehrte Kabinen und ihr Wert,
    # nur in Kategorien unter 100 (darüber verschiebt sich nur AIDAs sichtbares Fenster)
    summ = {"ts": stamp, "trips": 0, "free": 0, "sold": 0, "back": 0, "sold_value": 0.0, "back_value": 0.0,
            "unpriced": 0, "by_ship": {}, "by_trip": {}}
    dd = d / "dashboard"
    dd.mkdir(exist_ok=True)
    df = dd / f"flotte-{today}.json"
    runs_today = json.loads(df.read_text()) if df.exists() else []
    runs_today.append(summ)

    def type_price(t, code):
        """Ersatz, wenn AIDA für die Kategorie keinen Preis ohne Flug liefert (z. B. Reisen nur
        als Flugpaket): Kabinenpreis der Kabinenart aus den Katalog-Details (PREMIUM, sonst günstigster)."""
        grp = None
        kf = d / "ships" / f"kategorien-{t.get('ship')}-{t.get('var')}.json"
        if kf.exists():
            grp = (json.loads(kf.read_text()).get(code) or [None])[0]
        typ = grp or code[0]
        tp = {}
        for x in t.get("tariffs") or []:
            if x.get("cabin_type") == typ and x.get("cabin"):
                tp[TNAME.get(x["tariff"], x["tariff"])] = x["cabin"]
        return tp.get("PREMIUM") or (min(tp.values()) if tp else None)

    def store(t, cats, tprices):
        f = fd / f"{t['id']}.json"
        h = json.loads(f.read_text()) if f.exists() else {"journey": t["id"], "ship": t["ship"],
                                                          "start": t["start"], "counts": {}, "moves": {}}
        if not any(v is not None for v in cats.values()):
            return                       # gar keine Antwort: nicht speichern, nächster Lauf versucht es neu
        # Kategorien ohne Antwort (None) oder gar nicht abgefragt behalten ihren letzten Stand –
        # sonst zählt ein Abruffehler die ganze Kategorie als verkauft und beim nächsten Mal als zurück
        unknown = {c for c in h.get("cabins") or {} if cats.get(c) is None}
        if unknown:
            summ["unknown"] = summ.get("unknown", 0) + len(unknown)
        cats = {c: v for c, v in cats.items() if v is not None}
        cats.update({c: h["cabins"][c] for c in unknown})
        old = {n for v in (h.get("cabins") or {}).values() for n in v}
        new = {n for v in cats.values() for n in v}
        summ["trips"] += 1
        summ["free"] += len(new)
        if h.get("cabins") is not None:
            pr_prev = (h.get("prices") or {}).get(max(h.get("prices") or {"": 0}), {}) if h.get("prices") else {}

            def value(code):
                p = {k: v for k, v in (tprices.get(code) or {}).items() if v}       # 0-Preise ignorieren
                v = p.get("IND") or (min(p.values()) if p else None)
                if v is None:
                    q = {k: x for k, x in (pr_prev.get(code) or {}).items() if x}
                    v = q.get("PREMIUM") or (min(q.values()) if q else None)
                if v is None:
                    v = type_price(t, code)
                return v
            # Ereignis für die Auswertung im Dashboard (report._fleet_sales): wer verschwand, wer kam,
            # zu welchem Preis, Gesamtzahl frei vorher/nachher (für die Erkennung entzogener Kontingente)
            ev = {"ts": stamp, "g": {}, "b": {}, "v": {}, "n": {}, "f0": len(old), "f1": len(new)}
            for code in set(h["cabins"]) | set(cats):
                o, n = set(h["cabins"].get(code, [])), set(cats.get(code, []))
                if len(o) >= 100 or len(n) >= 100:
                    continue
                gone, back = o - n, n - o
                if not gone and not back:
                    continue
                v = value(code)
                if gone:
                    ev["g"][code] = sorted(gone)
                if back:
                    ev["b"][code] = sorted(back)
                ev["v"][code] = v
                ev["n"][code] = len(n)          # frei danach (0 = ganze Kategorie weg)
                if v is None:
                    summ["unpriced"] += len(gone)
                summ["sold"] += len(gone); summ["back"] += len(back)
                summ["sold_value"] += (v or 0) * len(gone); summ["back_value"] += (v or 0) * len(back)
                sh = summ["by_ship"].setdefault(t["ship"], [0, 0, 0.0])
                sh[0] += len(gone); sh[1] += len(back); sh[2] += (v or 0) * len(gone)
                tr = summ["by_trip"].setdefault(t["id"], [0, 0, 0.0, 0])
                tr[0] += len(gone); tr[1] += len(back); tr[2] += (v or 0) * len(gone)
                if v is None:
                    tr[3] += len(gone)          # verkauft, aber ohne Preis (nicht bewertbar)
            if ev["g"] or ev["b"]:
                h.setdefault("ev", []).append(ev)
        h["choice"] = bool(new)         # keine einzige Kabine -> AIDA teilt zu (keine Kabinenwahl)
        h["counts"][today] = {k: len(v) for k, v in cats.items() if v}
        if h.get("cabins") is not None:
            mv = h["moves"].setdefault(today, {"+": [], "-": []})
            mv["+"] = sorted(set(mv["+"]) | (new - old))
            mv["-"] = sorted(set(mv["-"]) | (old - new))
        h["cabins"] = {k: v for k, v in cats.items() if v}
        if tprices:   # Preise je Unterkategorie und Tarif (Tarifnamen ausgeschrieben)
            h.setdefault("prices", {})[today] = {code: {TCODE.get(tf, tf): amt for tf, amt in tp.items()}
                                                 for code, tp in tprices.items()}
        h["updated"] = stamp
        write_atomic(f, json.dumps(h, ensure_ascii=False, separators=(",", ":")))

    def flush_summary():
        out = dict(summ, sold_value=round(summ["sold_value"]), back_value=round(summ["back_value"]))
        runs_today[-1] = out
        write_atomic(df, json.dumps(runs_today, ensure_ascii=False, separators=(",", ":")))

    url = "https://aida.de/finden/NO07261121/PREMIUM?pax[adults]=2"
    total, n_done = len(todo), 0
    for attempt in range(1, 4):
        try:
            with aida.aida_session(url) as call:
                while todo:
                    part = todo[:20]
                    res = aida.fleet_free(call, part, log=lambda m: None)
                    try:
                        pr = aida.fleet_prices(call, [t["id"] for t in part], log=lambda m: None)
                    except aida.AidaError as e:
                        print(f"Preise: {e}", flush=True)
                        pr = {}
                    for t in part:
                        if res.get(t["id"]):
                            store(t, res[t["id"]], pr.get(t["id"]) or {})
                    flush_summary()          # sofort gespeichert
                    todo = todo[20:]
                    n_done += len(part)
                    print(f"  freie Kabinen {n_done}/{total}", flush=True)
            break
        except aida.CabinSelectionClosed as e:
            print(f"Flotte abgebrochen: {e}", flush=True)
            break
        except Exception as e:
            if "403" in str(e):          # Bot-Schutz: nicht nachhaken, nächster Lauf macht weiter
                print(f"Flotte: Bot-Schutz (403) – Rest im nächsten Lauf", flush=True)
                break
            print(f"Flotte Versuch {attempt}: {e}", file=sys.stderr, flush=True)
            time.sleep(20)
    flush_summary()
    print(f"Flotte: {summ['trips']} Reisen gespeichert, {summ['sold']} Kabinen verschwunden "
          f"(Wert {round(summ['sold_value'])} €), {summ['back']} zurück"
          + (f", {summ['unknown']} Kategorien ohne Antwort (letzter Stand behalten)" if summ.get("unknown") else ""),
          flush=True)


def main(force=False, only=None):
    aida.SUBCAT_CACHE = str(daten_dir() / "ships")
    trips = json.loads(store.live_trips_file().read_text())
    if only:
        trips = [t for t in trips if aida.parse_trip_url(t["url"])["journey"] in only]
    con = store.connect()
    summary, errors = [], []
    closed = None   # AIDA sperrt die Kabinenauswahl -> alle weiteren Reisen sofort als Fehler vermerken
    offers = None   # Reisesuche (ab-Preis, Aktionen, Häfen) einmal je Lauf

    for t in trips:
        trip = aida.parse_trip_url(t["url"])
        jid = trip["journey"]
        label = t.get("label") or jid
        if closed:
            store.save_error(con, jid, label, datetime.now().isoformat(timespec="seconds"), closed)
            continue
        # Jeder geplante Lauf (03:30/09:00/14:00) ruft komplett neu ab -> Tagesverlauf.
        for attempt in (1, 2, 3):
            try:
                with aida.aida_session(t["url"]) as call:
                    snap = aida.snapshot(call, trip)
                    if offers is None:
                        try:
                            offers = aida.search_index(call)
                        except Exception as e:
                            print(f"Reisesuche fehlgeschlagen: {e}", file=sys.stderr)
                            offers = {}
                snap["offer"] = (offers or {}).get(jid)
                save_ship_cabins(snap)
                try:
                    import ports
                    ports.update(daten_dir() / "ports.json",
                                 {p["code"]: p["name"] for p in snap.get("itinerary") or []})
                except Exception as e:
                    print(f"Häfen: {e}", file=sys.stderr)
                if not snap["categories"] or snap["ship_total"] == 0:
                    raise aida.AidaError("leere Antwort (Schnittstelle geändert?)")
                label = snap.get("title") or label  # Name aus der AIDA-Bezeichnung
                store.save_snapshot(con, label, snap)
                print(f"{jid}: {snap['free_total']} von {snap['ship_total']} frei")
                change = describe_change(con, jid, t.get("focus"))
                if change:
                    summary.append(f"{label}: {change}")
                break
            except aida.CabinSelectionClosed as e:
                print(f"{jid}: {e}", file=sys.stderr)
                closed = str(e)
                store.save_error(con, jid, label, datetime.now().isoformat(timespec="seconds"), closed)
                break
            except Exception as e:
                print(f"{jid}: Versuch {attempt} fehlgeschlagen: {e}", file=sys.stderr)
                traceback.print_exc()
                if attempt == 3:
                    store.save_error(con, jid, label, datetime.now().isoformat(timespec="seconds"), str(e))
                    errors.append(f"{label}: {e}")
                else:
                    time.sleep(15 * attempt)

    out = report.build(con)
    print(f"Übersicht: {out}")
    if errors:
        notify("AIDA-Scraper: Fehler", " | ".join(errors)[:240])
    if summary:
        notify("AIDA-Buchungslage", " | ".join(summary)[:240])


if __name__ == "__main__":
    only = sys.argv[sys.argv.index("--only") + 1].split(",") if "--only" in sys.argv else None
    if "--catalog" in sys.argv:
        run_catalog(incremental="--incremental" in sys.argv)
    if "--fleet" in sys.argv:
        run_fleet(batch=int(sys.argv[sys.argv.index("--batch") + 1]) if "--batch" in sys.argv else None)
    if "--catalog" in sys.argv or "--fleet" in sys.argv:
        if "--with-trips" not in sys.argv:
            sys.exit(0)
    main(force="--force" in sys.argv, only=only)
