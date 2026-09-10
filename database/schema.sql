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
    (10, 'gemini-2.5-flash',          'social_anxiety'),
    (11, 'gemini-2.5-flash',          'rumination'),
    (12, 'gemini-2.5-flash',          'anticipatory_anxiety'),
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


-- ── voice_turns (primary voice conversation data) ───────────────────────────
CREATE TABLE IF NOT EXISTS voice_turns (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    participant_id      UUID NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    session_id          TEXT,
    turn_number         INT NOT NULL,           -- 1 = intro, 2-5 = story, 6 = closing
    whisper_transcript  TEXT,                   -- STT transcript of participant's audio
    llm_response_text   TEXT,                   -- AI text response
    audio_file_url      TEXT,                   -- path in voice-recordings bucket
    tts_voice_used      TEXT,                   -- nova | onyx | alloy
    platform            TEXT,                   -- assigned LLM platform
    hsp_condition       TEXT,
    topic               TEXT,
    response_time_ms    INT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_voice_turns_participant ON voice_turns(participant_id);


-- ── Migration: attention checks (run once in Supabase SQL Editor) ────────────
ALTER TABLE participants ADD COLUMN IF NOT EXISTS attention_check_instruction INT;
ALTER TABLE participants ADD COLUMN IF NOT EXISTS hsps_reverse_1              INT;
ALTER TABLE participants ADD COLUMN IF NOT EXISTS hsps_reverse_13             INT;
ALTER TABLE participants ADD COLUMN IF NOT EXISTS attention_failed            BOOLEAN NOT NULL DEFAULT FALSE;

-- ── Migration: financial worry + education (run once in Supabase SQL Editor) ──
ALTER TABLE participants ADD COLUMN IF NOT EXISTS financial_worry             TEXT;
ALTER TABLE participants ADD COLUMN IF NOT EXISTS education                   TEXT;

-- ── Migration: mental health screening (run once in Supabase SQL Editor) ─────
ALTER TABLE participants ADD COLUMN IF NOT EXISTS mental_health_screening     TEXT;

-- ── Migration: per-topic flow — chat → post-survey × 3 ──────────────────────
-- Each topic now has its own chat session + its own post-survey.
ALTER TABLE participants     ADD COLUMN IF NOT EXISTS topics_completed INT NOT NULL DEFAULT 0;
ALTER TABLE participants     ADD COLUMN IF NOT EXISTS awaiting_survey  BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE survey_responses ADD COLUMN IF NOT EXISTS topic_index      INT;
ALTER TABLE survey_responses ADD COLUMN IF NOT EXISTS ai_name          TEXT;
ALTER TABLE voice_turns      ADD COLUMN IF NOT EXISTS topic_index      INT;
ALTER TABLE voice_turns      ADD COLUMN IF NOT EXISTS ai_name          TEXT;

-- Store BOTH the raw Whisper output AND the (possibly edited) text submitted to the LLM.
-- whisper_transcript      = final submitted text (what AI saw)
-- whisper_transcript_raw  = original Whisper output, never modified
ALTER TABLE voice_turns      ADD COLUMN IF NOT EXISTS whisper_transcript_raw TEXT;

-- Behavioral timing — raw timestamps from the participant's side. Durations are
-- intentionally NOT pre-computed; analyses derive them as:
--   hesitation_ms   = record_started_at - ai_audio_ended_at  (NULL on first turn of a topic)
--   speaking_ms     = record_ended_at   - record_started_at
--   editing_ms      = submitted_at      - preview_shown_at
ALTER TABLE voice_turns      ADD COLUMN IF NOT EXISTS ai_audio_ended_at TIMESTAMPTZ;
ALTER TABLE voice_turns      ADD COLUMN IF NOT EXISTS record_started_at TIMESTAMPTZ;
ALTER TABLE voice_turns      ADD COLUMN IF NOT EXISTS record_ended_at   TIMESTAMPTZ;
ALTER TABLE voice_turns      ADD COLUMN IF NOT EXISTS preview_shown_at  TIMESTAMPTZ;
ALTER TABLE voice_turns      ADD COLUMN IF NOT EXISTS submitted_at      TIMESTAMPTZ;

-- ── Migration: Latin-square topic order (run once in Supabase SQL Editor) ────
-- Each participant now goes through ALL 3 topics in a counterbalanced order.
-- assigned_topic_order ∈ {'ABC','BCA','CAB'}.
ALTER TABLE participants ADD COLUMN IF NOT EXISTS assigned_topic_order TEXT;
ALTER TABLE voice_turns  ADD COLUMN IF NOT EXISTS topic                TEXT;

-- Reset condition_counts to the new 6 platforms × 3 orders design.
-- WARNING: this clears existing counts. Only run if you also delete test participants.
DELETE FROM condition_counts;
INSERT INTO condition_counts (condition_id, platform, topic) VALUES
    (1,  'gpt-4o',                   'ABC'),
    (2,  'gpt-4o',                   'BCA'),
    (3,  'gpt-4o',                   'CAB'),
    (4,  'gpt-4o-mini',              'ABC'),
    (5,  'gpt-4o-mini',              'BCA'),
    (6,  'gpt-4o-mini',              'CAB'),
    (7,  'claude-sonnet-4-6',        'ABC'),
    (8,  'claude-sonnet-4-6',        'BCA'),
    (9,  'claude-sonnet-4-6',        'CAB'),
    (10, 'gemini-2.5-flash',         'ABC'),
    (11, 'gemini-2.5-flash',         'BCA'),
    (12, 'gemini-2.5-flash',         'CAB'),
    (13, 'deepseek-chat',            'ABC'),
    (14, 'deepseek-chat',            'BCA'),
    (15, 'deepseek-chat',            'CAB'),
    (16, 'llama-3.3-70b-versatile',  'ABC'),
    (17, 'llama-3.3-70b-versatile',  'BCA'),
    (18, 'llama-3.3-70b-versatile',  'CAB');

-- ── Migration: data sharing consent (run once in Supabase SQL Editor) ────────
ALTER TABLE participants ADD COLUMN IF NOT EXISTS data_sharing_consent        BOOLEAN;


-- ── Migration: re-record history (run once in Supabase SQL Editor) ───────────
-- Keep every recording attempt for a turn instead of overwriting on re-record.

ALTER TABLE voice_turns ADD COLUMN IF NOT EXISTS total_attempts INT;

-- Per (session_id, turn_number) atomic attempt counter.
CREATE TABLE IF NOT EXISTS voice_turn_attempt_counters (
    session_id    TEXT NOT NULL,
    turn_number   INT  NOT NULL,
    attempt_count INT  NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, turn_number)
);

