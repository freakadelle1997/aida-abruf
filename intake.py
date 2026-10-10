"""Übernimmt eingereichte Reisen aus dem Cloudflare-KV-Speicher in die aktive Reiseliste.

Wird vom nächtlichen Lauf vor dem Scrapen aufgerufen. Liest die Einreichungen (von der
Plus-Schaltfläche der Online-Übersicht) per Cloudflare-API, hängt neue Reisen an
daten/trips.json an, nimmt zum Entfernen vorgemerkte Reisen heraus und löscht die
verarbeiteten Einträge im KV-Speicher.
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import aida
import store

HERE = Path(__file__).parent
MAX_TRIPS = 8   # wie functions/api/submit.js
KV_NAMESPACE = "b374e2b29ca245cabc0215d3d4770adc"
CRED_FILE = Path.home() / ".essensbert-cloudflare"


def _creds():
    import os
    c = {k: os.environ[k] for k in ("CLOUDFLARE_EMAIL", "CLOUDFLARE_API_KEY", "CLOUDFLARE_ACCOUNT_ID")
         if os.environ.get(k)}   # GitHub Actions: aus den Secrets
    if CRED_FILE.exists():
        for line in CRED_FILE.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                c.setdefault(k.strip(), v.strip())
    return c


def _api(method, path, cred):
    url = f"https://api.cloudflare.com/client/v4/accounts/{cred['CLOUDFLARE_ACCOUNT_ID']}{path}"
    req = urllib.request.Request(url, method=method, headers={
        "X-Auth-Email": cred["CLOUDFLARE_EMAIL"], "X-Auth-Key": cred["CLOUDFLARE_API_KEY"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main():
    cred = _creds()
    if not cred.get("CLOUDFLARE_API_KEY"):
        print("intake: keine Cloudflare-Zugangsdaten, übersprungen")
        return
    daten = HERE / "daten"
    if not daten.is_dir():
        print("intake: kein daten-Verzeichnis, übersprungen")
        return

    live = daten / "trips.json"
    if not live.exists():
        seed = HERE / "trips.json"
        live.write_text(seed.read_text() if seed.exists() else "[]\n")
    trips = json.loads(live.read_text())
    have = {aida.parse_trip_url(t["url"])["journey"] for t in trips}

    base = f"/storage/kv/namespaces/{KV_NAMESPACE}"
    try:
        keys = _api("GET", f"{base}/keys", cred).get("result", [])
    except urllib.error.URLError as e:
        print(f"intake: KV nicht erreichbar: {e}")
        return

    added, removed = [], []
    for k in keys:
        name = k["name"]
        if name.startswith("status:"):   # Fortschritt des Abgleichs, keine Einreichung
            continue
        try:
            raw = _api("GET", f"{base}/values/{name}", cred)
        except Exception as e:
            print(f"intake: {name} nicht lesbar: {e}")
            continue
        entry = raw if isinstance(raw, dict) and ("url" in raw or "action" in raw) else None
        if entry is None:
            # values-Endpunkt liefert den rohen Wert, nicht in success/result verpackt
            try:
                entry = json.loads(_raw_value(base, name, cred))
            except Exception:
                continue
        if entry.get("action") == "remove":
            jid = entry.get("journey")
            if jid in have:
                trips = [t for t in trips if aida.parse_trip_url(t["url"])["journey"] != jid]
                have.discard(jid)
                removed.append(jid)
            _delete(base, name, cred)
            continue
        try:
            jid = aida.parse_trip_url(entry["url"])["journey"]
        except Exception:
            _delete(base, name, cred)  # unbrauchbare Einreichung verwerfen
            continue
        if jid not in have and len(have) >= MAX_TRIPS:
            print(f"intake: {jid} abgelehnt – bereits {MAX_TRIPS} Reisen hinterlegt")
        elif jid not in have:
            trips.append({"url": entry["url"], "focus": entry.get("focus") or "IA",
                          "added_by": entry.get("submitted_by")})
            have.add(jid)
            added.append(jid)
        _delete(base, name, cred)  # verarbeitet -> entfernen

    if added or removed:
        live.write_text(json.dumps(trips, ensure_ascii=False, indent=2) + "\n")
    if added:
        print("intake: neue Reisen übernommen:", ", ".join(added))
    if removed:
        print("intake: Reisen entfernt:", ", ".join(removed))
    if not added and not removed:
        print("intake: keine neuen Einreichungen")


def _raw_value(base, name, cred):
    url = (f"https://api.cloudflare.com/client/v4/accounts/{cred['CLOUDFLARE_ACCOUNT_ID']}"
           f"{base}/values/{name}")
    req = urllib.request.Request(url, headers={
        "X-Auth-Email": cred["CLOUDFLARE_EMAIL"], "X-Auth-Key": cred["CLOUDFLARE_API_KEY"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def _delete(base, name, cred):
    try:
        _api("DELETE", f"{base}/values/{name}", cred)
    except Exception as e:
        print(f"intake: {name} nicht gelöscht: {e}")


if __name__ == "__main__":
    main()
