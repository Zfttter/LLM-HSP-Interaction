"""
Exploratory feature extraction for the admin "Research" dashboard.

Maps raw transcripts/timings/survey answers onto the RQ1/RQ2/RQ3 measures
described in the project's research design, so the admin page can show
scatter/bar charts instead of a flat table of numbers.

IMPORTANT — this is NOT the licensed LIWC/eGeMAPS toolchain the analysis
plan calls for. The word lists below are small, hand-built stand-ins meant
to give an early, exploratory read while N is tiny (single digits). Treat
every number here as descriptive, not confirmatory — do not report these
as the paper's validated measures.
"""
import math
import re
from collections import Counter
from typing import Optional

import app.database as db_
import app.llm as llm_

# ── Word lists (hand-built approximations, not LIWC) ──────────────────────────

FIRST_PERSON_SINGULAR = {"i", "me", "my", "mine", "myself"}

NEGEMO_WORDS = {
    "sad", "sadness", "angry", "anger", "afraid", "fear", "scared", "worried",
    "worry", "anxious", "anxiety", "upset", "hurt", "pain", "painful", "awful",
    "terrible", "horrible", "bad", "worse", "worst", "hate", "hated", "hopeless",
    "lonely", "alone", "ashamed", "guilt", "guilty", "regret", "frustrated",
    "frustrating", "annoyed", "annoying", "stress", "stressed", "stressful",
    "nervous", "embarrassed", "embarrassing", "disappointed", "miserable",
    "cry", "crying", "cried", "panic", "panicked", "dread", "grief", "devastated",
}

TENTATIVE_WORDS = {
    "maybe", "perhaps", "possibly", "probably", "guess", "guessing", "seems",
    "seem", "seemed", "might", "could", "sort", "kind", "somewhat", "suppose",
    "supposed", "unsure", "think", "thought", "wonder", "wondering", "unclear",
    "somehow", "apparently",
}

FUNCTION_WORDS = {
    # pronouns
    "i", "me", "my", "mine", "myself", "you", "your", "yours", "yourself",
    "he", "him", "his", "she", "her", "hers", "it", "its", "we", "us", "our",
    "ours", "they", "them", "their", "theirs", "this", "that", "these", "those",
    # prepositions
    "in", "on", "at", "by", "for", "with", "about", "against", "between",
    "into", "through", "during", "before", "after", "above", "below", "to",
    "from", "up", "down", "of", "off", "over", "under", "as",
    # conjunctions
    "and", "but", "or", "nor", "so", "yet", "because", "although", "though",
    "while", "if", "unless", "since", "whereas",
    # auxiliary/modal verbs
    "am", "is", "are", "was", "were", "be", "been", "being", "have", "has",
    "had", "do", "does", "did", "will", "would", "shall", "should", "can",
    "could", "may", "might", "must",
    # negations + common adverbs
    "not", "no", "never", "very", "really", "just", "only", "also", "too",
}

_WORD_RE = re.compile(r"[a-zA-Z']+")


def _tokenize(text: Optional[str]) -> list[str]:
    if not text:
        return []
    return _WORD_RE.findall(text.lower())


def word_ratio(text: Optional[str], wordset: set[str]) -> Optional[float]:
    """Fraction of tokens in `text` that fall in `wordset`. None if no tokens."""
    tokens = _tokenize(text)
    if not tokens:
        return None
    return sum(1 for t in tokens if t in wordset) / len(tokens)


def _function_word_vector(text: Optional[str]) -> Counter:
    tokens = _tokenize(text)
    return Counter(t for t in tokens if t in FUNCTION_WORDS)


def _cosine_sim(v1: Counter, v2: Counter) -> Optional[float]:
    keys = set(v1) | set(v2)
    if not keys:
        return None
    dot = sum(v1.get(k, 0) * v2.get(k, 0) for k in keys)
    n1 = math.sqrt(sum(v * v for v in v1.values()))
    n2 = math.sqrt(sum(v * v for v in v2.values()))
    if n1 == 0 or n2 == 0:
        return None
    return dot / (n1 * n2)


