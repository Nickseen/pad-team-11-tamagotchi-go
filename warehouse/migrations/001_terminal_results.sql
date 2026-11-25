-- Data Warehouse schema, version 1 (#194). Applied once by the ETL job, which records it in
-- warehouse.schema_migrations. The Warehouse is analytical: it holds facts loaded from the
-- service-owned CSV exports and is never read or written by a domain service.

CREATE SCHEMA IF NOT EXISTS warehouse;

-- One row per finished battle, keyed by the source and its ID (battle:<battleId>).
CREATE TABLE warehouse.battle_results (
    result_key   text        PRIMARY KEY,
    battle_id    uuid        NOT NULL UNIQUE,
    outcome      text        NOT NULL CHECK (outcome IN ('knockout', 'forfeit', 'timeout')),
    finished_at  timestamptz NOT NULL,
    finished_on  date        NOT NULL,           -- UTC calendar day of finished_at
    loaded_at    timestamptz NOT NULL,           -- first load
    updated_at   timestamptz NOT NULL,           -- last load that changed the row
    load_run_id  bigint      NOT NULL            -- warehouse.etl_runs.run_id of that load
);

-- One row per raid that reached a terminal status, keyed raid:<raidId>.
CREATE TABLE warehouse.raid_results (
    result_key   text        PRIMARY KEY,
    raid_id      uuid        NOT NULL UNIQUE,
    status       text        NOT NULL CHECK (status IN ('defeated', 'expired', 'cancelled')),
    ended_at     timestamptz NOT NULL,
    ended_on     date        NOT NULL,           -- UTC calendar day of ended_at
    loaded_at    timestamptz NOT NULL,
    updated_at   timestamptz NOT NULL,
    load_run_id  bigint      NOT NULL
);

CREATE INDEX battle_results_finished_on ON warehouse.battle_results (finished_on);
CREATE INDEX raid_results_ended_on ON warehouse.raid_results (ended_on);

-- Every ETL load of one source: committed together with its facts, so a run that is interrupted
-- leaves neither facts nor a run row behind.
CREATE TABLE warehouse.etl_runs (
    run_id         bigserial   PRIMARY KEY,
    source         text        NOT NULL CHECK (source IN ('battle', 'raid')),
    source_file    text        NOT NULL,
    started_at     timestamptz NOT NULL,
    finished_at    timestamptz NOT NULL,
    rows_read      integer     NOT NULL,
    rows_inserted  integer     NOT NULL,
    rows_updated   integer     NOT NULL,
    rows_unchanged integer     NOT NULL
);

-- Both sources in one shape, with source-prefixed keys, for queries across them.
CREATE VIEW warehouse.terminal_results AS
    SELECT result_key, 'battle' AS source, outcome AS result, finished_at AS ended_at, finished_on AS ended_on
    FROM warehouse.battle_results
    UNION ALL
    SELECT result_key, 'raid', status, ended_at, ended_on
    FROM warehouse.raid_results;

-- Results per UTC day, source and outcome.
CREATE VIEW warehouse.daily_results AS
    SELECT ended_on AS day, source, result, count(*) AS results
    FROM warehouse.terminal_results
    GROUP BY ended_on, source, result;
