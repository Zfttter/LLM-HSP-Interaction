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
from app.config import CONVERSATION_ROUNDS, OPENING_MESSAGE, build_system_prompt, MAX_TURNS

_history_cache: dict = {}
_pending_cache: dict = {}

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
    self_mbti = str(form.get("self_mbti", "")).strip().upper() or None

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
    excluded, reason = check_exclusion(age, native_english, ai_usage)

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
    participant_id = request.session.get("participant_id")
    if not participant_id:
        return JSONResponse({"error": "No session"}, status_code=400)

    if not request.session.get("voice_session_id"):
        request.session["voice_session_id"] = str(uuid.uuid4())
        request.session["turn_number"] = 0

    session_id = request.session["voice_session_id"]
    history = _history_cache.setdefault(session_id, [])
    if not history:
        history.append({"role": "assistant", "content": OPENING_MESSAGE})

    tts_voice = request.session.get("tts_voice", "nova")
    tts_b64 = text_to_speech(OPENING_MESSAGE, tts_voice)
    return JSONResponse({"ok": True, "opening_text": OPENING_MESSAGE, "tts_b64": tts_b64})


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

    audio_url  = db_.upload_audio(participant_id, session_id, turn_number, audio_bytes)
    transcript = transcribe_audio(audio_bytes)

    _pending_cache[session_id] = {
        "transcript":  transcript,
        "audio_url":   audio_url,
        "turn_number": turn_number,
    }
    return JSONResponse({"ok": True, "transcript": transcript})


@router.post("/turn")
async def process_turn(request: Request):
    participant_id = request.session.get("participant_id")
    if not participant_id:
        return JSONResponse({"error": "No session"}, status_code=400)

    session_id = request.session.get("voice_session_id", "")
    pending    = _pending_cache.pop(session_id, {})
    transcript = pending.get("transcript", "").strip()
    audio_url  = pending.get("audio_url", "")
    turn_number = pending.get("turn_number", request.session.get("turn_number", 0) + 1)

    if not transcript:
        return JSONResponse({"error": "No pending transcript"}, status_code=400)

    participant   = db_.get_participant_by_id(participant_id)
    platform      = participant.get("assigned_platform", "gpt-4o")
    topic         = participant.get("assigned_topic", "")
    hsp_condition = participant.get("hsp_condition", "")

    history = _history_cache.setdefault(session_id, [])
    history.append({"role": "user", "content": transcript})

    system_prompt = build_system_prompt(topic, turn_number)
    ai_text, response_time_ms = call_llm(platform, history, system_prompt)
    history.append({"role": "assistant", "content": ai_text})

    request.session["turn_number"] = turn_number
    is_final = turn_number >= MAX_TURNS

    tts_voice = request.session.get("tts_voice", "nova")
    tts_b64 = text_to_speech(ai_text, tts_voice)

    db_.save_voice_turn({
        "participant_id":     participant_id,
        "session_id":         session_id,
        "turn_number":        turn_number,
        "whisper_transcript": transcript,
        "llm_response_text":  ai_text,
        "audio_file_url":     audio_url,
        "tts_voice_used":     tts_voice,
        "platform":           platform,
        "hsp_condition":      hsp_condition,
        "topic":              topic,
        "response_time_ms":   response_time_ms,
    })

    if is_final:
        db_.update_participant(participant_id, {"chat_completed": True})

    return JSONResponse({
        "ok":          True,
        "ai_text":     ai_text,
        "tts_b64":     tts_b64,
        "turn_number": turn_number,
        "is_final":    is_final,
    })


# ── Post-survey ───────────────────────────────────────────────────────────────

@router.post("/post-survey")
async def submit_post_survey(request: Request, background_tasks: BackgroundTasks):
    participant = _require_participant(request)
    if participant.get("post_survey_completed"):
        return RedirectResponse(url="/complete", status_code=302)

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
    mbti_guess               = str(form.get("mbti_guess", "")).strip()

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

    db_.save_post_survey(participant["id"], {
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

    code = db_.finalize_participant(participant["id"])
    request.session["completion_code"] = code

    # Silently run predictions after the participant sees their completion page
    background_tasks.add_task(run_hsp_prediction, participant["id"])
    background_tasks.add_task(run_mbti_prediction, participant["id"])

    return RedirectResponse(url="/complete", status_code=302)
