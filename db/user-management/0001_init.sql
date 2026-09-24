-- User Management Service — initial schema.
-- Owns: accounts, credentials, refresh tokens, the social graph and both currencies.

CREATE TABLE users (
    user_id      UUID PRIMARY KEY,
    username     TEXT        NOT NULL UNIQUE,
    email        TEXT        NOT NULL UNIQUE,
    display_name TEXT,
    avatar_ref   TEXT,
    status       TEXT        NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'suspended', 'deleted')),
    roles        TEXT[]      NOT NULL DEFAULT ARRAY['user'],
    package_id   UUID        NOT NULL,
    purge_at     TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX users_created_at_idx ON users (created_at, user_id);

CREATE TABLE credentials (
    user_id       UUID PRIMARY KEY REFERENCES users (user_id) ON DELETE CASCADE,
    password_hash TEXT        NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Refresh tokens are stored as a SHA-256 hex digest; the plaintext never touches the database.
CREATE TABLE refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id    UUID        NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    issued_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
);

CREATE INDEX refresh_tokens_user_idx ON refresh_tokens (user_id);

-- A friendship is symmetric and stored as two rows; an enmity is directional and stored as one.
CREATE TABLE relationships (
    user_id       UUID        NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    other_user_id UUID        NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    relation      TEXT        NOT NULL CHECK (relation IN ('friend', 'enemy')),
    since         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, other_user_id),
    CHECK (user_id <> other_user_id)
);

CREATE INDEX relationships_lookup_idx ON relationships (user_id, relation, since, other_user_id);

CREATE TABLE friend_requests (
    request_id   UUID PRIMARY KEY,
    from_user_id UUID        NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    to_user_id   UUID        NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    status       TEXT        NOT NULL
                 CHECK (status IN ('pending', 'accepted', 'declined', 'cancelled')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at  TIMESTAMPTZ,
    CHECK (from_user_id <> to_user_id)
);

-- At most one open request per ordered pair; resolved ones stay for history.
CREATE UNIQUE INDEX friend_requests_pending_uniq
    ON friend_requests (from_user_id, to_user_id) WHERE status = 'pending';
CREATE INDEX friend_requests_incoming_idx ON friend_requests (to_user_id, created_at, request_id);
CREATE INDEX friend_requests_outgoing_idx ON friend_requests (from_user_id, created_at, request_id);

CREATE TABLE balances (
    user_id         UUID PRIMARY KEY REFERENCES users (user_id) ON DELETE CASCADE,
    global_currency BIGINT      NOT NULL DEFAULT 0 CHECK (global_currency >= 0),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per package the user is registered with; the package-local currency of the contract.
CREATE TABLE local_balances (
    user_id    UUID        NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    package_id UUID        NOT NULL,
    amount     BIGINT      NOT NULL DEFAULT 0 CHECK (amount >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, package_id)
);

-- Idempotency ledger for POST /internal/users/{userId}/balances/adjust. A replayed commandId
-- returns the stored result instead of applying the delta twice.
CREATE TABLE balance_operations (
    command_id UUID PRIMARY KEY,
    user_id    UUID        NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    currency   TEXT        NOT NULL CHECK (currency IN ('global', 'local')),
    package_id UUID,
    delta      BIGINT      NOT NULL,
    reason     TEXT        NOT NULL,
    result     JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX balance_operations_user_idx ON balance_operations (user_id, created_at);
