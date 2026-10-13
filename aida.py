"""AIDA-Buchungslage: freie Kabinen und Preise je Unterkategorie einer Reise.

Nutzt die internen JSON-Endpunkte der aida.de-Buchungsstrecke aus einer echten
(sichtbaren) Chrome-Session heraus - headless wird von Akamai geblockt.

    .venv/bin/python aida.py check "<aida.de/finden/...-Link>"
"""
import json
import re
import sys
from contextlib import contextmanager
from datetime import date, datetime
from urllib.parse import quote, urlparse, parse_qs

from playwright.sync_api import sync_playwright

BASE = "https://aida.de"
REQ = "/content/aida-search-and-booking/requests"
TARIFF_CODES = {"LIGHT": "LIG", "CLASSIC": "CLA", "PREMIUM": "IND", "CLASSICAI": "CLAAI",
                "PREMIUMAI": "INDAI", "COMFORTAI": "COMAI"}
PAX_AGES = {"adults": 40, "juveniles": 14, "children": 8, "babies": 1}


class AidaError(RuntimeError):
    pass


class CabinSelectionClosed(AidaError):
    """AIDA meldet "Cabin selection currently not possible" (Kabinenauswahl gesperrt, z. B. nachts)."""


def parse_trip_url(url):
    """'https://aida.de/finden/CO07270106/CLASSIC?pax[adults]=2...' -> Reise-Parameter."""
    u = urlparse(url)
    m = re.search(r"/(?:finden|buchen)/([A-Z]{2,3}\d{7,8})/([A-Z]+)", u.path)
    if not m:
        raise AidaError(f"Kein AIDA-Reiselink erkannt: {url}")
    q = parse_qs(u.query)
    pax = {k: int((q.get(f"pax[{k}]") or q.get(k) or ["0"])[0]) for k in PAX_AGES}
    if not any(pax.values()):
        pax["adults"] = 2
    return {"journey": m.group(1), "tariff": m.group(2), "pax": pax}


def _travellers(pax):
    out = []
    for kind, n in pax.items():
        out += [{"id": len(out) + i + 1, "age": PAX_AGES[kind]} for i in range(n)]
    return out


