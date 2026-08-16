"""
API endpoints — JSON in, JSON out (or redirects for form submissions).
"""
import uuid
from fastapi import APIRouter, BackgroundTasks, Request, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse, RedirectResponse

import app.database as db_
from app.assignment import score_hsps, score_bfi, check_exclusion
from app.llm import call_llm
from app.voice import transcribe_audio, text_to_speech
from app.models import SurveySubmission, IntroSubmission, ChatMessage, PostSurveySubmission
from app.hsp_prediction import run_hsp_prediction
from app.mbti_prediction import run_mbti_prediction
from app.config import (
    CONVERSATION_ROUNDS, build_system_prompt, MAX_TURNS,
    turn_phase, PER_TOPIC_TURNS, NUM_TOPICS,
    AI_NAMES, ai_name_for_topic, current_topic_for_participant, opening_message,
)

import hashlib
import pathlib

_history_cache: dict = {}
_pending_cache: dict = {}

# Persistent disk cache for greeting TTS audio.
# Cache key includes a hash of the text so any prompt change invalidates the cache.
_TTS_CACHE_DIR = pathlib.Path(".tts_cache")
_TTS_CACHE_DIR.mkdir(exist_ok=True)


def _get_greeting_tts(ai_name: str, voice: str, is_first_topic: bool, text: str) -> str:
    # 8-char hash of the text — if the opening message changes, cache miss & regenerate.
    text_hash = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    key = f"{ai_name}_{voice}_{'first' if is_first_topic else 'repeat'}_{text_hash}.b64"
    path = _TTS_CACHE_DIR / key
    if path.exists():
        return path.read_text()
    tts_b64 = text_to_speech(text, voice)
    try:
        path.write_text(tts_b64)
    except Exception as exc:
        print(f"[TTS cache] write failed for {key}: {exc}")
    return tts_b64

router = APIRouter()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_participant(request: Request) -> dict:
    pid = request.session.get("participant_id")
    if not pid:
        raise HTTPException(status_code=401, detail="Session expired. Please start over.")
    participant = db_.get_participant_by_id(pid)
    if not participant:
        raise HTTPException(status_code=401, detail="Participant not found.")
    return participant


# ── Consent ───────────────────────────────────────────────────────────────────

@router.post("/consent")
def consent(request: Request):
    prolific_id = request.session.get("prolific_id", "").strip()
    if not prolific_id:
        # Accept prolific_id posted from the form (manual entry / testing)
        return RedirectResponse(url="/", status_code=302)

    participant = db_.get_or_create_participant(prolific_id)
    request.session["participant_id"] = participant["id"]

    # Resume if returning participant
    from app.routers.pages import _next_step
    return RedirectResponse(url=_next_step(participant), status_code=302)


@router.post("/consent-form")
async def consent_form(request: Request):
    """Handle consent form with prolific_id submitted as form field."""
    form = await request.form()
    prolific_id = str(form.get("prolific_id", "")).strip()
    if prolific_id:
        request.session["prolific_id"] = prolific_id

    if not prolific_id:
        return RedirectResponse(url="/?error=missing_id", status_code=302)

    participant = db_.get_or_create_participant(prolific_id)
    request.session["participant_id"] = participant["id"]

    from app.routers.pages import _next_step
    return RedirectResponse(url=_next_step(participant), status_code=302)


# ── Survey ────────────────────────────────────────────────────────────────────

