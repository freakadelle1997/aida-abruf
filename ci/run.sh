#!/usr/bin/env bash
# Ein Abruf auf GitHub Actions: Datenbank aus dem Daten-Repo aufbauen, abfragen, Daten
# zurückschreiben, Übersicht zu Cloudflare Pages hochladen. Läuft unter xvfb-run (Akamai
# lässt nur einen sichtbaren Browser durch).
#   ci/run.sh trips|daily|weekly|catalog|fleet
set -euo pipefail
MODE="${1:-trips}"
CODE="$GITHUB_WORKSPACE"                 # Checkout des öffentlichen Abruf-Repos (nur Code)
ROOT="$GITHUB_WORKSPACE/daten"           # Checkout des privaten Daten-Repos aida-daten
RT="$RUNNER_TEMP/rt"                     # Laufzeitordner wie auf dem Mac: Code + data/ + daten/
mkdir -p "$RT/data"
cp "$CODE"/*.py "$RT/"
cp -R "$CODE/functions" "$RT/"
ln -sfn "$ROOT" "$RT/daten"
cd "$RT"

python import_db.py daten
python intake.py || echo "intake fehlgeschlagen"
case "$MODE" in
    daily)            python run.py --catalog --incremental || echo "Katalog fehlgeschlagen" ;;
    weekly|catalog|both) python run.py --catalog || echo "Katalog fehlgeschlagen" ;;
esac
case "$MODE" in
    daily|weekly|fleet) python run.py --fleet || echo "Flotte fehlgeschlagen" ;;
esac
if [[ "$MODE" != "catalog" && "$MODE" != "fleet" ]]; then
    python run.py || echo "Abruf mit Fehlern"
fi
python export.py daten

cd "$ROOT"
git config user.name "aida-abruf"
git config user.email "aida-abruf@users.noreply.github.com"
for p in data latest trips.json people.json decks venues ships ports.json catalog.json catalog-history changes fleet README.md; do
    [[ -e "$p" ]] && git add "$p"
done
if ! git diff --cached --quiet; then
    git commit -q -m "Daten $(TZ=Europe/Berlin date '+%Y-%m-%d %H:%M') ($MODE)"
    for i in 1 2 3; do git pull -q --rebase && git push -q && break || sleep 5; done
fi

cd "$RT"
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