@contextmanager
def aida_session(entry_url):
    """Öffnet eine Seite von aida.de, damit Akamai-Cookies gesetzt sind, und gibt
    eine Funktion zurück, die Requests aus dieser Seite heraus absetzt."""
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False,
                                    args=["--disable-blink-features=AutomationControlled",
                                          "--window-size=900,700"])
        page = browser.new_context(locale="de-DE").new_page()
        resp = page.goto(entry_url, wait_until="domcontentloaded", timeout=60000)
        if not resp or resp.status != 200:
            browser.close()
            raise AidaError(f"aida.de antwortet mit {resp.status if resp else 'nichts'} (Bot-Schutz?)")
        page.wait_for_timeout(6000)

        def call(path, body=None):
            r = page.evaluate(
                """async ([path, body]) => {
                    const opt = body === null ? {} : {method: 'POST',
                        headers: {'content-type': 'application/json'}, body: JSON.stringify(body)};
                    const r = await fetch(path, Object.assign(opt, {signal: AbortSignal.timeout(45000)}));
                    return {status: r.status, text: await r.text()};
                }""", [path, body])
            if r["status"] != 200:
                raise AidaError(f"{path[:80]} -> HTTP {r['status']}")
            try:
                return json.loads(r["text"])
            except json.JSONDecodeError:
                raise AidaError(f"{path[:80]} -> keine JSON-Antwort")

        def call_many(paths, conc=2, delay=0.5, reduce_js=None):
            """Viele GET-Abrufe parallel aus der Seite heraus (kleiner Worker-Pool mit Pause).
            reduce_js: JS-Funktionsausdruck, der jede Antwort schon in der Seite verkleinert.
            Liefert {path: json | None}; fehlgeschlagene einzeln mit call() nachholen."""
            res = page.evaluate(
                """async ([paths, conc, delay, reduceSrc]) => {
                    const reduce = reduceSrc ? eval(reduceSrc) : null;
                    const out = {}; let i = 0;
                    const worker = async () => {
                        while (i < paths.length) {
                            const p = paths[i++];
                            try {
                                // höchstens 30 s pro Anfrage, sonst hängt der ganze Lauf an einer Antwort
                                const r = await fetch(p, {headers: {accept: 'application/json'}, signal: AbortSignal.timeout(30000)});
                                if (r.status !== 200) out[p] = 'HTTP ' + r.status;
                                else if (reduce) out[p] = JSON.stringify(reduce(await r.json()));
                                else out[p] = await r.text();
                            } catch (e) { out[p] = 'ERR ' + e; }
                            await new Promise(ok => setTimeout(ok, delay * 1000 * (1 + Math.random() / 2)));
                        }
                    };
                    await Promise.all(Array.from({length: conc}, worker));
                    return out;
                }""", [paths, conc, delay, reduce_js])
            parsed = {}
            for pth, txt in res.items():
                if txt.startswith("HTTP 403"):
                    raise AidaError("HTTP 403 – Bot-Schutz, Abbruch")
                try:
                    parsed[pth] = json.loads(txt)
                except (json.JSONDecodeError, TypeError):
                    parsed[pth] = None
            return parsed

        def call_posts(items, conc=2, delay=0.4):
            """Viele POST-Abrufe parallel aus der Seite: items = [(schlüssel, pfad, body)].
            Liefert {schlüssel: json | None}."""
            res = page.evaluate(
                """async ([items, conc, delay]) => {
                    const out = {}; let i = 0;
                    const worker = async () => {
                        while (i < items.length) {
                            const [k, p, body] = items[i++];
                            try {
                                const r = await fetch(p, {method: 'POST', headers: {'content-type': 'application/json'},
                                                          body: JSON.stringify(body), signal: AbortSignal.timeout(30000)});
                                out[k] = r.status === 200 ? await r.text() : 'HTTP ' + r.status;
                            } catch (e) { out[k] = 'ERR ' + e; }
                            await new Promise(ok => setTimeout(ok, delay * 1000 * (1 + Math.random() / 2)));
                        }
                    };
                    await Promise.all(Array.from({length: conc}, worker));
                    return out;
                }""", [[list(x) for x in items], conc, delay])
            parsed = {}
            for k, txt in res.items():
                if txt.startswith("HTTP 403"):
                    raise AidaError("HTTP 403 – Bot-Schutz, Abbruch")
                try:
                    parsed[k] = json.loads(txt)
                except (json.JSONDecodeError, TypeError):
                    parsed[k] = None
            return parsed

        call.many = call_many
        call.posts = call_posts
        try:
            yield call
        finally:
            browser.close()


def _cabin_list_body(journey, start, end, tariff_code, category, travellers):
    ctx = {"requestor": {"agency": {"agencyID": "30449"}, "agent": {"id": "GOOFY", "name": ""}},
           "messageContext": {"touroperatorSessionID": "", "transactionCounter": 0,
                              "touroperatorCode": "AIDA", "touroperatorBrand": "AID",
                              "discriminator": "GlobalMessageContext"},
           "timeStamp": date.today().isoformat(), "discriminator": "StringRequestType"}
    action = {"startDate": start, "endDate": end, "requestedTraveller": travellers,
              "product": {"code": journey, "discriminator": "ProductCodeRequestType"},
              "cruiseCategoryCode": {"code": tariff_code, "discriminator": "CruiseCategoryCodeRequestType"},
              "cabinCategoryCode": {"code": category, "discriminator": "CabinCategoryCodeRequestType"},
              "attributes": [{"code": "CPLFilter", "value": "N"}],
              "discriminator": "SearchCruiseCabinsRequestType"}

    def wrap(content):
        return {"version": "4", "content": {"contentType": "text/json", "stringRequest": content},
                "discriminator": "StringVersionSpecificRequestType"}

    return {"routingHeader": {"routingHeader": {"operatorSystemRouting": "", "gdsAgency": "30449"},
                              "payload": wrap(ctx), "operatorSystemCode": "AIDA"},
            "payload": wrap(dict(ctx, requestedAction=action))}


def _iso(d):
    return datetime.strptime(d, "%d.%m.%Y").date().isoformat()


PRICE_TARIFFS = ["IND", "INDAI", "CLA", "LIG", "CLAAI", "COMAI"]


def _price_body(jid, tc, travellers):
    return {"cruise": {"journeyIdentifier": [jid], "tariffTypes": [tc]},
            "paging": {"entity": "Route", "resultsFrom": 1, "resultsTotal": 20},
            "detailLevel": ["PriceDetails"], "productTypes": ["Cruise", "CruisePackage"],
            "flight": {"allowOpenJaw": False, "allowOneway": True},
            "period": {"start": {"preOffset": 0, "date": date.today().isoformat(), "postOffset": 999}},
            "composition": "none", "passengers": travellers}


