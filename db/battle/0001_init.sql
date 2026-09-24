-- The PvP match runtime, and nothing else. Creature ownership, currency balances and stat
-- definitions live in the services that own them; this schema only references their identifiers.

CREATE TABLE battles (
    battle_id            UUID PRIMARY KEY,
    status               TEXT        NOT NULL CHECK (status IN ('pending', 'active', 'finished', 'declined', 'forfeited', 'expired')),
    package_id           UUID        NOT NULL,
    package_version      TEXT        NOT NULL DEFAULT '',
    current_turn_user_id UUID,
    turn_number          INTEGER     NOT NULL DEFAULT 0,
    winner_id            UUID,
    loser_id             UUID,
    outcome              TEXT        NOT NULL DEFAULT '' CHECK (outcome IN ('', 'knockout', 'forfeit', 'timeout')),
    challenger_advantage DOUBLE PRECISION NOT NULL DEFAULT 1,
    opponent_advantage   DOUBLE PRECISION NOT NULL DEFAULT 1,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at          TIMESTAMPTZ
);

CREATE INDEX battles_created_at_idx ON battles (created_at DESC, battle_id DESC);

-- One row per side. side tells the two apart; the challenger always moves first.
CREATE TABLE battle_participants (
    battle_id               UUID        NOT NULL REFERENCES battles (battle_id) ON DELETE CASCADE,
    side                    TEXT        NOT NULL CHECK (side IN ('challenger', 'opponent')),
    user_id                 UUID        NOT NULL,
    primary_tamagotchi_id   UUID,
    secondary_tamagotchi_id UUID,
    boosts                  UUID[]      NOT NULL DEFAULT '{}',
    consumed_boosts         UUID[]      NOT NULL DEFAULT '{}',
    current_hp              INTEGER     NOT NULL DEFAULT 0,
    max_hp                  INTEGER     NOT NULL DEFAULT 0,
    primary_level           INTEGER     NOT NULL DEFAULT 0,
    secondary_level         INTEGER     NOT NULL DEFAULT 0,
    combat_type             TEXT        NOT NULL DEFAULT '',
    stat_bonus_multiplier   DOUBLE PRECISION NOT NULL DEFAULT 1,
    PRIMARY KEY (battle_id, side)
);

CREATE INDEX battle_participants_user_idx ON battle_participants (user_id);

CREATE TABLE battle_turns (
    battle_id             UUID        NOT NULL REFERENCES battles (battle_id) ON DELETE CASCADE,
    turn_number           INTEGER     NOT NULL,
    actor_id              UUID        NOT NULL,
    action                TEXT        NOT NULL CHECK (action IN ('attack', 'special', 'use_boost')),
    boost_id              UUID,
    damage_dealt          INTEGER     NOT NULL CHECK (damage_dealt >= 0),
    type_multiplier       DOUBLE PRECISION NOT NULL,
    stat_bonus_multiplier DOUBLE PRECISION NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (battle_id, turn_number)
);

-- Client-issued command deduplication: a retried turn returns its original result instead of
-- dealing damage twice. The contract guarantees the result stays available for at least 24 hours.
CREATE TABLE processed_commands (
    command_id UUID PRIMARY KEY,
    battle_id  UUID        NOT NULL REFERENCES battles (battle_id) ON DELETE CASCADE,
    user_id    UUID        NOT NULL,
    result     JSON        NOT NULL, -- json, not jsonb: a replayed command returns the identical body

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX processed_commands_expiry_idx ON processed_commands (expires_at);
