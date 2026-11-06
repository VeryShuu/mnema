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
#                FORCE_DEPLOY=1  — propustit' blokirovku po aktivnym zapisjam
#                API_CHECK_URL   — URL API dlja proverki (po umolch.: http://HOST:8000)
#
# Pered deplojem skript proveryayet, net li zhivykh zapisey (launching/recording/
# uploading): `up -d --force-recreate web-bot` ubivaet zhivoy brauzer poserede
# vstrechi (job 32, 06.11.2026: 6,2 MB sobrano, vstrecha propala). Pri aktivnykh
# vstrechakh deploy ostanavlivaetsja; FORCE_DEPLOY=1 peremikaet blokirovku.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Real'nyy adres serve (dev-mashina): scripts/deploy-gpu.local.env (untracked, v git ne pishetsya)
[ -f "$REPO_DIR/scripts/deploy-gpu.local.env" ] && . "$REPO_DIR/scripts/deploy-gpu.local.env"

DEST_SSH="${DEST_SSH:-user@mnema-gpu.internal}"
DEST_DIR="${DEST_DIR:-/data_local/docker/mnema}"
API_CHECK_URL="${API_CHECK_URL:-http://$(printf '%s' "$DEST_SSH" | sed 's/.*@//'):8000}"

if ! git -C "$REPO_DIR" diff --quiet -- || ! git -C "$REPO_DIR" diff --cached --quiet --; then
    echo "ERROR: v $REPO_DIR est nekommitnutyje izmenenija otkhyzhdyonnykh faylov." >&2
    echo "Deploy vykladivaet HEAD; snachala sdelajte commit." >&2
    exit 1
fi

# --- blokirovka po aktivnym zapisjam ---------------------------------------
# web-bot perezhivayet `up -d --force-recreate` s umeršim brauzerom: zapis'
# obryvayetsja poserede vstrechi. Sprashivaem API serve o tekushchikh jobakh;
# zapisi v hodke (launching/recording/uploading) — ostanavlivajemsja.
# Proverka idet DO kakih-libo izmeneniy na serve (fetch/reset ešhe nidne).
JOBS_JSON="$(curl -sf --max-time 10 "$API_CHECK_URL/api/jobs" || true)"
CHECK_SH="$REPO_DIR/scripts/check-active-jobs.sh"
if [ -z "$JOBS_JSON" ]; then
    echo "VNI MANIE: API $API_CHECK_URL/api/jobs ne otvechal — aktivnyye zapisi NE provereny." >&2
elif [ ! -f "$CHECK_SH" ]; then
    echo "VNI MANIE: net $CHECK_SH — aktivnyye zapisi NE provereny." >&2
else
    set +e
    ACTIVE_LIST="$(printf '%s' "$JOBS_JSON" | bash "$CHECK_SH" -)"
    CHECK_RC=$?
    set -e
    case "$CHECK_RC" in
        0) echo "==> Aktivnykh zapisey net — prodolzhajem." ;;
        1)
            if [ "${FORCE_DEPLOY:-0}" = "1" ]; then
                echo "VNI MANIE: idut aktivnyye zapisi, no FORCE_DEPLOY=1 — prodolzhajem na svoy strakh:" >&2
                printf '%s\n' "$ACTIVE_LIST" >&2
            else
                echo "ERROR: idut aktivnyye zapisi — deploy ostanovlen, chtoby ne ubit' zhivyy brauzer web-bot:" >&2
                printf '%s\n' "$ACTIVE_LIST" >&2
                echo "Dokanchivajte/otmenyte vstrechi ili povtorite s FORCE_DEPLOY=1." >&2
                exit 1
            fi
            ;;
        *) echo "VNI MANIE: ne razobral otvet API — aktivnyye zapisi NE provereny." >&2 ;;
    esac
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
