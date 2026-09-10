"""
Supabase database helpers.

All functions use the synchronous supabase-py client.
FastAPI runs sync route handlers in a thread pool automatically.
"""
from typing import Optional

from supabase import create_client, Client

from app.config import settings, PROLIFIC_COMPLETION_CODE


def _get_client() -> Client:
    return create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)


# Lazy singleton — recreated if settings change (shouldn't happen in prod)
_db: Optional[Client] = None


def db() -> Client:
    global _db
    if _db is None:
        _db = _get_client()
    return _db


# ── Participant helpers ───────────────────────────────────────────────────────

def get_participant_by_prolific(prolific_id: str) -> Optional[dict]:
    result = db().table("participants").select("*").eq("prolific_id", prolific_id).execute()
    return result.data[0] if result.data else None


def get_participant_by_id(participant_id: str) -> Optional[dict]:
    result = db().table("participants").select("*").eq("id", participant_id).execute()
    return result.data[0] if result.data else None


def create_participant(prolific_id: str) -> dict:
    result = db().table("participants").insert({"prolific_id": prolific_id}).execute()
    return result.data[0]


def get_or_create_participant(prolific_id: str) -> dict:
    existing = get_participant_by_prolific(prolific_id)
    if existing:
        return existing
    return create_participant(prolific_id)


def update_participant(participant_id: str, data: dict) -> dict:
    result = db().table("participants").update(data).eq("id", participant_id).execute()
    return result.data[0]


# ── Assignment ────────────────────────────────────────────────────────────────

def assign_condition(participant_id: str) -> dict:
    """
    Atomically assign the lowest-count condition to the participant.
    Idempotent: if already assigned, returns the existing assignment.
    """
    participant = get_participant_by_id(participant_id)
    if participant and participant.get("assigned_platform"):
        return {
            "condition_id": participant["condition_id"],
            "platform":     participant["assigned_platform"],
            "topic_order":  participant.get("assigned_topic_order"),
        }

    result = db().rpc("assign_condition_atomic", {}).execute()
    condition = result.data[0]

    # NOTE: the `topic` column in condition_counts now holds the topic-order code
    # (e.g. "ABC"), not a single topic. Stored in `assigned_topic_order`.
    update_participant(participant_id, {
        "assigned_platform":     condition["platform"],
        "assigned_topic_order":  condition["topic"],
        "condition_id":          condition["condition_id"],
    })

    return {
        "condition_id": condition["condition_id"],
        "platform":     condition["platform"],
        "topic_order":  condition["topic"],
    }


# ── Survey ────────────────────────────────────────────────────────────────────

def save_survey(participant_id: str, survey_data: dict) -> None:
    update_participant(participant_id, {**survey_data, "survey_completed": True})


# ── Conversations ─────────────────────────────────────────────────────────────

def get_conversation(participant_id: str) -> list[dict]:
    result = (
        db()
        .table("conversations")
        .select("*")
        .eq("participant_id", participant_id)
        .order("round_number")
        .execute()
    )
    return result.data or []


def get_all_voice_turns(participant_id: str) -> list[dict]:
    """All voice turns across all 3 topics, in the order they were played
    (chronological). Used by the AI HSPS/MBTI prediction background tasks,
    since the actual conversation transcript lives here, not in `conversations`
    (that table is unused by the voice pipeline)."""
    result = (
        db()
        .table("voice_turns")
        .select("*")
        .eq("participant_id", participant_id)
        .order("created_at")
        .execute()
    )
    return result.data or []


def save_round(
    participant_id: str,
    round_number: int,
    user_message: str,
    ai_response: str,
    response_time_ms: int,
) -> None:
    db().table("conversations").insert({
        "participant_id":     participant_id,
        "round_number":       round_number,
        "user_message":       user_message,
        "user_message_chars": len(user_message),
        "ai_response":        ai_response,
        "response_time_ms":   response_time_ms,
    }).execute()


def count_chat_rounds(participant_id: str) -> int:
    """Return number of completed chat rounds (excludes intro, round_number=0)."""
    history = get_conversation(participant_id)
    return sum(1 for r in history if r["round_number"] > 0)


# ── Post-survey ───────────────────────────────────────────────────────────────

