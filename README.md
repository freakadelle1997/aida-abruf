# AIDA-Abruf

Privates Hobby-Projekt: beobachtet für eine Handvoll Kreuzfahrten, wie sich freie Kabinen und
Preise über die Zeit entwickeln (bester Buchungszeitpunkt). Läuft per GitHub Actions; die
Ergebnisse liegen in einem privaten Repository, die Übersicht ist nicht öffentlich.

- `aida.py` – Abruf (sichtbarer Chrome unter xvfb, Anfragen aus der Seite heraus)
- `run.py` – Lauf-Modi: hinterlegte Reisen, Katalog, freie Kabinen der nächsten 4 Monate
- `report.py` – baut die Übersichtsseite
- `ci/` – Zeitplan (`slot.py`) und Ablauf eines Laufs (`run.sh`)