-- Atomically reserves and returns the next attempt_number for this session_id +
-- turn_number. INSERT ... ON CONFLICT DO UPDATE takes a row-level lock, so
-- concurrent calls for the same key are serialized without a separate advisory lock.
CREATE OR REPLACE FUNCTION next_voice_attempt_number(p_session_id TEXT, p_turn_number INT)
RETURNS INT
LANGUAGE plpgsql
AS $$
DECLARE
    v_count INT;
BEGIN
    INSERT INTO voice_turn_attempt_counters (session_id, turn_number, attempt_count)
    VALUES (p_session_id, p_turn_number, 1)
    ON CONFLICT (session_id, turn_number)
    DO UPDATE SET attempt_count = voice_turn_attempt_counters.attempt_count + 1
    RETURNING attempt_count INTO v_count;

    RETURN v_count;
END;
$$;

-- Every recording attempt for a turn (submitted or abandoned re-records).
-- The attempt that was actually submitted is also reflected in voice_turns
-- (whisper_transcript / whisper_transcript_raw / audio_file_url); this table
-- additionally keeps the discarded takes for traceability.
CREATE TABLE IF NOT EXISTS voice_turn_attempts (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    participant_id         UUID NOT NULL REFERENCES participants(id) ON DELETE CASCADE,
    session_id             TEXT NOT NULL,
    turn_number            INT NOT NULL,
    attempt_number         INT NOT NULL,
    whisper_transcript_raw TEXT,
    audio_file_url         TEXT,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_voice_turn_attempts_lookup
    ON voice_turn_attempts(session_id, turn_number);


-- ── Migration: MBTI rationale free-text (run once) ───────────────────────────
ALTER TABLE survey_responses ADD COLUMN IF NOT EXISTS mbti_rationale TEXT;


-- ── Migration: replace gpt-4o-mini condition with Grok (run once) ────────────
-- Drops the participants that were test-assigned to gpt-4o-mini, then repoints
-- their 3 condition_counts slots (one per topic order) at grok-4.
DELETE FROM participants WHERE assigned_platform = 'gpt-4o-mini';

UPDATE condition_counts
SET platform = 'grok-4', current_count = 0
WHERE platform = 'gpt-4o-mini';


-- ── Migration: scratchpad-drafting behavior per turn (run once) ──────────────
-- Snapshot of the private-notes scratchpad at the moment the participant
-- submitted THIS turn's recording, plus lightweight drafting-behavior signals.
ALTER TABLE voice_turns ADD COLUMN IF NOT EXISTS draft_final_text     TEXT NOT NULL DEFAULT '';
ALTER TABLE voice_turns ADD COLUMN IF NOT EXISTS draft_started_at     TIMESTAMPTZ;
ALTER TABLE voice_turns ADD COLUMN IF NOT EXISTS draft_char_count     INT NOT NULL DEFAULT 0;
ALTER TABLE voice_turns ADD COLUMN IF NOT EXISTS draft_revision_count INT NOT NULL DEFAULT 0;


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


-- ── Migration: replace llama-3.3-70b-versatile with gpt-oss-120b (run once) ──
-- Groq retired llama-3.3-70b-versatile; repoint its 3 condition_counts slots
-- (one per topic order) at openai/gpt-oss-120b, also served via Groq.
UPDATE condition_counts
SET platform = 'openai/gpt-oss-120b'
WHERE platform = 'llama-3.3-70b-versatile';


-- ── Migration: human-readable display_id (run once) ──────────────────────────
-- Storage folders and admin listings used the raw participant UUID, which is
-- painful to browse. display_id is a "MMDD-NN" label (date + per-day sequence
-- number), assigned once at participant creation and used as the Storage
-- folder name going forward instead of the UUID.
ALTER TABLE participants ADD COLUMN IF NOT EXISTS display_id TEXT UNIQUE;