def save_post_survey(participant_id: str, data: dict) -> None:
    db().table("survey_responses").insert({
        "participant_id": participant_id,
        **data,
    }).execute()
    update_participant(participant_id, {"post_survey_completed": True})


# ── HSP prediction ───────────────────────────────────────────────────────────

def save_hsp_prediction(participant_id: str, data: dict) -> None:
    """Write AI HSP prediction fields (or nulls on failure) to the participant row."""
    update_participant(participant_id, data)


# ── MBTI prediction ──────────────────────────────────────────────────────────

def save_mbti_prediction(participant_id: str, data: dict) -> None:
    """Write AI MBTI prediction fields (or nulls on failure) to the participant row."""
    update_participant(participant_id, data)


# ── Completion code ───────────────────────────────────────────────────────────
# Prolific requires the SAME fixed code for every participant (set in the
# study's "Completion paths" config on Prolific's side) — not a per-participant
# value, which Prolific would reject as a mismatch.

def finalize_participant(participant_id: str) -> str:
    update_participant(participant_id, {"completion_code": PROLIFIC_COMPLETION_CODE})
    return PROLIFIC_COMPLETION_CODE


# ── Voice pipeline ────────────────────────────────────────────────────────────

_BUCKET = "voice-recordings"


def next_voice_attempt_number(session_id: str, turn_number: int) -> int:
    """
    Atomically reserve and return the next attempt_number (1, 2, 3, ...) for this
    session_id + turn_number, via the next_voice_attempt_number() Postgres function.
    Safe under concurrent calls — the row-level lock from INSERT ... ON CONFLICT
    DO UPDATE serializes increments for the same key.
    """
    result = db().rpc("next_voice_attempt_number", {
        "p_session_id": session_id,
        "p_turn_number": turn_number,
    }).execute()
    return result.data


def save_voice_turn_attempt(data: dict) -> None:
    """Insert a row into voice_turn_attempts (one per recording attempt, kept even
    if the participant re-records and this take is never submitted)."""
    try:
        db().table("voice_turn_attempts").insert(data).execute()
    except Exception as exc:
        print(f"[DB] voice_turn_attempts insert failed: {exc}")


def get_voice_turn_attempts(session_id: str, turn_number: int) -> list[dict]:
    """All recording attempts for a given turn, oldest first."""
    result = (
        db().table("voice_turn_attempts")
        .select("*")
        .eq("session_id", session_id)
        .eq("turn_number", turn_number)
        .order("attempt_number")
        .execute()
    )
    return result.data or []


def upload_audio(participant_id: str, session_id: str, turn_number: int,
                 attempt_number: int, audio_bytes: bytes, topic: Optional[str] = None) -> str:
    """Upload WebM audio to Supabase Storage (private bucket).
    Path layout: {participant_id}/{topic}/{session_id}_{turn_number}_{attempt_number}_audio.webm
    Falls back to {participant_id}/ for legacy callers that don't pass a topic.
    Different attempts get different paths, so re-recording never overwrites a prior take.
    Returns the file path or empty string on failure.
    """
    if topic:
        path = f"{participant_id}/{topic}/{session_id}_{turn_number}_{attempt_number}_audio.webm"
    else:
        path = f"{participant_id}/{session_id}_{turn_number}_{attempt_number}_audio.webm"
    try:
        db().storage.from_(_BUCKET).upload(
            path=path,
            file=audio_bytes,
            file_options={"content-type": "audio/webm", "upsert": "true"},
        )
        return path
    except Exception as exc:
        print(f"[Storage] Upload failed for {path}: {exc}")
        return ""


def save_voice_turn(data: dict) -> None:
    """Insert a row into voice_turns."""
    try:
        db().table("voice_turns").insert(data).execute()
    except Exception as exc:
        print(f"[DB] voice_turns insert failed: {exc}")


# ── Health check ──────────────────────────────────────────────────────────────

def check_health() -> dict:
    """Verify the Supabase table connection and storage bucket are reachable."""
    result: dict = {}

    try:
        db().table("participants").select("id").limit(1).execute()
        result["database"] = {"ok": True}
    except Exception as exc:
        result["database"] = {"ok": False, "error": str(exc)}

    try:
        db().storage.from_(_BUCKET).list()
        result["storage"] = {"ok": True}
    except Exception as exc:
        result["storage"] = {"ok": False, "error": str(exc)}

    return result