def accommodation_score(participant_text: str, ai_text: str) -> Optional[float]:
    """RQ2 layer 1 — cosine similarity of function-word frequency vectors.
    Higher = AI's function-word usage more closely mirrors the participant's."""
    return _cosine_sim(_function_word_vector(participant_text), _function_word_vector(ai_text))


def is_followup_question(ai_text: Optional[str]) -> bool:
    """RQ2 layer 3 — crude proxy for 'AI asked a follow-up' behavioral choice."""
    return bool(ai_text) and "?" in ai_text


def _vec_cosine(a: list[float], b: list[float]) -> Optional[float]:
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return None
    return dot / (na * nb)


def _ms_between(a: Optional[str], b: Optional[str]) -> Optional[float]:
    """Milliseconds between two ISO timestamp strings (b - a), or None."""
    if not a or not b:
        return None
    from datetime import datetime
    try:
        ta = datetime.fromisoformat(a.replace("Z", "+00:00"))
        tb = datetime.fromisoformat(b.replace("Z", "+00:00"))
        return (tb - ta).total_seconds() * 1000
    except Exception:
        return None


def build_research_dataset() -> dict:
    """Pulls all completed participants + their turns/surveys and computes
    per-turn, per-participant, and per-platform aggregates for the admin
    Research tab. Embeddings (attunement) are fetched concurrently."""
    participants = (
        db_.db().table("participants").select("*")
        .eq("excluded", False)
        .gt("topics_completed", 0)
        .execute().data or []
    )
    p_by_id = {p["id"]: p for p in participants}
    pids = list(p_by_id.keys())
    if not pids:
        return {"participants": [], "platforms": {}, "turns_used": 0}

    voice_turns = (
        db_.db().table("voice_turns").select("*")
        .in_("participant_id", pids)
        .order("participant_id").order("turn_number")
        .execute().data or []
    )
    survey_rows = (
        db_.db().table("survey_responses").select("*")
        .in_("participant_id", pids)
        .execute().data or []
    )

    # Attunement needs embeddings — gather the (participant_text, ai_text) pairs
    # for turns that have both, and embed them all concurrently up front.
    pairs = []
    for vt in voice_turns:
        txt_p = vt.get("whisper_transcript") or ""
        txt_a = vt.get("llm_response_text") or ""
        if txt_p.strip() and txt_a.strip():
            pairs.append((vt["id"], txt_p, txt_a))

    embeddings: dict[str, tuple[list[float], list[float]]] = {}
    if pairs:
        from concurrent.futures import ThreadPoolExecutor

        def _embed_pair(item):
            vt_id, txt_p, txt_a = item
            try:
                e_p = llm_.get_embedding(txt_p)
                e_a = llm_.get_embedding(txt_a)
                return vt_id, (e_p, e_a)
            except Exception as exc:
                print(f"[Analysis] embedding failed for turn {vt_id}: {exc}")
                return vt_id, None

        with ThreadPoolExecutor(max_workers=8) as pool:
            for vt_id, result in pool.map(_embed_pair, pairs):
                if result is not None:
                    embeddings[vt_id] = result

    # ── Per-turn feature computation ──────────────────────────────────────────
    per_participant_turns: dict[str, list[dict]] = {pid: [] for pid in pids}
    for vt in voice_turns:
        pid = vt["participant_id"]
        if pid not in per_participant_turns:
            continue
        txt_p = vt.get("whisper_transcript") or ""
        txt_a = vt.get("llm_response_text") or ""

        attunement = None
        if vt["id"] in embeddings:
            e_p, e_a = embeddings[vt["id"]]
            attunement = _vec_cosine(e_p, e_a)

        per_participant_turns[pid].append({
            "topic":              vt.get("topic"),
            "turn_number":        vt.get("turn_number"),
            "word_count":         len(_tokenize(txt_p)),
            "first_person_ratio": word_ratio(txt_p, FIRST_PERSON_SINGULAR),
            "negemo_ratio":       word_ratio(txt_p, NEGEMO_WORDS),
            "tentative_ratio":    word_ratio(txt_p, TENTATIVE_WORDS),
            "accommodation":      accommodation_score(txt_p, txt_a) if (txt_p and txt_a) else None,
            "attunement":         attunement,
            "followup_question":  1 if is_followup_question(txt_a) else (0 if txt_a else None),
            "ai_response_len":    len(txt_a) if txt_a else None,
            "hesitation_ms":      _ms_between(vt.get("ai_audio_ended_at"), vt.get("record_started_at")),
            "speaking_ms":        _ms_between(vt.get("record_started_at"), vt.get("record_ended_at")),
            "editing_ms":         _ms_between(vt.get("preview_shown_at"), vt.get("submitted_at")),
        })

    def _mean(vals: list[Optional[float]]) -> Optional[float]:
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    survey_by_pid: dict[str, list[dict]] = {}
    for sr in survey_rows:
        survey_by_pid.setdefault(sr["participant_id"], []).append(sr)
    for rows in survey_by_pid.values():
        rows.sort(key=lambda r: (r.get("topic_index") or 0))

    # ── Per-participant aggregate record ──────────────────────────────────────
    out_participants = []
    for pid in pids:
        p = p_by_id[pid]
        turns = per_participant_turns.get(pid, [])
        bfi = p.get("bfi_scores") or {}
        surveys = survey_by_pid.get(pid, [])

        hsps = p.get("hsps_score")
        ai_hsps = p.get("ai_hsps_score")
        hsps_error = abs(ai_hsps - hsps) if (hsps is not None and ai_hsps is not None) else None

        out_participants.append({
            "id":                 pid,
            "prolific_id":        p.get("prolific_id"),
            "platform":           p.get("assigned_platform"),
            "hsps_score":         hsps,
            "neuroticism":        bfi.get("neuroticism"),
            "openness":           bfi.get("openness"),
            "ai_hsps_score":      ai_hsps,
            "hsps_error":         hsps_error,
            "self_mbti":          p.get("self_mbti"),
            "ai_mbti_type":       p.get("ai_mbti_type"),
            "mbti_match":         (
                p.get("self_mbti")[:1] == p.get("ai_mbti_type", "")[:1]
                if p.get("self_mbti") and p.get("ai_mbti_type") else None
            ),
            "total_words":        sum(t["word_count"] for t in turns) if turns else None,
            "first_person_ratio": _mean([t["first_person_ratio"] for t in turns]),
            "negemo_ratio":       _mean([t["negemo_ratio"] for t in turns]),
            "tentative_ratio":    _mean([t["tentative_ratio"] for t in turns]),
            "hesitation_ms":      _mean([t["hesitation_ms"] for t in turns]),
            "speaking_ms":        _mean([t["speaking_ms"] for t in turns]),
            "editing_ms":         _mean([t["editing_ms"] for t in turns]),
            "accommodation":      _mean([t["accommodation"] for t in turns]),
            "attunement":         _mean([t["attunement"] for t in turns]),
            "followup_rate":      _mean([t["followup_question"] for t in turns]),
            "ai_response_len":    _mean([t["ai_response_len"] for t in turns]),
            "surveys":            [
                {
                    "topic_index":  s.get("topic_index"),
                    "satisfaction": s.get("satisfaction"),
                    "trust":        s.get("trust"),
                    "empathy":      s.get("general_empathy"),
                    "mbti_guess":   s.get("mbti_guess"),
                }
                for s in surveys
            ],
            "satisfaction_mean": _mean([s.get("satisfaction") for s in surveys]),
            "trust_mean":        _mean([s.get("trust") for s in surveys]),
            "empathy_mean":      _mean([s.get("general_empathy") for s in surveys]),
        })

    # ── Per-platform aggregate (RQ2) ──────────────────────────────────────────
    platforms: dict[str, dict] = {}
    for row in out_participants:
        plat = row["platform"] or "unknown"
        bucket = platforms.setdefault(plat, {
            "n": 0, "accommodation": [], "attunement": [], "followup_rate": [],
            "ai_response_len": [], "hsps_error": [], "satisfaction": [], "trust": [],
        })
        bucket["n"] += 1
        for key in ("accommodation", "attunement", "followup_rate", "ai_response_len", "hsps_error"):
            if row[key] is not None:
                bucket[key].append(row[key])
        if row["satisfaction_mean"] is not None:
            bucket["satisfaction"].append(row["satisfaction_mean"])
        if row["trust_mean"] is not None:
            bucket["trust"].append(row["trust_mean"])

    platform_summary = {}
    for plat, bucket in platforms.items():
        platform_summary[plat] = {
            "n":                bucket["n"],
            "accommodation":    _mean(bucket["accommodation"]),
            "attunement":       _mean(bucket["attunement"]),
            "followup_rate":    _mean(bucket["followup_rate"]),
            "ai_response_len":  _mean(bucket["ai_response_len"]),
            "hsps_error":       _mean(bucket["hsps_error"]),
            "satisfaction":     _mean(bucket["satisfaction"]),
            "trust":            _mean(bucket["trust"]),
        }

    return {
        "participants": out_participants,
        "platforms":    platform_summary,
        "turns_used":   len(pairs),
        "turns_total":  len(voice_turns),
    }