@router.post("/survey")
async def submit_survey(request: Request):
    participant = _require_participant(request)

    form = await request.form()

    # Parse all fields
    def fi(key: str) -> int:
        return int(form.get(key, 0))

    raw = {
        **{f"hsps_{i}": fi(f"hsps_{i}") for i in range(1, 19)},
        **{f"bfi_{i}": fi(f"bfi_{i}") for i in range(1, 45)},
    }

    attention_check_instruction = fi("attention_check_instruction")
    hsps_reverse_1  = fi("hsps_reverse_1")
    hsps_reverse_13 = fi("hsps_reverse_13")

    age = fi("age")
    gender = str(form.get("gender", ""))
    native_english = str(form.get("native_english", "no"))
    ai_usage = str(form.get("ai_usage", "never"))
    country = str(form.get("country", ""))
    race = str(form.get("race", ""))
    financial_worry = str(form.get("financial_worry", ""))
    education = str(form.get("education", ""))
    mental_health_screening = str(form.get("mental_health_screening", "no"))
    self_mbti = str(form.get("self_mbti", "")).strip().upper() or None
    if self_mbti == "UNKNOWN":
        self_mbti = None

    # Validate ranges
    for i in range(1, 19):
        if not 1 <= raw[f"hsps_{i}"] <= 7:
            return RedirectResponse(url="/survey?error=invalid", status_code=302)
    for i in range(1, 45):
        if not 1 <= raw[f"bfi_{i}"] <= 5:
            return RedirectResponse(url="/survey?error=invalid", status_code=302)

    # Scores
    hsps_score = score_hsps(raw)
    bfi_scores = score_bfi(raw)

    # Exclusion check
    excluded, reason = check_exclusion(age, native_english, ai_usage, mental_health_screening)

    # Attention check
    attention_failed = (
        attention_check_instruction != 4
        or abs(raw["hsps_1"]  - (8 - hsps_reverse_1))  >= 5
        or abs(raw["hsps_13"] - (8 - hsps_reverse_13)) >= 5
    )

    survey_data = {
        "hsps_score": hsps_score,
        "hsps_responses": {f"hsps_{i}": raw[f"hsps_{i}"] for i in range(1, 19)},
        "bfi_scores": bfi_scores,
        "age": age,
        "gender": gender,
        "native_english": native_english.lower() == "yes",
        "ai_usage_frequency": ai_usage,
        "country": country,
        "race": race,
        "financial_worry": financial_worry,
        "education": education,
        "mental_health_screening": mental_health_screening,
        "excluded": excluded,
        "exclusion_reason": reason,
        "self_mbti": self_mbti,
        "survey_completed": True,
        "attention_check_instruction": attention_check_instruction,
        "hsps_reverse_1":              hsps_reverse_1,
        "hsps_reverse_13":             hsps_reverse_13,
        "attention_failed":            attention_failed,
    }

    db_.save_survey(participant["id"], survey_data)

    if excluded:
        return RedirectResponse(url="/screened-out", status_code=302)

    # Assign condition (guard: only if not already assigned)
    if not participant.get("assigned_platform"):
        db_.assign_condition(participant["id"])

    return RedirectResponse(url="/intro", status_code=302)


# ── Intro ─────────────────────────────────────────────────────────────────────

@router.post("/intro")
async def submit_intro(request: Request):
    participant = _require_participant(request)
    if participant.get("intro_completed"):
        return JSONResponse({"ok": True})

    db_.update_participant(participant["id"], {"intro_completed": True})
    return JSONResponse({"ok": True})


# ── Chat ──────────────────────────────────────────────────────────────────────

@router.post("/chat")
def chat_message(request: Request, payload: ChatMessage):
    participant = _require_participant(request)

    if participant.get("chat_completed"):
        print("[CHAT 400] chat_completed=True")
        return JSONResponse({"error": "Conversation already completed."}, status_code=400)

    message = payload.message.strip()
    if not message:
        print("[CHAT 400] empty message")
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    # Load full history (including intro)
    history = db_.get_conversation(participant["id"])
    rounds_done = sum(1 for r in history if r["round_number"] > 0)
    print(f"[CHAT DEBUG] rounds_done={rounds_done}, history_len={len(history)}")

    if rounds_done >= CONVERSATION_ROUNDS:
        print("[CHAT 400] max rounds reached")
        db_.update_participant(participant["id"], {"chat_completed": True})
        return JSONResponse({"error": "Maximum rounds reached."}, status_code=400)

    # Build message list for LLM
    messages: list[dict] = []
    for row in sorted(history, key=lambda r: (r["round_number"], r.get("timestamp", ""))):
        messages.append({"role": "user", "content": row["user_message"]})
        messages.append({"role": "assistant", "content": row["ai_response"]})
    messages.append({"role": "user", "content": message})

    platform = participant.get("assigned_platform", "gpt-4o")

    try:
        ai_response, response_time = call_llm(platform, messages)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"LLM error: {str(exc)}")

    new_round = rounds_done + 1
    db_.save_round(
        participant_id=participant["id"],
        round_number=new_round,
        user_message=message,
        ai_response=ai_response,
        response_time_ms=response_time,
    )

    is_final = new_round >= CONVERSATION_ROUNDS
    if is_final:
        db_.update_participant(participant["id"], {"chat_completed": True})

    return JSONResponse({
        "ai_response": ai_response,
        "round": new_round,
        "total_rounds": CONVERSATION_ROUNDS,
        "is_final": is_final,
    })


