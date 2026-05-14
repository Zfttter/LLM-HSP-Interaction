"""
Admin dashboard — local-only, protected by ADMIN_KEY.
Access: GET /admin?key=<ADMIN_KEY>
"""
import csv
import io
from typing import Optional
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
import app.database as db_

router = APIRouter()
templates = Jinja2Templates(directory="templates")

_FORBIDDEN = JSONResponse({"error": "Forbidden"}, status_code=403)
_VOICE_BUCKET = "voice-recordings"
_VOICE_LABEL = {"nova": "Voice 1", "onyx": "Voice 2", "alloy": "Voice 3"}


def _check_key(key: Optional[str]) -> bool:
    return key == settings.ADMIN_KEY


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, key: str = ""):
    if not _check_key(key):
        return HTMLResponse("<h1>403 Forbidden</h1>", status_code=403)
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"admin_key": key},
    )


@router.get("/admin/api/participants")
def api_participants(request: Request):
    key = request.query_params.get("key") or request.headers.get("X-Admin-Key")
    if not _check_key(key):
        return _FORBIDDEN

    try:
        # Participants
        p_result = (
            db_.db()
            .table("participants")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )
        participants: list[dict] = p_result.data or []

        # Survey responses
        sr_result = (
            db_.db()
            .table("survey_responses")
            .select("participant_id, mbti_guess, completed_at")
            .execute()
        )
        survey_map: dict[str, dict] = {r["participant_id"]: r for r in (sr_result.data or [])}

        # Voice turns — get voice selected + turns completed per participant
        voice_map: dict[str, dict] = {}
        try:
            vt_result = (
                db_.db()
                .table("voice_turns")
                .select("participant_id, tts_voice_used, turn_number")
                .execute()
            )
            for row in vt_result.data or []:
                pid = row["participant_id"]
                tn  = row.get("turn_number", 0) or 0
                if pid not in voice_map:
                    voice_map[pid] = {"tts_voice": row.get("tts_voice_used"), "turns_completed": tn}
                else:
                    voice_map[pid]["turns_completed"] = max(voice_map[pid]["turns_completed"], tn)
        except Exception as vt_exc:
            print(f"[Admin] voice_turns query failed: {vt_exc}")

        # Merge
        output = []
        for p in participants:
            sr = survey_map.get(p["id"], {})
            vm = voice_map.get(p["id"], {})

            hsps_score    = p.get("hsps_score")
            ai_hsps_score = p.get("ai_hsps_score")
            hsps_diff: Optional[float] = None
            if hsps_score is not None and ai_hsps_score is not None:
                hsps_diff = round(ai_hsps_score - hsps_score, 2)

            hsps_item_diffs: Optional[dict] = None
            hsps_responses    = p.get("hsps_responses")
            ai_hsps_responses = p.get("ai_hsps_responses")
            if hsps_responses and ai_hsps_responses:
                hsps_item_diffs = {}
                for i in range(1, 19):
                    key_name = f"hsps_{i}"
                    sv = hsps_responses.get(key_name)
                    av = ai_hsps_responses.get(key_name)
                    if sv is not None and av is not None:
                        hsps_item_diffs[key_name] = round(av - sv, 1)

            output.append({
                **p,
                "mbti_guess":           sr.get("mbti_guess"),
                "survey_completed_at":  sr.get("completed_at"),
                "hsps_diff":            hsps_diff,
                "hsps_item_diffs":      hsps_item_diffs,
                "tts_voice":            vm.get("tts_voice"),
                "turns_completed":      vm.get("turns_completed", 0),
            })

        return JSONResponse(output)

    except Exception as exc:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/admin/participant/{participant_id}", response_class=HTMLResponse)
def admin_participant_page(request: Request, participant_id: str, key: str = ""):
    if not _check_key(key):
        return HTMLResponse("<h1>403 Forbidden</h1>", status_code=403)
    return templates.TemplateResponse(
        request,
        "admin_participant.html",
        {"participant_id": participant_id, "admin_key": key},
    )


