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
        "index.html",
        {"request": request, "prolific_id": effective_id},
    )


@router.get("/survey", response_class=HTMLResponse)
def survey(request: Request):
    from app.config import HSPS_ITEMS, BFI_ITEMS, HSPS_LABELS, BFI_LABELS

    participant = _get_participant(request)
    if not participant:
        return _redirect("/")

    return templates.TemplateResponse(
        "survey.html",
        {
            "request": request,
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
    return templates.TemplateResponse("screened_out.html", {"request": request, "reason": reason})


@router.get("/intro", response_class=HTMLResponse)
def intro(request: Request):
    participant = _get_participant(request)
    if not participant:
        return _redirect("/")
    if not participant.get("survey_completed"):
        return _redirect("/survey")
    if participant.get("intro_completed"):
        return _redirect("/chat")

    from app.config import TOPIC_PROMPTS, TOPIC_DISPLAY
    topic = participant.get("assigned_topic", "")

    return templates.TemplateResponse(
        "intro.html",
        {
            "request": request,
            "topic": TOPIC_DISPLAY.get(topic, topic),
            "topic_prompt": TOPIC_PROMPTS.get(topic, ""),
        },
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
    if participant.get("chat_completed"):
        return _redirect("/post-survey")

    from app.config import TOPIC_PROMPTS, TOPIC_DISPLAY, CONVERSATION_ROUNDS
    topic = participant.get("assigned_topic", "")
    history = db_.get_conversation(participant["id"])
    # Exclude intro (round 0) from display count
    rounds_done = sum(1 for r in history if r["round_number"] > 0)

    return templates.TemplateResponse(
        "chat.html",
        {
            "request": request,
            "topic": TOPIC_DISPLAY.get(topic, topic),
            "topic_prompt": TOPIC_PROMPTS.get(topic, ""),
            "history": [r for r in history if r["round_number"] > 0],
            "rounds_done": rounds_done,
            "total_rounds": CONVERSATION_ROUNDS,
        },
    )


@router.get("/post-survey", response_class=HTMLResponse)
def post_survey(request: Request):
    participant = _get_participant(request)
    if not participant:
        return _redirect("/")
    if not participant.get("chat_completed"):
        return _redirect("/chat")
    if participant.get("post_survey_completed"):
        return _redirect("/complete")

    from app.config import POST_SURVEY_LABELS, AI_NAME

    return templates.TemplateResponse(
        "post_survey.html",
        {"request": request, "labels": POST_SURVEY_LABELS, "ai_name": AI_NAME},
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
        "complete.html",
        {
            "request":       request,
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
    if not participant.get("chat_completed"):
        return "/chat"
    if not participant.get("post_survey_completed"):
        return "/post-survey"
    return "/complete"