# ── Voice pipeline ───────────────────────────────────────────────────────────

_ALLOWED_VOICES = {"nova", "onyx", "alloy"}
_VOICE_PREVIEW_TEXT = "Hi there! I'm really glad you're here. I'm looking forward to our conversation."


@router.get("/voice-preview")
def voice_preview(request: Request, voice: str = "nova"):
    if voice not in _ALLOWED_VOICES:
        return JSONResponse({"error": "Invalid voice"}, status_code=400)
    tts_b64 = text_to_speech(_VOICE_PREVIEW_TEXT, voice)
    return JSONResponse({"ok": True, "tts_b64": tts_b64})


@router.post("/voice-select")
async def voice_select(request: Request):
    data = await request.json()
    voice = data.get("voice", "nova")
    if voice not in _ALLOWED_VOICES:
        return JSONResponse({"error": "Invalid voice"}, status_code=400)
    request.session["tts_voice"] = voice
    return JSONResponse({"ok": True})


@router.get("/greeting")
def get_greeting(request: Request):
    import time
    t0 = time.perf_counter()

    participant_id = request.session.get("participant_id")
    if not participant_id:
        return JSONResponse({"error": "No session"}, status_code=400)

    if not request.session.get("voice_session_id"):
        request.session["voice_session_id"] = str(uuid.uuid4())
        request.session["turn_number"] = 0

    session_id = request.session["voice_session_id"]

    # Prefer cached values from session (set by /chat route) — saves a DB round-trip.
    # Falls back to DB only if session is missing the cache (e.g. user hit /greeting directly).
    t1 = time.perf_counter()
    topics_completed = request.session.get("cached_topics_completed")
    topic_order      = request.session.get("cached_topic_order")
    if topics_completed is None or topic_order is None:
        participant      = db_.get_participant_by_id(participant_id)
        topics_completed = participant.get("topics_completed", 0) or 0
        topic_order      = participant.get("assigned_topic_order", "ABC")
    t2 = time.perf_counter()

    current_topic = current_topic_for_participant(topic_order, topics_completed)
    current_ai    = ai_name_for_topic(topics_completed)

    opening = opening_message(current_ai, current_topic, is_first_topic=(topics_completed == 0))

    history = _history_cache.setdefault(session_id, [])

    # ── Resume mid-conversation if voice_turns already exist for this topic ─────
    # (happens when participant navigates back / refreshes / closes browser).
    existing_turns_payload: list[dict] = []
    existing = []
    try:
        existing = (
            db_.db().table("voice_turns")
            .select("turn_number, whisper_transcript, llm_response_text")
            .eq("participant_id", participant_id)
            .eq("topic_index", topics_completed + 1)
            .order("turn_number").execute().data or []
        )
    except Exception as exc:
        print(f"[greeting] existing-turn lookup failed: {exc}")

    if existing:
        # Re-hydrate LLM history (if in-memory cache was lost) AND build the payload
        # the frontend uses to repopulate the chat window.
        if not history:
            history.append({"role": "assistant", "content": opening})
            for vt in existing:
                if vt.get("whisper_transcript"):
                    history.append({"role": "user", "content": vt["whisper_transcript"]})
                if vt.get("llm_response_text"):
                    history.append({"role": "assistant", "content": vt["llm_response_text"]})
        for vt in existing:
            if vt.get("whisper_transcript"):
                existing_turns_payload.append({
                    "role": "user", "text": vt["whisper_transcript"], "turn": vt["turn_number"],
                })
            if vt.get("llm_response_text"):
                existing_turns_payload.append({
                    "role": "ai", "text": vt["llm_response_text"], "turn": vt["turn_number"],
                })
        # Restore turn counter so /transcribe & /turn continue from the right number
        last_turn = existing[-1]["turn_number"]
        request.session["turn_number"] = last_turn
    else:
        # Fresh start for this topic
        if not history:
            history.append({"role": "assistant", "content": opening})

    tts_voice = request.session.get("tts_voice", "nova")

    t3 = time.perf_counter()
    # Only generate (and play) TTS if this is a fresh conversation. On resume the
    # frontend skips the greeting audio entirely.
    tts_b64 = "" if existing else _get_greeting_tts(current_ai, tts_voice, topics_completed == 0, opening)
    t4 = time.perf_counter()

    print(f"[greeting] DB={t2-t1:.2f}s  TTS={t4-t3:.2f}s  total={t4-t0:.2f}s  "
          f"ai={current_ai} voice={tts_voice} resume={bool(existing)} cached={(t4-t3)<0.5}")
    return JSONResponse({
        "ok": True,
        "opening_text":        opening,
        "tts_b64":             tts_b64,
        "ai_name":             current_ai,
        "topic_index":         topics_completed + 1,
        "existing_turns":      existing_turns_payload,
        "current_turn_number": existing[-1]["turn_number"] if existing else 0,
    })


