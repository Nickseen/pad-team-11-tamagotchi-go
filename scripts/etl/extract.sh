#!/usr/bin/env bash
# Extracts the service-owned CSV exports into etl/input for the etl job (#194). Run from the CPR root
# with the stack up. Each export is the owning service's own command against its own database; the
# Warehouse never reads a service database itself.
#
#   RAID_CHECKOUT=<Monster Raid checkout> bash scripts/etl/extract.sh
#   docker compose run --rm etl
#
# Raid:   RAID_CHECKOUT points to a Monster Raid checkout at the reviewed producer revision (#194 handoff,
#         c3427817). RAID_DB_SERVICE names the current primary after a promotion (default monster-raid-db).
# Battle: the producer command follows the Battle handoff on #194; until then BATTLE_CSV=<file> copies an
#         existing export, e.g. etl/fixtures/battle.csv. RAID_CSV=<file> does the same for Raid.
# A source with nothing configured is left out (the etl job then skips it). Each file is written to a
# temporary name and moved into place only when its command succeeded, so a failed export never reaches
# the Warehouse.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
out=${ETL_INPUT_DIR:-$root/etl/input}
mkdir -p "$out"

publish() { # name, command...: run the command into <name>.csv.partial, then rename it
    local name=$1 partial="$out/$1.csv.partial"
    shift
    if "$@" > "$partial"; then
        mv "$partial" "$out/$name.csv"
        echo "extract: $name -> $out/$name.csv ($(($(wc -l < "$out/$name.csv") - 1)) rows)"
    else
        rm -f "$partial"
        echo "extract: $name export failed; the previous $name.csv, if any, is kept" >&2
        return 1
    fi
}

raid_export() {
    docker compose --project-directory "$root" exec -T "${RAID_DB_SERVICE:-monster-raid-db}" \
        psql -X -q -v ON_ERROR_STOP=1 -U raid -d raid -f - < "$RAID_CHECKOUT/scripts/export-raid-results.sql"
}

status=0
if [ -n "${RAID_CHECKOUT:-}" ]; then
    publish raid raid_export || status=1
elif [ -n "${RAID_CSV:-}" ]; then
    publish raid cat "$RAID_CSV" || status=1
else
    echo "extract: raid skipped (set RAID_CHECKOUT or RAID_CSV)"
fi

if [ -n "${BATTLE_CSV:-}" ]; then
    publish battle cat "$BATTLE_CSV" || status=1
else
    echo "extract: battle skipped (set BATTLE_CSV until the Battle producer handoff on #194)"
fi
exit "$status"