def _parse_prices(pr, prices):
    """player.proxy-Antwort -> prices[(tarif, unterkategorie)] = günstigster Kabinenpreis (ohne Flug).
    Gibt es eine Kategorie nur als Flugpaket (z. B. Reisen mit Pflichtflug), wird der Kabinenpreis
    aus dem Paket abgeleitet: Paketpreis minus Flugzuschlag (priceDetails "FlightSupplement")."""
    services = {s["id"]: s for s in (pr or {}).get("services", [])}
    pkg = {}
    for prod in (pr or {}).get("products", []):
        refs = prod["serviceReferences"]
        with_flight = not all("cruiseService" in services.get(r["serviceId"], {}) for r in refs)
        info = prod.get("priceInformation") or {}
        amt = info.get("amount")
        if with_flight and amt:
            amt -= sum(x.get("amount") or 0 for x in info.get("priceDetails") or [] if x.get("priceType") == "FlightSupplement")
        if not amt:
            continue
        for r in refs:
            cs = services.get(r["serviceId"], {}).get("cruiseService")
            if not cs:
                continue
            var = next(v for v in cs["cruiseVariants"] if v["id"] == r["serviceVariantId"])
            key = (var["cabin"]["tariffType"], var["cabin"]["cabinCode"])
            tgt = pkg if with_flight else prices
            if key not in tgt or amt < tgt[key]:
                tgt[key] = amt
    for key, amt in pkg.items():          # nur wo es keinen Preis ohne Flug gibt
        prices.setdefault(key, amt)
    return prices


def fleet_prices(call, ids, log=print, chunk=10):
    """Preise je Unterkategorie (IA, IB, ...) und Tarif für viele Reisen, parallel aus der Seite.
    Liefert {id: {kategorie: {tarif: preis}}}."""
    travellers = _travellers({"adults": 2, "juveniles": 0, "children": 0, "babies": 0})
    out = {}
    for k in range(0, len(ids), chunk):
        part = ids[k:k + chunk]
        items = [(f"{j}|{tc}", f"{REQ}/player.proxy.json?subCategoryCall", _price_body(j, tc, travellers))
                 for j in part for tc in PRICE_TARIFFS]
        res = call.posts(items)
        for key, pr in res.items():
            j = key.split("|")[0]
            p = _parse_prices(pr, {})
            for (tf, code), amt in p.items():
                out.setdefault(j, {}).setdefault(code, {})[tf] = amt
        log(f"  Preise {min(k + chunk, len(ids))}/{len(ids)} Reisen")
    return out


TARIFF_KEYS = {"lig": "LIGHT", "cla": "CLASSIC", "ind": "PREMIUM", "claAl": "CLASSIC ALL IN",
               "indAl": "PREMIUM ALL IN", "comAl": "COMFORT ALL IN", "pau": "PAUSCHAL",
               "pauAl": "PAUSCHAL ALL IN", "see": "SEEREISE", "seeAl": "SEEREISE ALL IN"}


def parse_tariffs(detail):
    """Preise je Kabinenart (I/M/B/V/...) und Tarif aus detail.cruise.json: reiner Kabinenpreis,
    günstigster Preis mit Hin- und Rückflug (+ Flughafen), Frühbucher-Rabatt, Aktionen."""
    out = []
    for v in detail.get("cabinItemsVariant") or []:
        for key, label in TARIFF_KEYS.items():
            t = v.get(key) or {}
            prices = t.get("prices") or []
            if not prices:
                continue
            cabin = min((p["cabinAmount"] for p in prices if p.get("cabinAmount")), default=None)
            two = [p for p in prices if p.get("flightIncluded") and p.get("flightDirection") == "two-way"]
            best = min(two, key=lambda p: p["amount"], default=None)
            # Flugpreisanteil (Hin- und Rückflug) je Abflughafen = Paketpreis - Kabinenpreis
            flights = {}
            for p in two:
                ap, part = p.get("departureAirport"), (p.get("amount") or 0) - (p.get("cabinAmount") or 0)
                if ap and part > 0 and (ap not in flights or part < flights[ap]):
                    flights[ap] = round(part, 2)
            out.append({"cabin_type": v.get("cabinCode"), "tariff": label, "cabin": cabin,
                        "flight": best["amount"] if best else None,
                        "flight_from": best.get("departureAirport") if best else None,
                        "flights": flights,
                        "rabat": min((p.get("amountRabat") or 0 for p in prices), default=0) or None,
                        "notes": sorted({n for p in prices for n in p.get("notes") or []
                                         if not n.startswith("inkl.")}),
                        "campaigns": sorted({c.get("name") for p in prices for c in p.get("campaigns") or []
                                             if c.get("name")})})
    return out


