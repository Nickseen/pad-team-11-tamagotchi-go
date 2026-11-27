#!/usr/bin/env bash
# Repeatable ETL check (#194) on a disposable Compose project: only the Warehouse and the etl job run,
# with generated exports that follow the #194 CSV contract. It checks the first load, a repeated run, a
# new and a changed record, an interrupted load and its repetition, rejected input (bad value, duplicate
# ID, wrong header, a date or time that does not exist), a header-only export, and runs the README's
# example queries. Removed at exit.
#   bash scripts/etl/check-etl.sh
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/pad-etl.XXXXXXXX")
export COMPOSE_PROJECT_NAME="pad-etl-$(printf '%s' "${work##*.}" | tr '[:upper:]' '[:lower:]')"
export ETL_INPUT_DIR="$work/input"
mkdir -p "$ETL_INPUT_DIR"
sed 's/=CHANGE_ME$/=disposable-etl-secret/' "$root/.env.example" > "$work/env"

compose() { docker compose --project-name "$COMPOSE_PROJECT_NAME" --env-file "$work/env" -f "$root/docker-compose.yml" "$@"; }
cleanup() { compose --profile etl down -v --remove-orphans >/dev/null 2>&1 || :; rm -rf "$work"; }
trap cleanup EXIT

etl() { compose run --rm -T "$@" etl 2>&1 | grep -E '^(etl|battle|raid|psql)' || :; }
count() { compose exec -T warehouse-db psql -XAt -U warehouse -d warehouse -c "$1"; }
facts() { echo "battle_results $(count 'SELECT count(*) FROM warehouse.battle_results'), raid_results $(count 'SELECT count(*) FROM warehouse.raid_results'), etl_runs $(count 'SELECT count(*) FROM warehouse.etl_runs')"; }

battle() { printf '%s\n' "battleId,outcome,finishedAt" "$@" > "$ETL_INPUT_DIR/battle.csv"; }
raid() { printf '%s\n' "raidId,status,endedAt" "$@" > "$ETL_INPUT_DIR/raid.csv"; }
b1=3c9e1f70-5b2d-4a88-91cf-64bd0a2e7c15 b2=0f4b7c1e-2a6d-4e1f-9c3b-5d8a7e6f1b20
r1=b81f0a63-77de-4f2c-91de-6b2a0c5f7e33 r2=6e2d9a14-8c3f-4b7a-a1d5-2f9c0b8e4a71

compose up -d --wait warehouse-db >/dev/null 2>&1

echo "== first run: the #194 samples"
battle "$b1,knockout,2026-11-24T15:25:00.000Z"
raid "$r1,defeated,2026-11-24T15:30:00.000Z"
etl
echo "   $(facts)"

echo "== repeated run, same exports"
etl
echo "   $(facts)"

echo "== a new record in each export, one changed battle outcome"
battle "$b1,forfeit,2026-11-24T15:25:00.000Z" "$b2,timeout,2026-11-25T00:10:00.000Z"
raid "$r1,defeated,2026-11-24T15:30:00.000Z" "$r2,expired,2026-11-25T09:00:00.000Z"
etl
echo "   $(facts); $b1 is now $(count "SELECT outcome FROM warehouse.battle_results WHERE battle_id = '$b1'")"

echo "== interrupted load: a third raid, the job killed before its commit"
raid "$r1,defeated,2026-11-24T15:30:00.000Z" "$r2,expired,2026-11-25T09:00:00.000Z" \
     "11111111-2222-4333-8444-555555555555,cancelled,2026-11-25T10:00:00.000Z"
compose run -d --name "$COMPOSE_PROJECT_NAME-etl-interrupted" -e ETL_PAUSE_BEFORE_COMMIT=60 etl >/dev/null 2>&1
sleep 8
docker kill "$COMPOSE_PROJECT_NAME-etl-interrupted" >/dev/null
docker rm -f "$COMPOSE_PROJECT_NAME-etl-interrupted" >/dev/null 2>&1 || :
for _ in $(seq 1 10); do [ "$(count "SELECT count(*) FROM pg_stat_activity WHERE datname = 'warehouse' AND pid <> pg_backend_pid() AND state <> 'idle'")" = 0 ] && break; sleep 1; done
echo "   after the kill: $(facts)"
etl
echo "   repeated: $(facts)"

echo "== rejected exports leave the Warehouse unchanged"
raid "not-a-uuid,defeated,2026-11-24T15:30:00.000Z"
etl
raid "$r1,defeated,2026-11-24T15:30:00.000Z" "$r1,expired,2026-11-25T09:00:00.000Z"
etl
printf '%s\n' "raidId,result,endedAt" "$r1,defeated,2026-11-24T15:30:00.000Z" > "$ETL_INPUT_DIR/raid.csv"
etl
battle "$b2,timeout,2026-11-25 00:10:00"
etl
battle "$b2,timeout,2026-02-30T00:10:00.000Z"
etl
raid "$r2,expired,2026-11-25T24:00:00.000Z"
etl
echo "   $(facts)"

echo "== header-only exports"
battle
raid
etl
echo "   $(facts)"

echo "== example queries"
compose exec -T warehouse-db psql -X -U warehouse -d warehouse \
    -c "SELECT day, source, result, results FROM warehouse.daily_results ORDER BY day, source, result" \
    -c "SELECT run_id, source, rows_read, rows_inserted, rows_updated, rows_unchanged FROM warehouse.etl_runs ORDER BY run_id"
