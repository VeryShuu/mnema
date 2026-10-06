#!/usr/bin/env bash
# Deploy Mnema on GPU-serve (user@mnema-gpu.internal) iz lokal'nogo HEAD cherez git-bundle.
#
# Sposob: git-bundle (a ne push v GitHub i ne rsync dereva) —
#   - na etom hoste delaetsya bundle tekushhey vetki,
#   - na serve `git fetch <bundle>` + `git reset --hard FETCH_HEAD`,
#   - untracked fayly serve (.env, docker-compose.override.yml) ne trayutsya,
#   - zatrem `docker compose build` + `up -d --force-recreate api worker web-bot`
#     s tremya -f (base + gpu + override).
#
# Ispolzovanie:  ./scripts/deploy-gpu.sh
# Peremenkie:    DEST_SSH=user@mnema-gpu.internal  DEST_DIR=/data_local/docker/mnema
set -euo pipefail

DEST_SSH="${DEST_SSH:-user@mnema-gpu.internal}"
DEST_DIR="${DEST_DIR:-/data_local/docker/mnema}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! git -C "$REPO_DIR" diff --quiet -- || ! git -C "$REPO_DIR" diff --cached --quiet --; then
    echo "ERROR: v $REPO_DIR est nekommitnutyje izmenenija otkhyzhdyonnykh faylov." >&2
    echo "Deploy vykladivaet HEAD; snachala sdelajte commit." >&2
    exit 1
fi

BRANCH="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD)"
COMMIT="$(git -C "$REPO_DIR" rev-parse --short HEAD)"
if [ "$BRANCH" = "HEAD" ]; then
    echo "ERROR: vy v detached HEAD, opredelite vetku." >&2
    exit 1
fi

echo "==> Deploy: vetka $BRANCH @ $COMMIT na $DEST_SSH:$DEST_DIR"

BUNDLE_NAME="mnema-deploy-$(date +%Y%m%d-%H%M%S).bundle"
LOCAL_BUNDLE="/tmp/$BUNDLE_NAME"
trap 'rm -f "$LOCAL_BUNDLE"' EXIT

echo "==> Bundle vetki $BRANCH @ $COMMIT..."
git -C "$REPO_DIR" bundle create "$LOCAL_BUNDLE" "$BRANCH"

echo "==> Kopirujem bundle na servershh..."
scp -q "$LOCAL_BUNDLE" "$DEST_SSH:/tmp/$BUNDLE_NAME"

echo "==> Na serve: fetch bundle + reset --hard + compose up -d --build..."
ssh "$DEST_SSH" bash -s <<EOF
set -euo pipefail
cd "$DEST_DIR"

git fetch --quiet "/tmp/$BUNDLE_NAME" "refs/heads/$BRANCH"
git reset --hard --quiet FETCH_HEAD
echo "    kommit na serve: \$(git log -1 --oneline)"

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.override.yml"
if [ ! -f docker-compose.override.yml ]; then
    echo "    VNI MANIE: net docker-compose.override.yml, bez GPU-override" >&2
    COMPOSE="docker compose -f docker-compose.yml -f docker-compose.gpu.yml"
fi

# esli izmeneny requirements/docker — sobudet zanovo; inache keskh i nothing to do
\$COMPOSE build

# migratsii BDD do zapuska: create_all dobavlyaet tol'ko tablitsy, no ne kolonki
# v sushchestvuyushchikh (esli zabies' shag — Pydantic/SQLAlchemy padayut na
# "no such column" pri permom zhe chtenii Job/VoiceProfile)
echo "    alembic upgrade head..."
# </dev/null: bez nego `compose run` s"edaet ostavshiysya heredoc iz stdin
# (ssh bash -s) — shagi posle nego losevayutsya kak "up: command not found"
\$COMPOSE run --rm --no-deps api python -m alembic upgrade head </dev/null

# --force-recreate: bez nego `up -d` mohet ostavit' sluzhby na STAROM obraze,
# kogda izmeneny tol'ko kod/sloi, a konfiguratsiya compose ne menyalas'
# (proveryalos' na serve: obrazy peresobrany, a konteinery - 2-ndavney datchy).
# redis ne recreationim — on trigger, nego SSOT; persozdanie sbrosit ochered'.
\$COMPOSE up -d --force-recreate api worker web-bot

echo "    proverka zapuska:"
\$COMPOSE ps --format "{{.Name}} {{.Status}}"
sleep 8
echo "    health: \$(curl -sf localhost:8000/health || echo 'NOT READY')"
rm -f "/tmp/$BUNDLE_NAME"
EOF

echo "==> Gotovo."