@router.get("/admin/api/participant/{participant_id}")
def api_participant_detail(request: Request, participant_id: str):
    key = request.query_params.get("key") or request.headers.get("X-Admin-Key")
    if not _check_key(key):
        return _FORBIDDEN

    # Participant
    p_res = db_.db().table("participants").select("*").eq("id", participant_id).execute()
    if not p_res.data:
        return JSONResponse({"error": "Not found"}, status_code=404)
    p = p_res.data[0]

    # Voice turns (primary conversation data)
    vt_res = (
        db_.db().table("voice_turns").select("*")
        .eq("participant_id", participant_id)
        .order("turn_number")
        .execute()
    )
    voice_turns = vt_res.data or []

    # Post-survey
    sr = (
        db_.db().table("survey_responses").select("*")
        .eq("participant_id", participant_id)
        .execute().data or []
    )
    post_survey = sr[0] if sr else {}

    # Prev/next navigation
    all_ids = [
        r["id"] for r in (
            db_.db().table("participants").select("id")
            .order("created_at", desc=True).execute().data or []
        )
    ]
    prev_id = next_id = None
    if participant_id in all_ids:
        idx = all_ids.index(participant_id)
        prev_id = all_ids[idx - 1] if idx > 0 else None
        next_id = all_ids[idx + 1] if idx < len(all_ids) - 1 else None

    # HSPS diffs
    hsps_r    = p.get("hsps_responses") or {}
    ai_hsps_r = p.get("ai_hsps_responses") or {}
    item_diffs: dict = {}
    if hsps_r and ai_hsps_r:
        for i in range(1, 19):
            k = f"hsps_{i}"
            if k in hsps_r and k in ai_hsps_r:
                item_diffs[k] = round(ai_hsps_r[k] - hsps_r[k], 1)

    hs    = p.get("hsps_score")
    ai_hs = p.get("ai_hsps_score")
    hsps_diff = round(ai_hs - hs, 2) if (hs is not None and ai_hs is not None) else None

    # Voice selected (from first turn)
    tts_voice = voice_turns[0].get("tts_voice_used") if voice_turns else None

    return JSONResponse({
        **p,
        "voice_turns":      voice_turns,
        "post_survey":      post_survey,
        "hsps_diff":        hsps_diff,
        "hsps_item_diffs":  item_diffs,
        "prev_id":          prev_id,
        "next_id":          next_id,
        "current_index":    all_ids.index(participant_id) + 1 if participant_id in all_ids else None,
        "total_count":      len(all_ids),
        "tts_voice":        tts_voice,
        "turns_completed":  len(voice_turns),
    })


@router.get("/admin/api/overview")
def api_overview(request: Request):
    key = request.query_params.get("key") or request.headers.get("X-Admin-Key")
    if not _check_key(key):
        return _FORBIDDEN

    cc_result = (
        db_.db()
        .table("condition_counts")
        .select("*")
        .order("condition_id")
        .execute()
    )
    condition_counts: list[dict] = cc_result.data or []

    p_result = (
        db_.db()
        .table("participants")
        .select("condition_id, post_survey_completed, hsps_score, ai_hsps_score, assigned_platform")
        .eq("excluded", False)
        .execute()
    )
    participants: list[dict] = p_result.data or []

    # Voice distribution (one entry per participant, from their first turn)
    vt_result = (
        db_.db()
        .table("voice_turns")
        .select("participant_id, tts_voice_used, turn_number")
        .order("turn_number")
        .execute()
    )
    voice_dist: dict[str, int] = {}
    seen_voice: set = set()
    for row in vt_result.data or []:
        pid = row.get("participant_id")
        if pid and pid not in seen_voice:
            seen_voice.add(pid)
            v = row.get("tts_voice_used") or "unknown"
            voice_dist[v] = voice_dist.get(v, 0) + 1

    completed_by_condition: dict[int, int] = {}
    hsps_high = 0
    hsps_low  = 0
    platform_ai_scores: dict[str, list[float]] = {}
    total_completed = 0

    for p in participants:
        if p.get("post_survey_completed"):
            cid = p.get("condition_id")
            if cid is not None:
                completed_by_condition[cid] = completed_by_condition.get(cid, 0) + 1
            total_completed += 1

            score = p.get("hsps_score")
            if score is not None:
                if score >= 4.0:
                    hsps_high += 1
                else:
                    hsps_low += 1

            platform = p.get("assigned_platform")
            ai_score = p.get("ai_hsps_score")
            if platform and ai_score is not None:
                platform_ai_scores.setdefault(platform, []).append(ai_score)

    conditions = []
    for cc in condition_counts:
        cid = cc.get("condition_id")
        conditions.append({
            "condition_id":    cid,
            "platform":        cc.get("platform"),
            "topic":           cc.get("topic"),
            "assigned_count":  cc.get("current_count", 0),
            "completed_count": completed_by_condition.get(cid, 0),
        })

    platform_ai_avg: dict = {}
    for platform, scores in platform_ai_scores.items():
        platform_ai_avg[platform] = round(sum(scores) / len(scores), 2) if scores else None

    return JSONResponse({
        "conditions":       conditions,
        "hsps_high":        hsps_high,
        "hsps_low":         hsps_low,
        "total_completed":  total_completed,
        "platform_ai_avg":  platform_ai_avg,
        "voice_dist":       voice_dist,
    })


