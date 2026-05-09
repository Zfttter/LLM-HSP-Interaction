"""
Survey scoring and exclusion logic.
Condition assignment itself is handled atomically via PostgreSQL RPC (see database.py).
"""
from app.config import HSPS_ITEMS, BFI_ITEMS


def score_hsps(responses: dict) -> float:
    """Mean of 18 HSPS items (each 1-7). Returns float in [1, 7]."""
    total = sum(responses[f"hsps_{i}"] for i in range(1, 19))
    return round(total / 18, 4)


def score_bfi(responses: dict) -> dict:
    """
    Score BFI-44 into 5 dimensions.
    Reverse-coded items (marked R): reversed = 6 - raw (scale 1–5).
    Each dimension = mean of its items.
    """
    r = responses

    def rev(x: int) -> int:
        return 6 - x

    # (item_number, reversed)
    E_items = [(1,False),(6,True),(11,False),(16,False),(21,True),(26,False),(31,True),(36,False)]
    A_items = [(2,True),(7,False),(12,True),(17,False),(22,False),(27,True),(32,False),(37,True),(42,False)]
    C_items = [(3,False),(8,True),(13,False),(18,True),(23,True),(28,False),(33,False),(38,False),(43,True)]
    N_items = [(4,False),(9,True),(14,False),(19,False),(24,True),(29,False),(34,True),(39,False)]
    O_items = [(5,False),(10,False),(15,False),(20,False),(25,False),(30,False),(35,True),(40,False),(41,True),(44,False)]

    def dim_mean(items):
        scores = [rev(r[f"bfi_{i}"]) if reversed_ else r[f"bfi_{i}"]
                  for i, reversed_ in items]
        return round(sum(scores) / len(scores), 4)

    return {
        "extraversion":      dim_mean(E_items),
        "agreeableness":     dim_mean(A_items),
        "conscientiousness": dim_mean(C_items),
        "neuroticism":       dim_mean(N_items),
        "openness":          dim_mean(O_items),
        "raw": {f"bfi_{i}": r[f"bfi_{i}"] for i in range(1, 45)},
    }


def check_exclusion(age: int, native_english: str, ai_usage: str) -> tuple[bool, str]:
    """
    Returns (excluded: bool, reason: str).
    Excluded if: age < 18, non-native English, or frequent AI usage.
    """
    if age < 18:
        return True, "age_under_18"
    if native_english.lower() != "yes":
        return True, "non_native_english"
    if ai_usage in ("often", "very_often"):
        return True, "frequent_ai_usage"
    return False, ""