def _dt(s):
    """'21.11.2026, 17:00' -> '2026-11-21T17:00'"""
    if not s:
        return None
    try:
        return datetime.strptime(s, "%d.%m.%Y, %H:%M").isoformat(timespec="minutes")
    except ValueError:
        return None


def parse_itinerary(detail):
    return [{"day": p.get("day"), "code": p.get("code"), "name": p.get("name"),
             "arr": _dt(p.get("arrivalDateTime")), "dep": _dt(p.get("departureDateTime")),
             "tender": bool(p.get("isTenderPort")), "scenic": bool(p.get("isScenicPort"))}
            for p in detail.get("itinerary") or []]


SUBCAT_CACHE = None   # Verzeichnis für den Kategorien-Zwischenspeicher (setzt run.py)
SUBCAT_FRESH_DAYS = 7  # so lange gilt der gespeicherte Stand, ohne AIDA zu fragen
_SUBCAT_MEM = {}       # je Lauf: (Schiff, Variante) -> Kategorien (nur einmal holen)


def subcategories(call, ship, variation, tries=3):
    """Unterkategorien des Schiffs (Code -> (Gruppe, Name)). Pro Lauf nur einmal je Schiff;
    ist der gespeicherte Stand frisch, wird AIDA gar nicht gefragt. Das AIDA-GraphQL antwortet
    gelegentlich mit HTTP 500: kurz wiederholen, sonst den letzten gespeicherten Stand nehmen."""
    import time
    from pathlib import Path
    if (ship, variation) in _SUBCAT_MEM:
        return _SUBCAT_MEM[(ship, variation)]
    cf = Path(SUBCAT_CACHE) / f"kategorien-{ship}-{variation}.json" if SUBCAT_CACHE else None
    if cf and cf.exists() and time.time() - cf.stat().st_mtime < SUBCAT_FRESH_DAYS * 86400:
        names = {k: tuple(v) for k, v in json.loads(cf.read_text()).items()}
        _SUBCAT_MEM[(ship, variation)] = names
        return names
    for k in range(tries):
        try:
            sub = call("/graphql/execute.json/global/subcategories-by-ship-and-category"
                       + quote(f";shipName={ship};variation={variation}", safe=""))
            names = {i["cabin_category_code"]: (i["cabin_category"]["category_code"],
                                               i["cabin_category_specification_name"])
                     for i in sub["data"]["kabinenkategorieSpezifikationList"]["items"]}
            if names:
                if cf:
                    cf.parent.mkdir(parents=True, exist_ok=True)
                    cf.write_text(json.dumps(names, ensure_ascii=False))
                _SUBCAT_MEM[(ship, variation)] = names
                return names
        except AidaError:
            pass
        time.sleep(2 + 2 * k)
    if cf and cf.exists():
        print(f"Kategorien {ship}: GraphQL nicht erreichbar, nutze Zwischenspeicher")
        names = {k: tuple(v) for k, v in json.loads(cf.read_text()).items()}
        _SUBCAT_MEM[(ship, variation)] = names
        return names
    raise AidaError(f"Kategorien für {ship} nicht abrufbar (GraphQL)")


