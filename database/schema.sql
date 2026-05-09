-- ============================================================
-- HSP-LLM Experiment Platform — Supabase Schema
-- Run this once in the Supabase SQL Editor to set up all tables.
-- ============================================================

-- ── participants ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS participants (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prolific_id           TEXT UNIQUE NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Condition assignment
    assigned_platform     TEXT,
    assigned_topic        TEXT,
    condition_id          INT,

    -- Survey scores
    hsps_score            FLOAT,
    bfi_scores            JSONB,

    -- Demographics
    age                   INT,
    gender                TEXT,
    native_english        BOOLEAN,
    ai_usage_frequency    TEXT,
    country               TEXT,

    -- Exclusion
    excluded              BOOLEAN NOT NULL DEFAULT FALSE,
    exclusion_reason      TEXT,

    -- Completion
    completion_code       TEXT,

    -- Raw survey responses (for analysis / AI scoring)
    hsps_responses        JSONB,

    -- Demographics: self-reported MBTI
    self_mbti                TEXT,        -- participant's own MBTI type (e.g., INFJ)

    -- AI HSPS scoring (run after post-survey, stored silently)
    -- LLM rates the participant on all 18 items based on conversation content.
    -- Compare with hsps_responses (human self-report) in analysis.
    ai_hsps_responses        JSONB,       -- {"hsps_1": 1-7, ..., "hsps_18": 1-7}
    ai_hsps_score            FLOAT,       -- mean of the 18 AI scores
    ai_prediction_model      TEXT,        -- which model made the ratings
    ai_prediction_timestamp  TIMESTAMPTZ,

    -- AI MBTI prediction (run after post-survey, stored silently)
    -- LLM infers participant's MBTI type from conversation content.
    -- Compare with self_mbti (human self-report) in analysis.
    ai_mbti_type             TEXT,        -- inferred type, e.g., "INFJ"
    ai_mbti_rationale        TEXT,        -- brief explanation from the LLM
    ai_mbti_model            TEXT,        -- which model made the inference
    ai_mbti_timestamp        TIMESTAMPTZ,

    -- Progress flags
    survey_completed      BOOLEAN NOT NULL DEFAULT FALSE,
    intro_completed       BOOLEAN NOT NULL DEFAULT FALSE,
    chat_completed        BOOLEAN NOT NULL DEFAULT FALSE,
    post_survey_completed BOOLEAN NOT NULL DEFAULT FALSE
);

-- Index for fast prolific_id lookups
CREATE INDEX IF NOT EXISTS idx_participants_prolific_id ON participants(prolific_id);


-- ── conversations ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS conversations (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    participant_id   UUID NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    round_number        INT NOT NULL,   -- 0 = intro, 1-5 = main rounds
    user_message        TEXT NOT NULL,
    user_message_chars  INT,            -- character count of user message
    ai_response         TEXT NOT NULL,
    timestamp           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    response_time_ms    INT
);

CREATE INDEX IF NOT EXISTS idx_conversations_participant ON conversations(participant_id);