# ── Pre-survey factors by HSP tier (Overview tab) ─────────────────────────────
# Screening-survey fields, split by a median HSPS cut into "low"/"high" tiers,
# to eyeball whether any of them tracks HSP rather than being evenly spread —
# a quick confound check, not a substitute for actually modeling covariates.

_ORDERED_CATEGORIES = {
    "ai_usage_frequency":     ["never", "rarely", "sometimes", "often", "very_often"],
    "financial_worry":       ["never", "rarely", "sometimes", "often", "always"],
    "education":             ["no_formal", "primary", "secondary", "vocational",
                               "bachelors", "masters", "doctorate"],
    "mental_health_screening": ["no", "yes"],
    "native_english":        [True, False],
}

_CATEGORICAL_FIELDS = [
    "gender", "native_english", "ai_usage_frequency",
    "financial_worry", "education", "mental_health_screening", "race",
]


def build_presurvey_dataset() -> dict:
    """All participants who completed the screening survey (hsps_score not
    null), regardless of later exclusion/completion — this checks the
    screening pool itself, not just people who finished the study."""
    participants = (
        db_.db().table("participants")
        .select("id, hsps_score, age, gender, native_english, ai_usage_frequency, "
                "financial_worry, education, mental_health_screening, race")
        .execute().data or []
    )
    participants = [p for p in participants if p.get("hsps_score") is not None]
    if not participants:
        return {"n": 0}

    scores = sorted(p["hsps_score"] for p in participants)
    mid = len(scores) // 2
    median_hsps = scores[mid] if len(scores) % 2 else (scores[mid - 1] + scores[mid]) / 2

    def tier(p):
        return "high" if p["hsps_score"] >= median_hsps else "low"

    low = [p for p in participants if tier(p) == "low"]
    high = [p for p in participants if tier(p) == "high"]

    age = {
        "low":  [p["age"] for p in low if p.get("age") is not None],
        "high": [p["age"] for p in high if p.get("age") is not None],
    }

    fields = {}
    for field in _CATEGORICAL_FIELDS:
        cats_seen = {p[field] for p in participants if p.get(field) is not None}
        order = _ORDERED_CATEGORIES.get(field)
        categories = order if order else sorted(cats_seen, key=str)
        categories = [c for c in categories if c in cats_seen]

        def pct_by_category(group):
            total = sum(1 for p in group if p.get(field) is not None)
            counts = Counter(p[field] for p in group if p.get(field) is not None)
            return [round(100 * counts.get(c, 0) / total, 1) if total else 0 for c in categories]

        fields[field] = {
            "categories": [str(c) for c in categories],
            "low_pct":    pct_by_category(low),
            "high_pct":   pct_by_category(high),
        }

    return {
        "n": len(participants),
        "n_low": len(low),
        "n_high": len(high),
        "median_hsps": round(median_hsps, 2),
        "age": age,
        "fields": fields,
    }