@router.post("/transcribe")
async def transcribe_turn(request: Request, audio: UploadFile = File(...)):
    participant_id = request.session.get("participant_id")
    if not participant_id:
        return JSONResponse({"error": "No session"}, status_code=400)

    if not request.session.get("voice_session_id"):
        request.session["voice_session_id"] = str(uuid.uuid4())
        request.session["turn_number"] = 0

    session_id   = request.session["voice_session_id"]
    turn_number  = request.session.get("turn_number", 0) + 1
    audio_bytes  = await audio.read()

    # Determine the current topic so we can shard the bucket by topic.
    # Prefer values cached in session (set by /chat route); fall back to DB only if missing.
    topics_completed = request.session.get("cached_topics_completed")
    topic_order      = request.session.get("cached_topic_order")
    if topics_completed is None or topic_order is None:
        participant = db_.get_participant_by_id(participant_id)
        topics_completed = (participant or {}).get("topics_completed", 0) or 0
        topic_order      = (participant or {}).get("assigned_topic_order", "ABC")
    current_topic = current_topic_for_participant(topic_order, topics_completed)

    # Atomically reserve this take's attempt number — same turn re-recorded
    # concurrently would still get distinct, ordered numbers.
    attempt_number = db_.next_voice_attempt_number(session_id, turn_number)

    audio_url  = db_.upload_audio(
        participant_id, session_id, turn_number, attempt_number, audio_bytes, topic=current_topic,
    )
    transcript = transcribe_audio(audio_bytes)

    # Persist this attempt immediately, so the take survives even if the participant
    # abandons it and records another (durable — not dependent on in-memory cache).
    db_.save_voice_turn_attempt({
        "participant_id":         participant_id,
        "session_id":             session_id,
        "turn_number":            turn_number,
        "attempt_number":         attempt_number,
        "whisper_transcript_raw": transcript,
        "audio_file_url":         audio_url,
    })

    # Always queue — even if Whisper returned empty, let the user edit/type
    # in the preview textarea before submitting.
    # `raw_whisper` is the original Whisper output, frozen. `transcript` is what we
    # pre-fill the preview box with — the user may edit it before submitting.
    # Appended, not overwritten — a re-record adds a new attempt instead of
    # discarding the previous one's history.
    attempts = _pending_cache.setdefault(session_id, [])
    attempts.append({
        "raw_whisper":    transcript,
        "transcript":     transcript,
        "audio_url":      audio_url,
        "turn_number":    turn_number,
        "attempt_number": attempt_number,
    })
    return JSONResponse({"ok": True, "transcript": transcript})


