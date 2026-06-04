"""
Page routes — each returns an HTML response via Jinja2 template.
State guards redirect participants who try to skip steps.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import app.database as db_

router = APIRouter()
templates = Jinja2Templates(directory="templates")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_participant(request: Request):
    pid = request.session.get("participant_id")
    if not pid:
        return None
    return db_.get_participant_by_id(pid)


def _redirect(path: str):
    return RedirectResponse(url=path, status_code=302)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def landing(request: Request, prolific_id: str = ""):
    # prolific_id in URL always wins — update session if different
    if prolific_id and prolific_id != request.session.get("prolific_id"):
        request.session.clear()
        request.session["prolific_id"] = prolific_id

    effective_id = request.session.get("prolific_id", "")

    # If this prolific_id already exists in the DB, restore session and resume
    if effective_id:
        existing = db_.get_participant_by_prolific(effective_id)
        if existing:
            request.session["participant_id"] = existing["id"]
            return _redirect(_next_step(existing))

    return templates.TemplateResponse(
        request,
        "index.html",
        {"prolific_id": effective_id},
    )


@router.get("/survey", response_class=HTMLResponse)
def survey(request: Request):
    from app.config import HSPS_ITEMS, BFI_ITEMS, HSPS_LABELS, BFI_LABELS

    participant = _get_participant(request)
    if not participant:
        return _redirect("/")

    return templates.TemplateResponse(
        request,
        "survey.html",
        {
            "hsps_items": HSPS_ITEMS,
            "bfi_items": BFI_ITEMS,
            "hsps_labels": HSPS_LABELS,
            "bfi_labels": BFI_LABELS,
        },
    )


@router.get("/screened-out", response_class=HTMLResponse)
def screened_out(request: Request):
    participant = _get_participant(request)
    reason = participant.get("exclusion_reason", "") if participant else ""
    return templates.TemplateResponse(request, "screened_out.html", {"reason": reason})


@router.get("/intro", response_class=HTMLResponse)
def intro(request: Request):
    participant = _get_participant(request)
    if not participant:
        return _redirect("/")
    if not participant.get("survey_completed"):
        return _redirect("/survey")
    if participant.get("intro_completed"):
        return _redirect("/chat")

    from app.config import TOPIC_PROMPTS, TOPIC_DISPLAY, TOPIC_ORDERS
    topic_order = participant.get("assigned_topic_order", "ABC")
    topics      = TOPIC_ORDERS.get(topic_order, TOPIC_ORDERS["ABC"])
    ordered_topics = [
        {"key": t, "name": TOPIC_DISPLAY.get(t, t), "prompt": TOPIC_PROMPTS.get(t, "")}
        for t in topics
    ]

    return templates.TemplateResponse(
        request,
        "intro.html",
        {"ordered_topics": ordered_topics},
    )


@router.get("/chat", response_class=HTMLResponse)
def chat(request: Request):
    participant = _get_participant(request)
    if not participant:
        return _redirect("/")
    if not participant.get("survey_completed"):
        return _redirect("/survey")
    if not participant.get("intro_completed"):
        return _redirect("/intro")

    topics_completed = participant.get("topics_completed", 0) or 0
    # Currently between chat and post-survey for this topic → go to survey
    if participant.get("awaiting_survey"):
        return _redirect("/post-survey")
    # All 3 topics + surveys done → finish
    if topics_completed >= 3:
        return _redirect("/complete")

    from app.config import (
        TOPIC_PROMPTS, TOPIC_DISPLAY, TOPIC_ORDERS,
        PER_TOPIC_TURNS, NUM_TOPICS, AI_NAMES,
        current_topic_for_participant, ai_name_for_topic,
    )
    topic_order  = participant.get("assigned_topic_order", "ABC")
    current_key  = current_topic_for_participant(topic_order, topics_completed)
    current_ai   = ai_name_for_topic(topics_completed)

    # Only reset the voice session when moving to a NEW topic (not on plain refresh)
    expected_topic_idx = topics_completed + 1
    if request.session.get("topic_session_idx") != expected_topic_idx:
        request.session["voice_session_id"] = None
        request.session["turn_number"]      = 0
        request.session["topic_session_idx"] = expected_topic_idx

    return templates.TemplateResponse(
        request,
        "chat.html",
        {
            "topic_order":      topic_order,
            "topic_key":        current_key,
            "topic_name":       TOPIC_DISPLAY.get(current_key, current_key),
            "topic_prompt":     TOPIC_PROMPTS.get(current_key, ""),
            "topic_index":      topics_completed + 1,   # 1-based for display
            "num_topics":       NUM_TOPICS,
            "per_topic_turns":  PER_TOPIC_TURNS,
            "ai_name":          current_ai,
        },
    )


@router.get("/post-survey", response_class=HTMLResponse)
def post_survey(request: Request):
    participant = _get_participant(request)
    if not participant:
        return _redirect("/")

    topics_completed = participant.get("topics_completed", 0) or 0
    if topics_completed >= 3:
        return _redirect("/complete")
    # Must have finished the current topic's chat before surveying it
    if not participant.get("awaiting_survey"):
        return _redirect("/chat")

    from app.config import POST_SURVEY_LABELS, ai_name_for_topic
    current_ai = ai_name_for_topic(topics_completed)

    return templates.TemplateResponse(
        request,
        "post_survey.html",
        {
            "labels":       POST_SURVEY_LABELS,
            "ai_name":      current_ai,
            "topic_index":  topics_completed + 1,
            "num_topics":   3,
        },
    )


@router.get("/complete", response_class=HTMLResponse)
def complete(request: Request):
    participant = _get_participant(request)
    if not participant:
        return _redirect("/")

    code = participant.get("completion_code", "")

    # ── Build personal report data ──────────────────────────────────────────
    hsps_score = participant.get("hsps_score")
    bfi_raw    = participant.get("bfi_scores") or {}

    # HSPS
    if hsps_score:
        hsps_pct = round((hsps_score - 1) / 6 * 100)
        if hsps_score < 3.5:
            hsps_level, hsps_cls = "Low", "level-low"
            hsps_text = (
                "You tend to process sensory information at a standard level "
                "and generally feel comfortable in busy or stimulating environments."
            )
        elif hsps_score < 5.0:
            hsps_level, hsps_cls = "Moderate", "level-mid"
            hsps_text = (
                "You show a balanced level of sensory sensitivity, experiencing "
                "deeper emotional processing in some situations while remaining "
                "comfortable in most environments."
            )
        else:
            hsps_level, hsps_cls = "High", "level-high"
            hsps_text = (
                "You tend to process sensory and emotional information more deeply "
                "than average. You may notice subtleties others miss and be more "
                "strongly affected by intense stimulation or others' emotions."
            )
    else:
        hsps_score = hsps_pct = None
        hsps_level = hsps_cls = hsps_text = ""

    # BFI-10
    _bfi_meta = [
        ("Extraversion",      "extraversion",      "How outgoing and energetically engaged with the world you tend to be"),
        ("Agreeableness",     "agreeableness",     "How cooperative, trusting, and considerate of others you tend to be"),
        ("Conscientiousness", "conscientiousness", "How organised, dependable, and self-disciplined you tend to be"),
        ("Neuroticism",       "neuroticism",       "How prone to stress, worry, and emotional variability you tend to be"),
        ("Openness",          "openness",          "How curious, imaginative, and open to new experiences you tend to be"),
    ]
    bfi_display = []
    for label, key, desc in _bfi_meta:
        score = bfi_raw.get(key)
        if score is None:
            continue
        pct = round((score - 1) / 4 * 100)
        if score < 2.5:
            lvl, cls = "Low",      "level-low"
        elif score < 3.5:
            lvl, cls = "Moderate", "level-mid"
        else:
            lvl, cls = "High",     "level-high"
        bfi_display.append({"label": label, "score": score,
                             "pct": pct, "level": lvl, "cls": cls, "desc": desc})

    return templates.TemplateResponse(
        request,
        "complete.html",
        {
            "completion_code": code,
            "hsps_score":    hsps_score,
            "hsps_pct":      hsps_pct,
            "hsps_level":    hsps_level,
            "hsps_cls":      hsps_cls,
            "hsps_text":     hsps_text,
            "bfi_display":   bfi_display,
        },
    )


# ── Step resolver ─────────────────────────────────────────────────────────────

def _next_step(participant: dict) -> str:
    if participant.get("excluded"):
        return "/screened-out"
    if not participant.get("survey_completed"):
        return "/survey"
    if not participant.get("intro_completed"):
        return "/intro"
    if participant.get("awaiting_survey"):
        return "/post-survey"
    if (participant.get("topics_completed", 0) or 0) >= 3:
        return "/complete"
    return "/chat"
