#!/usr/bin/env bash
# Ein Abruf auf GitHub Actions: Datenbank aus dem Daten-Repo aufbauen, abfragen, Daten
# zurückschreiben, Übersicht zu Cloudflare Pages hochladen. Läuft unter xvfb-run (Akamai
# lässt nur einen sichtbaren Browser durch).
#   ci/run.sh trips|daily|weekly|catalog|fleet|deploy   (deploy = nur Seite bauen und hochladen)
set -euo pipefail
MODE="${1:-trips}"
CODE="$GITHUB_WORKSPACE"                 # Checkout des öffentlichen Abruf-Repos (nur Code)
ROOT="$GITHUB_WORKSPACE/daten"           # Checkout des privaten Daten-Repos fernweh-daten
RT="$RUNNER_TEMP/rt"                     # Laufzeitordner wie auf dem Mac: Code + data/ + daten/
mkdir -p "$RT/data"
cp "$CODE"/*.py "$RT/"
cp -R "$CODE/functions" "$RT/"
ln -sfn "$ROOT" "$RT/daten"
cd "$RT"
# Ausgaben nicht ins öffentliche Actions-Protokoll: erst in eine temporäre Datei, vor dem
# Commit angehängt an logs/<datum>.log im privaten Daten-Repo
TMPLOG="$RUNNER_TEMP/lauf.log"
exec 3>&1 >>"$TMPLOG" 2>&1
trap 'rc=$?; stop_autosave 2>/dev/null; [[ "$MODE" != "deploy" ]] && python ci_progress.py done $rc; echo "Lauf beendet (Code $rc) – Details im privaten Protokoll" >&3' EXIT
echo "=== $(date '+%F %T') Modus $MODE ==="
# Fortschritt: Zählerzeilen (ohne Reisenamen) live ins öffentliche Protokoll und auf die Seite
cp "$CODE/ci/progress.py" "$RT/ci_progress.py"
progress() { python -u ci_progress.py pipe "$TMPLOG"; }
[[ "$MODE" != "deploy" ]] && python ci_progress.py start "$MODE"

# Zwischenstand sichern: nach jeder Phase ins private Daten-Repo (bei Abbruch geht nur die laufende verloren)
save_data() {
    # flock: Hintergrund-Sicherung und Phasen-Sicherung nie gleichzeitig an git
    ( flock 9
      cd "$ROOT"
      mkdir -p logs && cat "$TMPLOG" >> "logs/$(date +%F).log" && : > "$TMPLOG"
      git config user.name "sync"
      git config user.email "sync@users.noreply.github.com"
      for p in data latest trips.json people.json decks venues ships ports.json catalog.json catalog-partial.json catalog-history changes fleet dashboard ships-img logs README.md; do
          git add -A -- "$p" 2>/dev/null || true      # auch Löschungen (z. B. catalog-partial.json)
      done
      if ! git diff --cached --quiet; then
          git commit -q -m "Daten $(TZ=Europe/Berlin date '+%Y-%m-%d %H:%M') ($1)"
          for i in 1 2 3; do git pull -q --rebase --autostash && git push -q && break || sleep 5; done
      fi ) 9>"$RUNNER_TEMP/git.lock" || echo "Sichern fehlgeschlagen ($1)"
}
# Alles Geholte laufend sichern: alle 2 Minuten ins private Daten-Repo
AUTOSAVE=""
if [[ "$MODE" != "deploy" ]]; then
    ( while sleep 120; do save_data "zwischenstand" >/dev/null 2>&1; done ) &
    AUTOSAVE=$!
fi
stop_autosave() { [[ -n "$AUTOSAVE" ]] && kill "$AUTOSAVE" 2>/dev/null && wait "$AUTOSAVE" 2>/dev/null; AUTOSAVE=""; }

# Selbstheilung: Fehlt der heutige Katalog-Stand (z. B. weil GitHub den 8-Uhr-Lauf verworfen
# hat), holt der nächste Lauf Katalog + freie Kabinen nach – montags komplett.
if [[ "$MODE" == "trips" && "$(date +%H)" -ge 8 && ! -f "$ROOT/catalog-history/$(date +%F).json" ]]; then
    if [[ "$(date +%u)" == "1" ]]; then MODE=weekly; else MODE=daily; fi
    echo "Heutiger Katalog fehlt noch -> Modus $MODE"
fi

python import_db.py daten
if [[ "$MODE" != "deploy" ]]; then
python ci_progress.py phase "Vorbereitung"
python intake.py || echo "intake fehlgeschlagen"
case "$MODE" in
    daily)            python -u run.py --catalog 2>&1 | progress || echo "Katalog fehlgeschlagen" ;;   # täglich alle Details (Preisverlauf)
    weekly|catalog|both) python -u run.py --catalog 2>&1 | progress || echo "Katalog fehlgeschlagen" ;;
esac
[[ "$MODE" =~ ^(daily|weekly|catalog|both)$ ]] && save_data "katalog"
case "$MODE" in
    daily|weekly|fleet) python -u run.py --fleet 2>&1 | progress || echo "Flotte fehlgeschlagen" ;;
esac
[[ "$MODE" =~ ^(daily|weekly|fleet)$ ]] && save_data "flotte"
if [[ "$MODE" != "catalog" && "$MODE" != "fleet" ]]; then
    python ci_progress.py phase "Hinterlegte Reisen"
    python run.py || echo "Abruf mit Fehlern"
fi
python export.py daten
fi

stop_autosave
save_data "$MODE"

# Seite immer mit dem NEUESTEN Seiten-Code bauen (der Lauf kann lange dauern; sonst würde er
# eine zwischenzeitlich veröffentlichte neuere Fassung mit altem Code überschreiben)
git -C "$CODE" pull -q --ff-only || true
cp "$CODE/report.py" "$RT/report.py"
rm -rf "$RT/functions" && cp -R "$CODE/functions" "$RT/"
cd "$RT"
[[ "$MODE" != "deploy" ]] && python ci_progress.py phase "Seite wird veröffentlicht"
rm -rf _public && mkdir -p _public
python - <<'PY'
import sys; sys.path.insert(0, ".")
import report
from pathlib import Path
report.OUT = Path("_public/index.html")
print(report.build())
PY
cp -R functions _public/functions
npx --yes wrangler@4 pages deploy _public --project-name aida --branch main --commit-dirty=true
