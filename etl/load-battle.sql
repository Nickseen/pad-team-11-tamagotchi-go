-- Loads one Battle export (battleId,outcome,finishedAt) from stdin into warehouse.battle_results.
-- Run by etl/load.sh as: psql -v source_file=... -v pause=... -f load-battle.sql < battle.csv
-- One transaction: the facts and the etl_runs row commit together or not at all, so an interrupted
-- load leaves nothing behind and is simply run again. Rows are upserted by battle:<battleId>.
\set ON_ERROR_STOP on
BEGIN;
-- One load at a time.
SELECT pg_advisory_xact_lock(194) AS locked \gset

CREATE TEMP TABLE stage (
    line       bigserial,
    battle_id  text,
    outcome    text,
    finished_at text
) ON COMMIT DROP;
\copy stage (battle_id, outcome, finished_at) FROM pstdin WITH (FORMAT csv, HEADER true)

-- A real UTC instant: the contract's format, and a date and time that exist (no 2026-02-30, no 24:00).
CREATE FUNCTION pg_temp.is_utc_millis(value text) RETURNS boolean LANGUAGE plpgsql AS $f$
BEGIN
    IF value IS NULL OR value !~ '^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d:[0-5]\d\.\d{3}Z$' THEN
        RETURN false;
    END IF;
    PERFORM value::timestamptz;
    RETURN true;
EXCEPTION WHEN datetime_field_overflow OR invalid_datetime_format THEN
    RETURN false;
END $f$;

-- Every field is required and must follow the #194 CSV contract; the first offending rows are reported.
DO $$
DECLARE problems text;
BEGIN
    SELECT string_agg(format('data row %s: %s', line, reason), '; ' ORDER BY line) INTO problems
    FROM (
        SELECT line, CASE
            WHEN battle_id IS NULL OR battle_id !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                THEN format('battleId %L is not a lowercase UUID', battle_id)
            WHEN outcome IS NULL OR outcome NOT IN ('knockout', 'forfeit', 'timeout')
                THEN format('outcome %L is not knockout, forfeit or timeout', outcome)
            WHEN NOT pg_temp.is_utc_millis(finished_at)
                THEN format('finishedAt %L is not a valid RFC 3339 UTC timestamp with milliseconds', finished_at)
            WHEN count(*) OVER (PARTITION BY battle_id) > 1
                THEN format('battleId %s appears more than once', battle_id)
        END AS reason
        FROM stage
    ) checked
    WHERE reason IS NOT NULL;
    IF problems IS NOT NULL THEN
        RAISE EXCEPTION 'battle export rejected, nothing loaded: %', left(problems, 2000);
    END IF;
END $$;

INSERT INTO warehouse.etl_runs (source, source_file, started_at, finished_at, rows_read, rows_inserted,
                                rows_updated, rows_unchanged)
SELECT 'battle', :'source_file', now(), now(), count(*), 0, 0, 0 FROM stage
RETURNING run_id, rows_read \gset

WITH written AS (
    INSERT INTO warehouse.battle_results AS fact
        (result_key, battle_id, outcome, finished_at, finished_on, loaded_at, updated_at, load_run_id)
    SELECT 'battle:' || battle_id, battle_id::uuid, outcome, finished_at::timestamptz,
           (finished_at::timestamptz AT TIME ZONE 'UTC')::date, now(), now(), :run_id
    FROM stage
    ON CONFLICT (result_key) DO UPDATE
        SET outcome = EXCLUDED.outcome, finished_at = EXCLUDED.finished_at, finished_on = EXCLUDED.finished_on,
            updated_at = EXCLUDED.updated_at, load_run_id = EXCLUDED.load_run_id
        WHERE (fact.outcome, fact.finished_at) IS DISTINCT FROM (EXCLUDED.outcome, EXCLUDED.finished_at)
    RETURNING (xmax = 0) AS inserted
)
SELECT count(*) FILTER (WHERE inserted) AS inserted, count(*) FILTER (WHERE NOT inserted) AS updated
FROM written \gset

UPDATE warehouse.etl_runs
SET rows_inserted = :inserted, rows_updated = :updated, rows_unchanged = :rows_read - :inserted - :updated,
    finished_at = clock_timestamp()
WHERE run_id = :run_id;

-- ETL_PAUSE_BEFORE_COMMIT: hold the transaction open, to show that an interrupted load leaves nothing.
SELECT pg_sleep(:pause) AS slept \gset
COMMIT;
\echo battle: run :run_id read :rows_read inserted :inserted updated :updated
