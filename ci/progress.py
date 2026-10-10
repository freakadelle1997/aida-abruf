"""Fortschritt des Laufs nach Cloudflare-KV (Schlüssel "status:lauf"), die Seite zeigt ihn oben an.

    python ci/progress.py start <modus>          Lauf beginnt
    python ci/progress.py phase <text>           neue Phase ohne Zähler
    python ci/progress.py done [fehler]          Lauf beendet
    <befehl> | python ci/progress.py pipe <log>  Ausgabe mitlesen: alles ins (private) Log,
                                                 Zählerzeilen ins öffentliche Protokoll + nach KV
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

NS = "b374e2b29ca245cabc0215d3d4770adc"   # KV-Namespace der Seite (wie intake.py)
KEY = "status:lauf"
STATE = os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "lauf-status.json")
COUNTER = re.compile(r"^\s*(Details|freie Kabinen|Preise) (\d+)/(\d+)")
PHASES = {"Details": "Katalog-Details", "freie Kabinen": "Freie Kabinen (4 Monate)", "Preise": "Preise je Kategorie (4 Monate)"}


def put(st):
    st["updated"] = datetime.now().isoformat(timespec="seconds")
    json.dump(st, open(STATE, "w"))
    acc, mail, key = (os.environ.get(k) for k in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_EMAIL", "CLOUDFLARE_API_KEY"))
    if not (acc and mail and key):
        return
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{acc}/storage/kv/namespaces/{NS}/values/{KEY}",
        data=json.dumps(st).encode(), method="PUT",
        headers={"X-Auth-Email": mail, "X-Auth-Key": key, "content-type": "text/plain"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


def load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


cmd = sys.argv[1]
if cmd == "start":
    put({"state": "running", "mode": sys.argv[2], "phase": "Start", "started": datetime.now().isoformat(timespec="seconds")})
elif cmd == "phase":
    st = load(); st.update(phase=" ".join(sys.argv[2:]), done=None, total=None); put(st)
elif cmd == "done":
    st = load(); st.update(state="error" if len(sys.argv) > 2 and sys.argv[2] != "0" else "done", phase="fertig",
                           finished=datetime.now().isoformat(timespec="seconds"), done=None, total=None)
    put(st)
elif cmd == "pipe":
    log = open(sys.argv[2], "a")
    try:
        pub = open("/dev/fd/3", "w")
    except OSError:
        pub = sys.stderr
    st, last = load(), 0
    for line in sys.stdin:
        log.write(line); log.flush()
        m = COUNTER.match(line)
        if m:
            pub.write(line); pub.flush()
            st.update(phase=PHASES.get(m.group(1), m.group(1)), done=int(m.group(2)), total=int(m.group(3)))
            if time.time() - last > 10 or st["done"] == st["total"]:
                put(st); last = time.time()