def snapshot(call, trip):
    """Ein vollständiger Abruf: Reise-Stammdaten, Preise, freie Kabinen je Unterkategorie."""
    jid, pax = trip["journey"], trip["pax"]
    tariff_code = TARIFF_CODES.get(trip["tariff"], "IND")
    travellers = _travellers(pax)

    paxpath = "/".join(f"{k}={v}" for k, v in pax.items())
    detail = call(f"{REQ}/detail.cruise.json/language=de/{paxpath}/JourneyIdentifier={jid}.json")
    if not detail.get("ship"):
        raise AidaError(f"Reise {jid} nicht gefunden")
    ship, variation = detail["ship"], detail["shipConfiguration"]
    start, end = _iso(detail["startDate"]), _iso(detail["endDate"])

    # Unterkategorien des Schiffs (statisch, aus dem AIDA-CMS)
    names = subcategories(call, ship, variation)

    ship_cabins = call(f"{REQ}/detail.cabins.json/shipName={quote(ship)}/variation={variation}.json")
    ship_total = len(ship_cabins)

    # Kabinenpreise (reine Kreuzfahrt, ohne Flug) je Tarif. PREMIUM liefert Preise je
    # Unterkategorie (VA, VB ...), CLASSIC nur für die Garantie-Kategorie (BV, IV ...).
    prices = {}  # (tarif, kategorie) -> günstigster Kabinenpreis
    for tc in dict.fromkeys([tariff_code] + PRICE_TARIFFS):
        _parse_prices(call(f"{REQ}/player.proxy.json?subCategoryCall", _price_body(jid, tc, travellers)), prices)

    categories = []
    any_state = False   # liefert keine Kategorie Kabinen mit Status -> Kabine wird zugeteilt
    for code in sorted(names):
        res = call(f"{REQ}/booking.proxy.json?cabinListCall={code}",
                   _cabin_list_body(jid, start, end, tariff_code, code, travellers))
        pl = res.get("payload") or {}
        if "content" not in pl:
            if "not possible" in (pl.get("text") or ""):
                raise CabinSelectionClosed(f"Kabinenauswahl bei AIDA derzeit nicht möglich ({pl.get('text')})")
            raise AidaError(f"Kabinenliste {code}: {pl.get('text') or 'unerwartete Antwort'}")
        sr = pl["content"]["stringResponse"]
        cabins = (sr.get("actionResult") or {}).get("cabins") or []
        any_state = any_state or bool(cabins)
        avail = [c for c in cabins if c["cabin"].get("state") == "Available"]
        nums = sorted(c["cabin"]["number"] for c in avail)
        # Lage (Front/Mid/Back) und Seite (Backbord/Steuerbord) je Kabine
        attrs = {c["cabin"]["number"]: [a["value"] for a in c.get("attributes") or []
                                        if a.get("code") == "CabinFacilities"] for c in avail}
        # 'G00000' ist der Platzhalter der Garantiekabine (CLASSIC), keine echte Kabine
        guarantee = "G00000" in nums
        nums = [n for n in nums if not n.startswith("G")]
        categories.append({"code": code, "group": names[code][0], "name": names[code][1],
                           "guarantee": guarantee, "free": len(nums), "cabins": nums,
                           "attrs": {n: attrs.get(n, []) for n in nums},
                           "prices": {t: a for (t, c), a in prices.items() if c == code}})

    return {"fetched_at": datetime.now().isoformat(timespec="seconds"),
            "journey": jid, "tariff": trip["tariff"], "pax": pax,
            "title": detail.get("title"), "ship": ship, "start": start, "end": end,
            "from": detail.get("fromCity"), "ship_total": ship_total,
            "map_url": (detail.get("imageUrl") or {}).get("mapImage"),
            "ship_cabins": ship_cabins,
            "tariffs": parse_tariffs(detail), "itinerary": parse_itinerary(detail),
            "region": detail.get("region"), "available": detail.get("isCruiseAvailable"),
            "cabin_choice": any_state,
            "free_total": sum(c["free"] for c in categories), "categories": categories}


CATALOG_PATH = (f"{REQ}/search.singleCruise.json/size=3000/sortCriteria=Price/sortDirection=Asc/"
                "pax[adults]=2/pax[juveniles]=0/pax[children]=0/pax[babies]=0.json")


# Verkleinert detail.cruise.json schon im Browser auf die Felder, die der Katalog braucht
DETAIL_REDUCE_JS = """(d) => ({
  region: d.region, isCruiseAvailable: d.isCruiseAvailable,
  imageUrl: {mapImage: (d.imageUrl || {}).mapImage},
  itinerary: (d.itinerary || []).map(p => ({day: p.day, code: p.code, name: p.name,
    arrivalDateTime: p.arrivalDateTime, departureDateTime: p.departureDateTime,
    isTenderPort: p.isTenderPort, isScenicPort: p.isScenicPort})),
  cabinItemsVariant: (d.cabinItemsVariant || []).map(v => {
    const o = {cabinCode: v.cabinCode};
    for (const k of ["lig","cla","ind","claAl","indAl","comAl","pau","pauAl","see","seeAl"]) {
      const t = v[k]; if (!t || !t.prices || !t.prices.length) continue;
      o[k] = {tariffName: t.tariffName, prices: t.prices.map(p => ({cabinAmount: p.cabinAmount, amount: p.amount,
        flightIncluded: p.flightIncluded, flightDirection: p.flightDirection, departureAirport: p.departureAirport,
        amountRabat: p.amountRabat}))};
    }
    return o;
  })
})"""


