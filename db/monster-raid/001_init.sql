CREATE SCHEMA IF NOT EXISTS raid;

CREATE TABLE IF NOT EXISTS raid.raids (
    raid_id UUID PRIMARY KEY,
    guild_id UUID NOT NULL,
    status TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    state JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS raids_guild_status_idx ON raid.raids (guild_id, status);

CREATE TABLE IF NOT EXISTS raid.raid_participants (
    raid_id UUID NOT NULL REFERENCES raid.raids(raid_id) ON DELETE CASCADE,
    user_id UUID NOT NULL,
    tamagotchi_id UUID NOT NULL,
    combat_type TEXT NOT NULL,
    damage_dealt BIGINT NOT NULL,
    attack_count INTEGER NOT NULL,
    joined BOOLEAN NOT NULL,
    joined_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (raid_id, user_id)
);

CREATE TABLE IF NOT EXISTS raid.damage_log (
    raid_id UUID NOT NULL REFERENCES raid.raids(raid_id) ON DELETE CASCADE,
    command_id UUID NOT NULL,
    user_id UUID NOT NULL,
    damage_dealt BIGINT NOT NULL,
    attacked_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (raid_id, command_id)
);

CREATE TABLE IF NOT EXISTS raid.processed_commands (
    raid_id UUID NOT NULL REFERENCES raid.raids(raid_id) ON DELETE CASCADE,
    command_id UUID NOT NULL,
    user_id UUID NOT NULL,
    result JSONB NOT NULL,
    PRIMARY KEY (raid_id, command_id)
);