-- ── survey_responses (post-interaction) ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS survey_responses (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    participant_id       UUID NOT NULL REFERENCES participants(id) ON DELETE CASCADE,

    -- Section A: Overall impression
    general_empathy      INT,    -- 1-7  (Not at all → Very much)
    satisfaction         INT,    -- 1-7  (Strongly disagree → agree)
    trust                INT,    -- 1-7  (Strongly disagree → agree)
    conversation_quality INT,    -- 1-7  (Very bad → Very good)

    -- Section B: How the AI engaged
    affective_empathy_1      INT,    -- 1-7  "AI experienced similar emotions"
    affective_empathy_2      INT,    -- 1-7  "My emotions were acknowledged"
    cognitive_empathy        INT,    -- 1-7  "AI understood my point of view"
    associative_empathy      INT,    -- 1-7  "AI could identify with my situation"
    emotional_responsiveness INT,    -- 1-7  "AI responded to feelings, not just facts"
    empathic_accuracy        INT,    -- 1-7  "AI understood what I was feeling even unsaid"
    implicit_understanding   INT,    -- 1-7  "AI picked up on hints I hadn't fully expressed"

    -- Section C: Closeness & emotional outcome
    closeness_ios        INT,    -- 1-7  IOS Venn diagram scale
    emotional_relief     INT,    -- 1-7  "Felt better after talking"

    -- Section D: Perceived sycophancy
    perceived_sycophancy INT,    -- 1-7  "AI told me what I wanted to hear"

    -- Bonus
    mbti_guess           TEXT,

    completed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ── condition_counts (balanced random assignment) ────────────────────────────
CREATE TABLE IF NOT EXISTS condition_counts (
    condition_id   INT PRIMARY KEY,
    platform       TEXT NOT NULL,
    topic          TEXT NOT NULL,
    current_count  INT NOT NULL DEFAULT 0
);

-- Seed all 18 conditions (6 platforms × 3 topics)
INSERT INTO condition_counts (condition_id, platform, topic) VALUES
    (1,  'gpt-4o',                   'social_anxiety'),
    (2,  'gpt-4o',                   'rumination'),
    (3,  'gpt-4o',                   'anticipatory_anxiety'),
    (4,  'gpt-4o-mini',              'social_anxiety'),
    (5,  'gpt-4o-mini',              'rumination'),
    (6,  'gpt-4o-mini',              'anticipatory_anxiety'),
    (7,  'claude-sonnet-4-6',        'social_anxiety'),
    (8,  'claude-sonnet-4-6',        'rumination'),
    (9,  'claude-sonnet-4-6',        'anticipatory_anxiety'),
    (10, 'gemini-2.0-flash',          'social_anxiety'),
    (11, 'gemini-2.0-flash',          'rumination'),
    (12, 'gemini-2.0-flash',          'anticipatory_anxiety'),
    (13, 'deepseek-chat',            'social_anxiety'),
    (14, 'deepseek-chat',            'rumination'),
    (15, 'deepseek-chat',            'anticipatory_anxiety'),
    (16, 'llama-3.3-70b-versatile',  'social_anxiety'),
    (17, 'llama-3.3-70b-versatile',  'rumination'),
    (18, 'llama-3.3-70b-versatile',  'anticipatory_anxiety')
ON CONFLICT (condition_id) DO NOTHING;


-- ── Atomic assignment function ───────────────────────────────────────────────
-- Called by the Python backend via supabase.rpc("assign_condition_atomic", {})
-- Finds the condition with the lowest count, picks randomly among ties,
-- increments the count, and returns the selected condition.
CREATE OR REPLACE FUNCTION assign_condition_atomic()
RETURNS TABLE(condition_id INT, platform TEXT, topic TEXT)
LANGUAGE plpgsql
AS $$
DECLARE
    v_min_count  INT;
    v_chosen_id  INT;
BEGIN
    -- Lock all rows to prevent race conditions
    PERFORM pg_advisory_xact_lock(42);  -- arbitrary app-level lock

    -- Find the minimum count
    SELECT MIN(cc.current_count)
    INTO v_min_count
    FROM condition_counts cc;

    -- Pick a random condition tied at the minimum
    SELECT cc.condition_id
    INTO v_chosen_id
    FROM condition_counts cc
    WHERE cc.current_count = v_min_count
    ORDER BY RANDOM()
    LIMIT 1;

    -- Increment
    UPDATE condition_counts
    SET current_count = current_count + 1
    WHERE condition_counts.condition_id = v_chosen_id;

    -- Return the chosen condition
    RETURN QUERY
    SELECT cc.condition_id, cc.platform, cc.topic
    FROM condition_counts cc
    WHERE cc.condition_id = v_chosen_id;
END;
$$;


-- ── Migration: add columns to existing DB (run once in Supabase SQL Editor) ──
-- Run these if the participants table already exists:
--
-- ALTER TABLE conversations    ADD COLUMN IF NOT EXISTS user_message_chars  INT;
-- ALTER TABLE survey_responses ADD COLUMN IF NOT EXISTS general_empathy      INT;
-- ALTER TABLE survey_responses ADD COLUMN IF NOT EXISTS conversation_quality INT;
-- ALTER TABLE survey_responses ADD COLUMN IF NOT EXISTS affective_empathy_1  INT;
-- ALTER TABLE survey_responses ADD COLUMN IF NOT EXISTS affective_empathy_2  INT;
-- ALTER TABLE survey_responses ADD COLUMN IF NOT EXISTS cognitive_empathy    INT;
-- ALTER TABLE survey_responses ADD COLUMN IF NOT EXISTS associative_empathy      INT;
-- ALTER TABLE survey_responses ADD COLUMN IF NOT EXISTS emotional_responsiveness INT;
-- ALTER TABLE survey_responses ADD COLUMN IF NOT EXISTS empathic_accuracy        INT;
-- ALTER TABLE survey_responses ADD COLUMN IF NOT EXISTS implicit_understanding   INT;
-- ALTER TABLE survey_responses ADD COLUMN IF NOT EXISTS closeness_ios        INT;
-- ALTER TABLE survey_responses ADD COLUMN IF NOT EXISTS emotional_relief     INT;
-- ALTER TABLE survey_responses DROP COLUMN IF EXISTS anthropomorphism;
-- ALTER TABLE survey_responses DROP COLUMN IF EXISTS emotional_state;
-- ALTER TABLE survey_responses ADD COLUMN IF NOT EXISTS perceived_sycophancy INT;
-- ALTER TABLE survey_responses ALTER COLUMN closeness_ios TYPE INT;  -- was 1-6, now allows 1-7 (no type change needed)
-- ALTER TABLE participants ADD COLUMN IF NOT EXISTS hsps_responses        JSONB;
-- ALTER TABLE participants ADD COLUMN IF NOT EXISTS ai_hsps_responses     JSONB;
-- ALTER TABLE participants ADD COLUMN IF NOT EXISTS ai_hsps_score         FLOAT;
-- ALTER TABLE participants ADD COLUMN IF NOT EXISTS ai_prediction_model   TEXT;
-- ALTER TABLE participants ADD COLUMN IF NOT EXISTS ai_prediction_timestamp TIMESTAMPTZ;
-- ALTER TABLE participants ADD COLUMN IF NOT EXISTS self_mbti             TEXT;
-- ALTER TABLE participants ADD COLUMN IF NOT EXISTS ai_mbti_type          TEXT;
-- ALTER TABLE participants ADD COLUMN IF NOT EXISTS ai_mbti_rationale     TEXT;
-- ALTER TABLE participants ADD COLUMN IF NOT EXISTS ai_mbti_model         TEXT;
-- ALTER TABLE participants ADD COLUMN IF NOT EXISTS ai_mbti_timestamp     TIMESTAMPTZ;
--
-- If you ran the previous migration (with ai_hsp_prediction etc.), clean up:
-- ALTER TABLE participants DROP COLUMN IF EXISTS ai_hsp_prediction;
-- ALTER TABLE participants DROP COLUMN IF EXISTS ai_prediction_confidence;
-- ALTER TABLE participants DROP COLUMN IF EXISTS ai_prediction_rationale;


-- ── Row-level security (optional, recommended for production) ────────────────
-- Enable RLS and restrict direct table access so only the service role
-- (used by the backend) can read/write data.
--
-- ALTER TABLE participants       ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE conversations      ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE survey_responses   ENABLE ROW LEVEL SECURITY;
-- ALTER TABLE condition_counts   ENABLE ROW LEVEL SECURITY;
--
-- Then create policies that allow only the service_role:
-- CREATE POLICY "service only" ON participants
--   USING (auth.role() = 'service_role');
-- (repeat for each table)
--
-- Use SUPABASE_SERVICE_KEY (not the anon key) in your Railway env vars
-- if you enable RLS.