@router.post("/turn")
async def process_turn(request: Request):
    participant_id = request.session.get("participant_id")
    if not participant_id:
        return JSONResponse({"error": "No session"}, status_code=400)

    session_id = request.session.get("voice_session_id", "")
    attempts   = _pending_cache.pop(session_id, [])

    # The participant may have re-recorded this turn multiple times; only the LAST
    # attempt is what gets submitted. Earlier attempts stay in voice_turn_attempts
    # (written at /transcribe time) for traceability.
    pending = attempts[-1] if attempts else {}

    # The participant may have edited the Whisper output in the preview box.
    # We save BOTH: the raw Whisper text (for data integrity / cheat detection)
    # AND the submitted text (what the LLM actually saw).
    form = await request.form()
    raw_whisper       = pending.get("raw_whisper", pending.get("transcript", "")).strip()
    edited_transcript = str(form.get("transcript", "")).strip()
    submitted         = edited_transcript or raw_whisper

    # Behavioral timestamps from the client (ISO 8601 strings; any may be missing).
    # Stored raw — durations are derived at analysis time.
    timings = {
        "ai_audio_ended_at": form.get("ai_audio_ended_at") or None,
        "record_started_at": form.get("record_started_at") or None,
        "record_ended_at":   form.get("record_ended_at")   or None,
        "preview_shown_at":  form.get("preview_shown_at")  or None,
        "submitted_at":      form.get("submitted_at")      or None,
    }

    audio_url      = pending.get("audio_url", "")
    turn_number    = pending.get("turn_number", request.session.get("turn_number", 0) + 1)
    # Attempt numbers are sequential from 1, so the submitted attempt's number
    # equals the total number of takes recorded for this turn.
    total_attempts = pending.get("attempt_number", 1)

    if not submitted:
        return JSONResponse({"error": "No pending transcript"}, status_code=400)

    participant   = db_.get_participant_by_id(participant_id)
    platform      = participant.get("assigned_platform", "gpt-4o")
    topic_order   = participant.get("assigned_topic_order", "ABC")
    hsp_condition = participant.get("hsp_condition", "")
    topics_completed = participant.get("topics_completed", 0) or 0

    current_topic = current_topic_for_participant(topic_order, topics_completed)
    current_ai    = ai_name_for_topic(topics_completed)

    history = _history_cache.setdefault(session_id, [])
    history.append({"role": "user", "content": submitted})

    system_prompt = build_system_prompt(current_ai, current_topic, turn_number)
    ai_text, response_time_ms = call_llm(platform, history, system_prompt)
    history.append({"role": "assistant", "content": ai_text})

    request.session["turn_number"] = turn_number
    phase    = turn_phase(turn_number)
    is_final = turn_number >= MAX_TURNS   # = end of THIS topic's chat

    tts_voice = request.session.get("tts_voice", "nova")
    tts_b64   = text_to_speech(ai_text, tts_voice)

    db_.save_voice_turn({
        "participant_id":         participant_id,
        "session_id":             session_id,
        "turn_number":            turn_number,
        "whisper_transcript":     submitted,     # what the LLM actually saw
        "whisper_transcript_raw": raw_whisper,   # original Whisper output (frozen)
        "llm_response_text":      ai_text,
        "audio_file_url":         audio_url,
        "total_attempts":         total_attempts,  # takes recorded for this turn (1 = no re-record)
        "tts_voice_used":         tts_voice,
        "platform":               platform,
        "hsp_condition":          hsp_condition,
        "topic":                  current_topic,
        "topic_index":            topics_completed + 1,
        "ai_name":                current_ai,
        "response_time_ms":       response_time_ms,
        # Raw behavioral timestamps (analysis computes hesitation / speaking / editing)
        "ai_audio_ended_at":      timings["ai_audio_ended_at"],
        "record_started_at":      timings["record_started_at"],
        "record_ended_at":        timings["record_ended_at"],
        "preview_shown_at":       timings["preview_shown_at"],
        "submitted_at":           timings["submitted_at"],
    })

    if is_final:
        # Mark this topic's chat as done; participant now needs to take the post-survey
        db_.update_participant(participant_id, {"awaiting_survey": True})

    return JSONResponse({
        "ok":             True,
        "ai_text":        ai_text,
        "tts_b64":        tts_b64,
        "turn_number":    turn_number,
        "is_final":       is_final,
        "current_topic":  current_topic,
        "phase":          phase,
        "redirect":       "/post-survey" if is_final else None,
    })


