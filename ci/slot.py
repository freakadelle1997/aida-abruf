"""Entscheidet im GitHub-Workflow, ob und was abgefragt wird.

GitHub-Cron läuft in UTC. Damit die Abfragen über die Zeitumstellung hinweg stündlich von 8 bis
20 Uhr deutscher Zeit laufen, gibt es zwei Cron-Zeilen (Sommer- und Winterzeit); hier wird nur
die zur aktuellen Berliner Zeit passende ausgeführt. Im 8-Uhr-Lauf kommt der Katalog dazu:
täglich Liste + Details geänderter Reisen ("daily"), montags alle Details ("weekly").
Verspätete GitHub-Starts sind egal, weil nach Cron-Zeile und UTC-Stunde entschieden wird.

    python3 ci/slot.py <event> <cron> <manueller-modus>   -> schreibt run=..., mode=...
"""
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

SUMMER = "0 6-18 * * *"   # MESZ (UTC+2)
WINTER = "0 7-19 * * *"   # MEZ  (UTC+1)

event, cron, manual = (sys.argv[1:] + ["", "", ""])[:3]
now = datetime.now(ZoneInfo("Europe/Berlin"))
dst = bool(now.dst())

if event == "workflow_dispatch":
    run, mode = "yes", (manual or "trips")
elif event == "schedule":
    run = "yes" if (cron == SUMMER and dst) or (cron == WINTER and not dst) else "no"
    utc_hour = datetime.now(ZoneInfo("UTC")).hour
    first_slot = utc_hour == (6 if dst else 7)   # 8-Uhr-Lauf (GitHub-Verspätung < 1 h)
    mode = "trips"
    if run == "yes" and first_slot:
        mode = "weekly" if now.weekday() == 0 else "daily"
else:
    run, mode = "no", "trips"
print(f"run={run}")
print(f"mode={mode}")
print(f"Berlin {now:%Y-%m-%d %H:%M} (Sommerzeit={dst}), cron='{cron}' -> run={run}, mode={mode}", file=sys.stderr)