@router.get("/admin/api/signed-url")
def get_signed_url(request: Request, path: str = ""):
    """Generate a short-lived signed URL for a private audio file."""
    key = request.query_params.get("key") or request.headers.get("X-Admin-Key")
    if not _check_key(key):
        return _FORBIDDEN
    if not path:
        return JSONResponse({"error": "No path"}, status_code=400)
    try:
        result = db_.db().storage.from_(_VOICE_BUCKET).create_signed_url(path, 3600)
        url = result.get("signedURL") or result.get("signedUrl", "")
        return JSONResponse({"url": url})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/admin/api/export/voice-turns.csv")
def export_voice_turns(request: Request):
    """Export all voice turns joined with participant + post-survey data as CSV."""
    key = request.query_params.get("key") or request.headers.get("X-Admin-Key")
    if not _check_key(key):
        return _FORBIDDEN

    # Participants
    p_map = {
        p["id"]: p for p in (
            db_.db().table("participants").select(
                "id, prolific_id, assigned_platform, assigned_topic, "
                "hsps_score, ai_hsps_score, age, gender, country, "
                "native_english, ai_usage_frequency, race, self_mbti, "
                "ai_mbti_type, excluded"
            ).execute().data or []
        )
    }

    # Post-surveys
    sr_map = {
        r["participant_id"]: r for r in (
            db_.db().table("survey_responses").select(
                "participant_id, general_empathy, satisfaction, trust, "
                "conversation_quality, affective_empathy_1, affective_empathy_2, "
                "cognitive_empathy, associative_empathy, emotional_responsiveness, "
                "empathic_accuracy, implicit_understanding, closeness_ios, "
                "emotional_relief, perceived_sycophancy, mbti_guess"
            ).execute().data or []
        )
    }

    # Voice turns
    voice_turns = (
        db_.db().table("voice_turns").select("*")
        .order("participant_id").order("turn_number")
        .execute().data or []
    )

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        # Participant
        "participant_id", "prolific_id", "platform", "topic",
        "age", "gender", "country", "native_english", "ai_usage_frequency",
        "race", "self_mbti", "ai_mbti_type", "excluded",
        "hsps_score", "ai_hsps_score",
        # Turn
        "turn_number", "tts_voice_used",
        "whisper_transcript", "transcript_word_count",
        "llm_response_text", "response_time_ms", "audio_file_path",
        # Post-survey
        "general_empathy", "satisfaction", "trust", "conversation_quality",
        "affective_empathy_1", "affective_empathy_2", "cognitive_empathy",
        "associative_empathy", "emotional_responsiveness", "empathic_accuracy",
        "implicit_understanding", "closeness_ios", "emotional_relief",
        "perceived_sycophancy", "mbti_guess",
    ])

    for vt in voice_turns:
        pid        = vt.get("participant_id", "")
        p          = p_map.get(pid, {})
        sr         = sr_map.get(pid, {})
        transcript = vt.get("whisper_transcript") or ""
        words      = len(transcript.split()) if transcript.strip() else 0

        writer.writerow([
            pid,
            p.get("prolific_id", ""),
            p.get("assigned_platform", ""),
            p.get("assigned_topic", ""),
            p.get("age", ""),
            p.get("gender", ""),
            p.get("country", ""),
            p.get("native_english", ""),
            p.get("ai_usage_frequency", ""),
            p.get("race", ""),
            p.get("self_mbti", ""),
            p.get("ai_mbti_type", ""),
            p.get("excluded", ""),
            p.get("hsps_score", ""),
            p.get("ai_hsps_score", ""),
            vt.get("turn_number", ""),
            vt.get("tts_voice_used", ""),
            transcript,
            words,
            vt.get("llm_response_text") or "",
            vt.get("response_time_ms", ""),
            vt.get("audio_file_url") or "",
            sr.get("general_empathy", ""),
            sr.get("satisfaction", ""),
            sr.get("trust", ""),
            sr.get("conversation_quality", ""),
            sr.get("affective_empathy_1", ""),
            sr.get("affective_empathy_2", ""),
            sr.get("cognitive_empathy", ""),
            sr.get("associative_empathy", ""),
            sr.get("emotional_responsiveness", ""),
            sr.get("empathic_accuracy", ""),
            sr.get("implicit_understanding", ""),
            sr.get("closeness_ios", ""),
            sr.get("emotional_relief", ""),
            sr.get("perceived_sycophancy", ""),
            sr.get("mbti_guess", ""),
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=voice_turns.csv"},
    )
