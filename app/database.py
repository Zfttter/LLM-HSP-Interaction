"""
Supabase database helpers.

All functions use the synchronous supabase-py client.
FastAPI runs sync route handlers in a thread pool automatically.
"""
import random
import string
from typing import Optional

from supabase import create_client, Client

from app.config import settings


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
            "topic":        participant["assigned_topic"],
        }

    result = db().rpc("assign_condition_atomic", {}).execute()
    condition = result.data[0]

    update_participant(participant_id, {
        "assigned_platform": condition["platform"],
        "assigned_topic":    condition["topic"],
        "condition_id":      condition["condition_id"],
    })

    return condition


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

def generate_completion_code() -> str:
    chars = string.ascii_uppercase + string.digits
    p1 = "".join(random.choices(chars, k=4))
    p2 = "".join(random.choices(chars, k=4))
    return f"HSP-{p1}-{p2}"


def finalize_participant(participant_id: str) -> str:
    code = generate_completion_code()
    update_participant(participant_id, {"completion_code": code})
    return code


# ── Voice pipeline ────────────────────────────────────────────────────────────

_BUCKET = "voice-recordings"


def upload_audio(participant_id: str, session_id: str, turn_number: int, audio_bytes: bytes) -> str:
    """Upload WebM audio to Supabase Storage (private bucket). Returns file path or empty string."""
    path = f"{participant_id}_{session_id}_{turn_number}_audio.webm"
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
