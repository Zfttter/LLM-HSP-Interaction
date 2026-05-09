"""
MBTI prediction task — runs silently after a participant completes the study.

Fetches the participant's conversation transcript and asks the assigned LLM
to infer the participant's MBTI personality type from their conversation style,
thought patterns, and emotional responses.

The result is stored in the participants table for later comparison with
the participant's own self-reported MBTI type.

All errors are caught and logged; the participant's completion flow is never affected.
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import app.database as db_
from app.llm import call_llm

logger = logging.getLogger(__name__)

VALID_MBTI_TYPES = {
    "ISTJ", "ISFJ", "INFJ", "INTJ",
    "ISTP", "ISFP", "INFP", "INTP",
    "ESTP", "ESFP", "ENFP", "ENTP",
    "ESTJ", "ESFJ", "ENFJ", "ENTJ",
}

# ── System prompt ─────────────────────────────────────────────────────────────

MBTI_PREDICTION_SYSTEM_PROMPT = """\
You are a research assistant. You will be given a conversation between two \
speakers: a human participant (labeled "Participant") and an AI assistant \
(labeled "AI"). The participant shared a personal emotional experience.

Your task is to infer the PARTICIPANT's most likely MBTI personality type \
based on how they communicate, structure their thoughts, describe emotions, \
respond to questions, and frame their experience. Focus exclusively on the \
Participant — do not assess the AI.

The 16 MBTI types are built from four dimensions:
- E (Extraversion) vs I (Introversion): energy source, social orientation
- S (Sensing) vs N (iNtuition): information processing, concrete vs abstract
- T (Thinking) vs F (Feeling): decision-making, logic vs values
- J (Judging) vs P (Perceiving): structure preference, planned vs flexible

Respond ONLY with a valid JSON object in this exact format, nothing else:
{"mbti_type": "XXXX", "rationale": "One or two sentences explaining the key \
behavioural signals that led to this assessment."}

Replace XXXX with one of the 16 valid types (e.g., INFJ, ENFP, ISTP, etc.)."""


# ── Message builders ──────────────────────────────────────────────────────────

def _format_conversation(rounds: list[dict]) -> str:
    lines = []
    for row in sorted(rounds, key=lambda r: r["round_number"]):
        label = "Introduction" if row["round_number"] == 0 else f"Round {row['round_number']}"
        lines.append(f"[{label} — Participant]: {row['user_message']}")
        lines.append(f"[{label} — AI]: {row['ai_response']}")
    return "\n\n".join(lines)


def _build_user_message(conversation_text: str) -> str:
    return (
        f"Here is the conversation:\n\n"
        f"{conversation_text}\n\n"
        f"---\n\n"
        f"Based on the Participant's messages, infer their MBTI personality type "
        f"and provide a brief rationale. Return ONLY the JSON object."
    )


# ── JSON parser ───────────────────────────────────────────────────────────────

def _parse_mbti(raw: str) -> dict:
    """
    Extract mbti_type and rationale from the LLM response.
    Handles optional markdown code fences.
    Validates that mbti_type is one of the 16 standard types.
    """
    text = raw.strip()
    if text.startswith("```"):
        inner = text.split("```")[1]
        if inner.startswith("json"):
            inner = inner[4:]
        text = inner.strip()

    parsed = json.loads(text)

    mbti_type = str(parsed.get("mbti_type", "")).strip().upper()
    # Accept with or without hyphens / spaces, normalise to 4 letters
    mbti_type = re.sub(r"[^A-Z]", "", mbti_type)[:4]
    if mbti_type not in VALID_MBTI_TYPES:
        raise ValueError(f"Invalid MBTI type: {mbti_type!r}")

    rationale = str(parsed.get("rationale", "")).strip()

    return {"mbti_type": mbti_type, "rationale": rationale}


# ── Background task ───────────────────────────────────────────────────────────

async def run_mbti_prediction(participant_id: str) -> None:
    """
    Async background task. Fetches the conversation transcript, asks the
    assigned LLM to infer the participant's MBTI type, and stores the result
    in the participants table.

    Never raises — all exceptions are caught and logged.
    """
    try:
        participant = await asyncio.to_thread(db_.get_participant_by_id, participant_id)
        if not participant:
            logger.error(f"[mbti_prediction] participant {participant_id} not found")
            return

        platform = participant.get("assigned_platform") or "gpt-4o"

        rounds = await asyncio.to_thread(db_.get_conversation, participant_id)
        if not rounds:
            logger.warning(
                f"[mbti_prediction] no conversation found for {participant_id}, skipping"
            )
            return

        conversation_text = _format_conversation(rounds)
        user_message = _build_user_message(conversation_text)
        messages = [{"role": "user", "content": user_message}]

        raw_response, _ = await asyncio.to_thread(
            call_llm,
            platform,
            messages,
            MBTI_PREDICTION_SYSTEM_PROMPT,
            200,  # short JSON reply
        )

        result = _parse_mbti(raw_response)

        await asyncio.to_thread(db_.save_mbti_prediction, participant_id, {
            "ai_mbti_type":      result["mbti_type"],
            "ai_mbti_rationale": result["rationale"],
            "ai_mbti_model":     platform,
            "ai_mbti_timestamp": datetime.now(timezone.utc).isoformat(),
        })

        self_mbti = participant.get("self_mbti") or "unknown"
        logger.info(
            f"[mbti_prediction] {participant_id}: "
            f"ai={result['mbti_type']}, self={self_mbti}, model={platform}"
        )

    except Exception as exc:
        logger.error(f"[mbti_prediction] failed for {participant_id}: {exc}")
        try:
            await asyncio.to_thread(db_.save_mbti_prediction, participant_id, {
                "ai_mbti_type":      None,
                "ai_mbti_rationale": None,
                "ai_mbti_model":     None,
                "ai_mbti_timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as inner:
            logger.error(
                f"[mbti_prediction] also failed to write null result "
                f"for {participant_id}: {inner}"
            )