def catalog_list(call):
    """Kompletter AIDA-Katalog in einer Abfrage; bricht ab, wenn die Liste unvollständig wirkt."""
    r = call(CATALOG_PATH)
    total = r.get("resultsTotal") or 0
    trips = {}
    for it in r.get("cruiseItems") or []:
        for v in it.get("cruiseItemVariant") or []:
            trips[v["journeyIdentifier"]] = {
                "id": v["journeyIdentifier"], "title": it.get("title"), "route": it.get("routeCode"),
                "ship": (v.get("ship") or {}).get("name"), "var": v.get("shipVariation"),
                "start": v.get("startDate"), "end": v.get("endDate"),
                "nights": v.get("duration"), "from": v.get("fromCity"), "to": v.get("toCity"),
                "from_pp": v.get("amountPerPerson"), "from_tariff": v.get("tariffType"),
                "flight": bool(v.get("flightIncluded")), "notes": v.get("notes") or [],
                "campaigns": [{"name": c.get("name"), "until": (c.get("validity") or {}).get("bookingDateTo")}
                              for c in v.get("campaigns") or []],
                "ports": [pt.get("code") for pt in it.get("ports") or [] if pt.get("code")]}
    if total and len(trips) < 0.95 * total:
        raise AidaError(f"Katalog unvollständig: {len(trips)} von {total}")
    return trips, total


def catalog_details(call, trips, ids, log=print, chunk=40, done=None):
    """Details (Region, Reiseverlauf, Tarife inkl. Flug) für ids in trips eintragen.
    Gibt die Liste der erledigten ids zurück (für Wiederaufnahme nach Browser-Abbruch)."""
    path = lambda j: f"{REQ}/detail.cruise.json/language=de/adults=2/juveniles=0/children=0/babies=0/JourneyIdentifier={j}.json"
    done = [] if done is None else done   # wird laufend gefüllt -> bleibt bei Abbruch erhalten
    for k in range(0, len(ids), chunk):
        part = ids[k:k + chunk]
        res = call.many([path(j) for j in part], reduce_js=DETAIL_REDUCE_JS)
        for j in part:
            d = res.get(path(j))
            if d is not None:
                trips[j].update({
                    "region": d.get("region"), "available": d.get("isCruiseAvailable"),
                    "itinerary": [{"code": p["code"], "name": p["name"], "arr": p["arr"], "dep": p["dep"]}
                                  for p in parse_itinerary(d)],
                    "tariffs": [{k2: t[k2] for k2 in ("cabin_type", "tariff", "cabin", "flight", "flight_from")}
                                for t in parse_tariffs(d)],
                    "map": (d.get("imageUrl") or {}).get("mapImage")})
            done.append(j)
        log(f"  Details {len(done)}/{len(ids)}")
    return done


