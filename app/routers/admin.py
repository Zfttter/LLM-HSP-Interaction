"""
Admin dashboard — local-only, protected by ADMIN_KEY.
Access: GET /admin?key=<ADMIN_KEY>
"""
from typing import Optional
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
import app.database as db_

router = APIRouter()
templates = Jinja2Templates(directory="templates")

_FORBIDDEN = JSONResponse({"error": "Forbidden"}, status_code=403)


def _check_key(key: Optional[str]) -> bool:
    return key == settings.ADMIN_KEY


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, key: str = ""):
    if not _check_key(key):
        return HTMLResponse("<h1>403 Forbidden</h1>", status_code=403)
    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "admin_key": key},
    )


@router.get("/admin/api/participants")
def api_participants(request: Request):
    key = request.query_params.get("key") or request.headers.get("X-Admin-Key")
    if not _check_key(key):
        return _FORBIDDEN

    # Fetch all participants
    p_result = (
        db_.db()
        .table("participants")
        .select("*")
        .order("created_at", desc=True)
        .execute()
    )
    participants: list[dict] = p_result.data or []

    # Fetch survey_responses — only the fields we need
    sr_result = (
        db_.db()
        .table("survey_responses")
        .select("participant_id, mbti_guess, completed_at")
        .execute()
    )
    survey_map: dict[str, dict] = {}
    for row in sr_result.data or []:
        survey_map[row["participant_id"]] = row

    # Merge and compute derived fields
    output = []
    for p in participants:
        sr = survey_map.get(p["id"], {})

        # HSPS diff
        hsps_score = p.get("hsps_score")
        ai_hsps_score = p.get("ai_hsps_score")
        hsps_diff: Optional[float] = None
        if hsps_score is not None and ai_hsps_score is not None:
            hsps_diff = round(ai_hsps_score - hsps_score, 2)

        # Per-item HSPS diffs
        hsps_item_diffs: Optional[dict] = None
        hsps_responses = p.get("hsps_responses")
        ai_hsps_responses = p.get("ai_hsps_responses")
        if hsps_responses and ai_hsps_responses:
            hsps_item_diffs = {}
            for i in range(1, 19):
                key_name = f"hsps_{i}"
                self_val = hsps_responses.get(key_name)
                ai_val = ai_hsps_responses.get(key_name)
                if self_val is not None and ai_val is not None:
                    hsps_item_diffs[key_name] = round(ai_val - self_val, 1)

        record = {
            **p,
            "mbti_guess": sr.get("mbti_guess"),
            "survey_completed_at": sr.get("completed_at"),
            "hsps_diff": hsps_diff,
            "hsps_item_diffs": hsps_item_diffs,
        }
        output.append(record)

    return JSONResponse(output)


@router.get("/admin/participant/{participant_id}", response_class=HTMLResponse)
def admin_participant_page(request: Request, participant_id: str, key: str = ""):
    if not _check_key(key):
        return HTMLResponse("<h1>403 Forbidden</h1>", status_code=403)
    return templates.TemplateResponse(
        "admin_participant.html",
        {"request": request, "participant_id": participant_id, "admin_key": key},
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

    # Conversation
    conv = (
        db_.db().table("conversations").select("*")
        .eq("participant_id", participant_id)
        .order("round_number")
        .execute().data or []
    )

    # Post-survey
    sr = (
        db_.db().table("survey_responses").select("*")
        .eq("participant_id", participant_id)
        .execute().data or []
    )
    post_survey = sr[0] if sr else {}

    # All IDs ordered by created_at DESC for prev/next
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

    hs = p.get("hsps_score")
    ai_hs = p.get("ai_hsps_score")
    hsps_diff = round(ai_hs - hs, 2) if (hs is not None and ai_hs is not None) else None

    return JSONResponse({
        **p,
        "conversation":   conv,
        "post_survey":    post_survey,
        "hsps_diff":      hsps_diff,
        "hsps_item_diffs": item_diffs,
        "prev_id":        prev_id,
        "next_id":        next_id,
        "current_index":  all_ids.index(participant_id) + 1 if participant_id in all_ids else None,
        "total_count":    len(all_ids),
    })


@router.get("/admin/api/overview")
def api_overview(request: Request):
    key = request.query_params.get("key") or request.headers.get("X-Admin-Key")
    if not _check_key(key):
        return _FORBIDDEN

    # Fetch condition_counts
    cc_result = (
        db_.db()
        .table("condition_counts")
        .select("*")
        .order("condition_id")
        .execute()
    )
    condition_counts: list[dict] = cc_result.data or []

    # Fetch relevant participant fields (non-excluded only)
    p_result = (
        db_.db()
        .table("participants")
        .select("condition_id, post_survey_completed, hsps_score, ai_hsps_score, assigned_platform")
        .eq("excluded", False)
        .execute()
    )
    participants: list[dict] = p_result.data or []

    # Per-condition completed count
    completed_by_condition: dict[int, int] = {}
    hsps_high = 0
    hsps_low = 0
    platform_ai_scores: dict[str, list[float]] = {}
    total_completed = 0

    for p in participants:
        if p.get("post_survey_completed"):
            cid = p.get("condition_id")
            if cid is not None:
                completed_by_condition[cid] = completed_by_condition.get(cid, 0) + 1
            total_completed += 1

            # HSPS high/low (based on self-report)
            score = p.get("hsps_score")
            if score is not None:
                if score >= 4.0:
                    hsps_high += 1
                else:
                    hsps_low += 1

            # Platform AI HSPS average
            platform = p.get("assigned_platform")
            ai_score = p.get("ai_hsps_score")
            if platform and ai_score is not None:
                platform_ai_scores.setdefault(platform, []).append(ai_score)

    # Build conditions list
    conditions = []
    for cc in condition_counts:
        cid = cc.get("condition_id")
        conditions.append({
            "condition_id": cid,
            "platform": cc.get("platform"),
            "topic": cc.get("topic"),
            "assigned_count": cc.get("current_count", 0),
            "completed_count": completed_by_condition.get(cid, 0),
        })

    # Per-platform AI HSPS average
    platform_ai_avg: dict = {}
    for platform, scores in platform_ai_scores.items():
        platform_ai_avg[platform] = round(sum(scores) / len(scores), 2) if scores else None

    return JSONResponse({
        "conditions": conditions,
        "hsps_high": hsps_high,
        "hsps_low": hsps_low,
        "total_completed": total_completed,
        "platform_ai_avg": platform_ai_avg,
    })
