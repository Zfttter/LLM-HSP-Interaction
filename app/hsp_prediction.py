"""
HSP prediction task — runs silently after a participant completes the study.

Fetches the participant's conversation transcript and asks the assigned LLM
to rate them on all 18 HSPS items (rewritten in third person, same 1–7 scale).
The result is stored in the participants table for later comparison with
the participant's own self-report scores.

All errors are caught and logged; the participant's completion flow is never affected.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

import app.database as db_
from app.llm import call_llm

logger = logging.getLogger(__name__)


# ── System prompt ─────────────────────────────────────────────────────────────

PREDICTION_SYSTEM_PROMPT = """\
You are a research assistant. You will be given a conversation between two \
speakers: a human participant (labeled "Participant") and an AI assistant \
(labeled "AI"). The participant shared a personal emotional experience during \
the conversation.

Your task is to rate the PARTICIPANT — not the AI — on 18 personality items. \
Base your ratings on what the Participant revealed about themselves through \
their words, reactions, sensitivities, and thought patterns. Do not rate the AI.

Use this scale: 1 = Not at all  |  7 = Extremely

If there is little direct evidence for an item, give your best estimate based \
on the overall impression the Participant gives.

Respond ONLY with a valid JSON object in this exact format, nothing else:
{"hsps_1": <1-7>, "hsps_2": <1-7>, "hsps_3": <1-7>, "hsps_4": <1-7>, \
"hsps_5": <1-7>, "hsps_6": <1-7>, "hsps_7": <1-7>, "hsps_8": <1-7>, \
"hsps_9": <1-7>, "hsps_10": <1-7>, "hsps_11": <1-7>, "hsps_12": <1-7>, \
"hsps_13": <1-7>, "hsps_14": <1-7>, "hsps_15": <1-7>, "hsps_16": <1-7>, \
"hsps_17": <1-7>, "hsps_18": <1-7>}"""


# ── HSPS items rewritten in third person (same order as config.HSPS_ITEMS) ───

HSPS_ITEMS_THIRD_PERSON = [
    "Is easily overwhelmed by strong sensory input",
    "Seems to be aware of subtleties in their environment",
    "Other people's moods affect them",
    "Tends to be more sensitive to pain",
    "Finds themselves needing to withdraw during busy days, into a darkened room or a place where they can have some privacy and relief from stimulation",
    "Is particularly sensitive to the effects of caffeine",
    "Is easily overwhelmed by things like bright lights, strong smells, coarse fabrics, or sirens close by",
    "Has a rich, complex inner life",
    "Is made uncomfortable by loud noises",
    "Is deeply moved by the arts or music",
    "Their nervous system sometimes feels so frazzled that they just need to go off by themselves",
    "Is conscientious",
    "Startles easily",
    "Gets rattled when they have a lot to do in a short amount of time",
    "Tends to notice when others are uncomfortable in a physical environment and knows how to make it more comfortable (e.g., changing the lighting or the seating)",
    "Gets annoyed when people try to get them to do too many things at once",
    "Tries hard to avoid making mistakes or forgetting things",
    "Makes a point to avoid violent movies or TV shows",
]


# ── Message builders ──────────────────────────────────────────────────────────

def _format_conversation(rounds: list[dict]) -> str:
    """Format all conversation rounds into a readable transcript."""
    lines = []
    for row in sorted(rounds, key=lambda r: r["round_number"]):
        label = "Introduction" if row["round_number"] == 0 else f"Round {row['round_number']}"
        lines.append(f"[{label} — Participant]: {row['user_message']}")
        lines.append(f"[{label} — AI]: {row['ai_response']}")
    return "\n\n".join(lines)


def _build_user_message(conversation_text: str) -> str:
    item_lines = "\n".join(
        f"{i}. {label}"
        for i, label in enumerate(HSPS_ITEMS_THIRD_PERSON, start=1)
    )
    return (
        f"Here is the conversation:\n\n"
        f"{conversation_text}\n\n"
        f"---\n\n"
        f"Based on this conversation, rate the PARTICIPANT (not the AI) "
        f"on each item (1 = Not at all, 7 = Extremely):\n\n"
        f"{item_lines}\n\n"
        f"Return ONLY the JSON object."
    )


# ── JSON parser ───────────────────────────────────────────────────────────────

def _parse_scores(raw: str) -> dict:
    """
    Extract and validate 18 integer scores (hsps_1 … hsps_18) from the
    LLM response. Handles optional markdown code fences.
    """
    text = raw.strip()
    if text.startswith("```"):
        inner = text.split("```")[1]
        if inner.startswith("json"):
            inner = inner[4:]
        text = inner.strip()

    parsed = json.loads(text)

    scores = {}
    for i in range(1, 19):
        key = f"hsps_{i}"
        val = int(parsed[key])
        if not 1 <= val <= 7:
            raise ValueError(f"{key}={val} is outside the valid range 1–7")
        scores[key] = val

    return scores


# ── Background task ───────────────────────────────────────────────────────────

async def run_hsp_prediction(participant_id: str) -> None:
    """
    Async background task. Fetches the conversation transcript, asks the
    assigned LLM to rate the participant on all 18 HSPS items, and stores
    the 18 scores alongside the mean in the participants table.

    Never raises — all exceptions are caught and logged so the participant's
    completion flow is never affected.
    """
    try:
        participant = await asyncio.to_thread(db_.get_participant_by_id, participant_id)
        if not participant:
            logger.error(f"[hsp_prediction] participant {participant_id} not found")
            return

        platform = participant.get("assigned_platform") or "gpt-4o"

        rounds = await asyncio.to_thread(db_.get_conversation, participant_id)
        if not rounds:
            logger.warning(
                f"[hsp_prediction] no conversation found for {participant_id}, skipping"
            )
            return

        conversation_text = _format_conversation(rounds)
        user_message = _build_user_message(conversation_text)
        messages = [{"role": "user", "content": user_message}]

        raw_response, _ = await asyncio.to_thread(
            call_llm,
            platform,
            messages,
            PREDICTION_SYSTEM_PROMPT,
            300,  # short JSON reply — 18 key-value pairs
        )

        scores = _parse_scores(raw_response)
        mean_score = round(sum(scores.values()) / len(scores), 4)

        await asyncio.to_thread(db_.save_hsp_prediction, participant_id, {
            "ai_hsps_responses":       scores,
            "ai_hsps_score":           mean_score,
            "ai_prediction_model":     platform,
            "ai_prediction_timestamp": datetime.now(timezone.utc).isoformat(),
        })

        logger.info(
            f"[hsp_prediction] {participant_id}: "
            f"ai_mean={mean_score:.2f}, human_mean={participant.get('hsps_score', '?')}, "
            f"model={platform}"
        )

    except Exception as exc:
        logger.error(f"[hsp_prediction] failed for {participant_id}: {exc}")
        try:
            await asyncio.to_thread(db_.save_hsp_prediction, participant_id, {
                "ai_hsps_responses":       None,
                "ai_hsps_score":           None,
                "ai_prediction_model":     None,
                "ai_prediction_timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as inner:
            logger.error(
                f"[hsp_prediction] also failed to write null result "
                f"for {participant_id}: {inner}"
            )