def fleet_free(call, trips, log=print, chunk=5):
    """Freie Kabinen je Unterkategorie für viele Reisen (aus dem Katalog: id, ship, var, start, end).
    Nur PREMIUM, 2 Erwachsene, je Kategorie max. 100 (AIDA-Grenze). Liefert {id: {kat: [nummern]}}.
    Kategorien ohne verwertbare Antwort (Zeitüberschreitung, HTTP-Fehler) werden zweimal langsam
    wiederholt und sonst als None geliefert – NICHT als leere Liste, sonst sähe ein Abruffehler
    wie eine ausverkaufte Kategorie aus. Bricht ab, wenn AIDA die Kabinenauswahl sperrt."""
    travellers = _travellers({"adults": 2, "juveniles": 0, "children": 0, "babies": 0})
    out = {}
    for k in range(0, len(trips), chunk):
        part = trips[k:k + chunk]
        items = []
        for t in part:
            try:
                cats = subcategories(call, t["ship"], t["var"])
            except AidaError as e:
                log(f"  {t['id']}: {e}")
                continue
            for code in sorted(cats):
                items.append((f"{t['id']}|{code}", f"{REQ}/booking.proxy.json?cabinListCall={code}",
                              _cabin_list_body(t["id"], t["start"], t["end"], "IND", code, travellers)))
        todo, res = items, {}
        for attempt in range(3):
            if attempt:
                log(f"  {len(todo)} Kabinenlisten ohne Antwort, Wiederholung {attempt}")
            res.update(call.posts(todo, **({} if not attempt else {"conc": 1, "delay": 1.5})))
            pls = {it[0]: (res.get(it[0]) or {}).get("payload") or {} for it in todo}
            if any("not possible" in (pl.get("text") or "") for pl in pls.values()):
                raise CabinSelectionClosed("Kabinenauswahl bei AIDA derzeit nicht möglich")
            todo = [it for it in todo if "content" not in pls[it[0]]]
            if not todo:
                break
        for key, r in res.items():
            jid, code = key.split("|")
            pl = (r or {}).get("payload") or {}
            if "content" not in pl:
                out.setdefault(jid, {})[code] = None      # unbekannt, nicht ausverkauft
                continue
            cabins = ((pl["content"].get("stringResponse") or {}).get("actionResult") or {}).get("cabins") or []
            nums = sorted(c["cabin"]["number"] for c in cabins if c["cabin"].get("state") == "Available"
                          and not c["cabin"]["number"].startswith("G"))
            out.setdefault(jid, {})[code] = nums
        log(f"  freie Kabinen {min(k + chunk, len(trips))}/{len(trips)} Reisen")
    return out


def search_index(call, max_pages=12):
    """Alle Reisen der AIDA-Suche: Journey-ID -> Angebot (ab-Preis, Aktionen, Hinweise, Häfen).
    Die Häfen hängen an der Route; ihre Zeiten werden auf das Startdatum der Abfahrt verschoben."""
    from datetime import timedelta
    idx = {}
    for p in range(1, 2):
        r = call(CATALOG_PATH)   # ganzer Katalog in einer Abfrage
        items = r.get("cruiseItems") or []
        for it in items:
            base = date.fromisoformat(it["startDate"])
            for v in it.get("cruiseItemVariant") or []:
                shift = date.fromisoformat(v["startDate"]) - base

                def mv(ts):
                    if not ts:
                        return None
                    return (datetime.fromisoformat(ts) + shift).isoformat(timespec="minutes")
                ports = [{"name": pt.get("name"), "code": pt.get("code"),
                          "arr": mv(pt.get("arrivalDateTime")), "dep": mv(pt.get("departureDateTime"))}
                         for pt in it.get("ports") or []]
                idx[v["journeyIdentifier"]] = {
                    "route": it.get("routeCode"), "duration": v.get("duration"),
                    "from_price": v.get("amount"), "from_pp": v.get("amountPerPerson"),
                    "from_tariff": v.get("tariffType"), "notes": v.get("notes") or [],
                    "campaigns": [{"name": c.get("name"), "medium": c.get("medium"),
                                   "until": (c.get("validity") or {}).get("bookingDateTo")}
                                  for c in v.get("campaigns") or []],
                    "amenities": [a[k]["label"] for a in v.get("amenities") or [] for k in a
                                  if isinstance(a[k], dict) and a[k].get("label")],
                    "ports": ports}
    return idx


def print_snapshot(s):
    pct = 100 * (1 - s["free_total"] / s["ship_total"]) if s["ship_total"] else 0
    print(f"{s['title']} | {s['ship']} | {s['start']} bis {s['end']} ab {s['from']} | Tarif {s['tariff']}")
    print(f"Frei: {s['free_total']} von {s['ship_total']} Kabinen -> {pct:.1f} % belegt\n")
    def eur(v):
        return f"{v:,.0f} €".replace(",", ".") if v else "-"

    print(f"{'Kat':4} {'Gr':3} {'Name':40} {'frei':>5} {'ALL IN':>9} {'PREMIUM':>9} {'CLASSIC':>9}")
    for c in s["categories"]:
        free = "Gar." if c["guarantee"] and not c["free"] else c["free"]
        print(f"{c['code']:4} {c['group']:3} {c['name'][:40]:40} {free:>5} "
              f"{eur(c['prices'].get('INDAI')):>9} {eur(c['prices'].get('IND')):>9} {eur(c['prices'].get('CLA')):>9}")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "check":
        sys.exit(__doc__)
    trip = parse_trip_url(sys.argv[2])
    with aida_session(sys.argv[2]) as call:
        snap = snapshot(call, trip)
    print_snapshot(snap)
    with open("last_check.json", "w") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