# ── Post-survey ───────────────────────────────────────────────────────────────

@router.post("/post-survey")
async def submit_post_survey(request: Request, background_tasks: BackgroundTasks):
    participant = _require_participant(request)
    topics_completed = participant.get("topics_completed", 0) or 0

    # All 3 topics + surveys done → straight to completion
    if topics_completed >= 3:
        return RedirectResponse(url="/complete", status_code=302)
    # Must have finished the current topic's chat
    if not participant.get("awaiting_survey"):
        return RedirectResponse(url="/chat", status_code=302)

    form = await request.form()

    def fi(key: str) -> int:
        return int(form.get(key, 0))

    general_empathy      = fi("general_empathy")
    satisfaction         = fi("satisfaction")
    trust                = fi("trust")
    conversation_quality = fi("conversation_quality")
    affective_empathy_1  = fi("affective_empathy_1")
    affective_empathy_2  = fi("affective_empathy_2")
    cognitive_empathy    = fi("cognitive_empathy")
    associative_empathy      = fi("associative_empathy")
    emotional_responsiveness = fi("emotional_responsiveness")
    empathic_accuracy        = fi("empathic_accuracy")
    implicit_understanding   = fi("implicit_understanding")
    closeness_ios            = fi("closeness_ios")
    emotional_relief         = fi("emotional_relief")
    perceived_sycophancy     = fi("perceived_sycophancy")
    mbti_guess               = str(form.get("mbti_guess", "")).strip().upper()
    if mbti_guess == "UNKNOWN":
        mbti_guess = ""
    data_sharing_consent     = str(form.get("data_sharing_consent", "")) == "yes"

    # Validate 1-7 fields
    for val in (general_empathy, satisfaction, trust, conversation_quality,
                affective_empathy_1, affective_empathy_2, cognitive_empathy,
                associative_empathy, emotional_responsiveness, empathic_accuracy,
                implicit_understanding, emotional_relief, perceived_sycophancy):
        if not 1 <= val <= 7:
            return RedirectResponse(url="/post-survey?error=invalid", status_code=302)

    # Validate closeness_ios (1-7)
    if not 1 <= closeness_ios <= 7:
        return RedirectResponse(url="/post-survey?error=invalid", status_code=302)

    current_ai = ai_name_for_topic(topics_completed)

    db_.save_post_survey(participant["id"], {
        "topic_index":          topics_completed + 1,
        "ai_name":              current_ai,
        "general_empathy":      general_empathy,
        "satisfaction":         satisfaction,
        "trust":                trust,
        "conversation_quality": conversation_quality,
        "affective_empathy_1":  affective_empathy_1,
        "affective_empathy_2":  affective_empathy_2,
        "cognitive_empathy":    cognitive_empathy,
        "associative_empathy":      associative_empathy,
        "emotional_responsiveness": emotional_responsiveness,
        "empathic_accuracy":        empathic_accuracy,
        "implicit_understanding":   implicit_understanding,
        "closeness_ios":            closeness_ios,
        "emotional_relief":         emotional_relief,
        "perceived_sycophancy":     perceived_sycophancy,
        "mbti_guess":               mbti_guess or None,
    })

    # Bump topic counter, clear awaiting flag, capture consent on the LAST survey
    updates = {
        "topics_completed": topics_completed + 1,
        "awaiting_survey":  False,
    }
    if topics_completed + 1 >= 3:
        updates["data_sharing_consent"] = data_sharing_consent
    db_.update_participant(participant["id"], updates)

    # Reset session state so next /chat starts a fresh voice session
    request.session["voice_session_id"] = None
    request.session["turn_number"] = 0

    # More topics to do?
    if topics_completed + 1 < 3:
        return RedirectResponse(url="/chat", status_code=302)

    # Final topic done → finalize and go to completion page
    code = db_.finalize_participant(participant["id"])
    request.session["completion_code"] = code
    background_tasks.add_task(run_hsp_prediction, participant["id"])
    background_tasks.add_task(run_mbti_prediction, participant["id"])
    return RedirectResponse(url="/complete", status_code=302)
