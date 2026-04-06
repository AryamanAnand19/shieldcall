CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- ─── Number Intelligence Database ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nid_numbers (
    e164                TEXT PRIMARY KEY,
    first_seen          TIMESTAMPTZ DEFAULT NOW(),
    last_seen           TIMESTAMPTZ DEFAULT NOW(),
    call_count          INT DEFAULT 0,
    ai_score            FLOAT DEFAULT 0.5,
    ai_score_conf       FLOAT DEFAULT 0.0,
    platform_tag        TEXT[] DEFAULT '{}',
    attestation         CHAR(1),
    cnam                TEXT,
    carrier             TEXT,
    asn                 INT,
    country             CHAR(2),
    is_voip             BOOL DEFAULT false,
    community_votes     JSONB DEFAULT '{"ai": 0, "human": 0, "spam": 0}',
    last_platform_crawl TIMESTAMPTZ,
    flags               TEXT[] DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_nid_last_seen  ON nid_numbers(last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_nid_ai_score   ON nid_numbers(ai_score DESC);
CREATE INDEX IF NOT EXISTS idx_nid_asn        ON nid_numbers(asn) WHERE asn IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_nid_cnam_trgm  ON nid_numbers USING gin (cnam gin_trgm_ops);

-- ─── ASN → Platform Map ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS asn_platform_map (
    asn         INT PRIMARY KEY,
    platform    TEXT NOT NULL,
    confidence  FLOAT DEFAULT 0.9,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO asn_platform_map (asn, platform, confidence) VALUES
    (54328,  'twilio',    0.95),
    (46562,  'twilio',    0.95),
    (19905,  'telnyx',    0.95),
    (398101, 'bandwidth', 0.90),
    (20473,  'vonage',    0.85),
    (63949,  'plivo',     0.85),
    (396982, 'retell',    0.80),
    (16276,  'livekit',   0.75)
ON CONFLICT (asn) DO NOTHING;

-- ─── Users ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email           TEXT UNIQUE NOT NULL,
    phone_e164      TEXT,
    password_hash   TEXT NOT NULL,
    trust_score     FLOAT DEFAULT 0.5,
    is_verified     BOOL DEFAULT false,
    is_premium      BOOL DEFAULT false,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Agent Configs (per-user AI shield persona) ───────────────────────────────
CREATE TABLE IF NOT EXISTS agent_configs (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id             UUID REFERENCES users(id) ON DELETE CASCADE UNIQUE,
    agent_name          TEXT DEFAULT 'Assistant',
    voice               TEXT DEFAULT 'default',
    persona             TEXT DEFAULT 'Professional and helpful.',
    handling_rules      TEXT DEFAULT 'Always get the caller''s name and purpose.',
    active_schedule     TEXT DEFAULT 'always',
    ai_call_handling    TEXT DEFAULT 'handle',
    escalation_triggers TEXT[] DEFAULT ARRAY['urgent', 'emergency'],
    languages           TEXT[] DEFAULT ARRAY['en'],
    is_active           BOOL DEFAULT true,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ─── User Feature Flags ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS user_features (
    user_id     UUID REFERENCES users(id) ON DELETE CASCADE,
    feature     TEXT NOT NULL,
    enabled     BOOL DEFAULT true,
    expires_at  TIMESTAMPTZ,
    source      TEXT DEFAULT 'subscription',
    PRIMARY KEY (user_id, feature)
);

-- ─── Shield Sessions (AI agent call sessions) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS shield_sessions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         UUID REFERENCES users(id),
    call_id         TEXT UNIQUE NOT NULL,
    caller_e164     TEXT NOT NULL,
    called_e164     TEXT,
    status          TEXT DEFAULT 'active',
    -- status: active | monitoring | whisper | takeover | dropped | completed
    ai_score_pre    FLOAT,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    answered_at     TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ,
    whisper_count   INT DEFAULT 0,
    takeover_at     TIMESTAMPTZ,
    summary         TEXT,
    metadata        JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_sessions_call_id  ON shield_sessions(call_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user_id  ON shield_sessions(user_id);

-- ─── Transcript Events ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS transcript_events (
    id          BIGSERIAL PRIMARY KEY,
    session_id  UUID REFERENCES shield_sessions(id) ON DELETE CASCADE,
    ts          FLOAT NOT NULL,
    speaker     TEXT NOT NULL,       -- 'caller' | 'agent' | 'user'
    word        TEXT,
    text        TEXT,                -- full utterance when available
    is_final    BOOL DEFAULT true,
    event_type  TEXT DEFAULT 'word', -- 'word' | 'utterance' | 'control' | 'summary'
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transcript_session ON transcript_events(session_id, ts);

-- ─── Call Records ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS call_records (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id             UUID REFERENCES users(id),
    caller_e164         TEXT,
    called_e164         TEXT,
    started_at          TIMESTAMPTZ DEFAULT NOW(),
    answered_at         TIMESTAMPTZ,
    ended_at            TIMESTAMPTZ,
    duration_sec        INT,
    shield_active       BOOL DEFAULT false,
    ai_score_pre        FLOAT,
    ai_score_post       FLOAT,
    user_label          TEXT,       -- 'ai' | 'human' | 'spam' | null
    shield_session_id   UUID REFERENCES shield_sessions(id),
    summary             TEXT,
    takeover_at         TIMESTAMPTZ,
    whisper_count       INT DEFAULT 0,
    metadata            JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_calls_user_id    ON call_records(user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_calls_caller     ON call_records(caller_e164);

-- ─── Community Votes ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS community_votes (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    number_e164     TEXT NOT NULL,
    user_id         UUID REFERENCES users(id),
    label           TEXT NOT NULL,  -- 'ai' | 'human' | 'spam'
    trust_weight    FLOAT DEFAULT 0.5,
    note            TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_votes_number ON community_votes(number_e164);
CREATE INDEX IF NOT EXISTS idx_votes_user   ON community_votes(user_id);
