"""
LLM service — routes to the correct provider based on platform name.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import openai
import anthropic
import httpx

from app.config import settings, SYSTEM_PROMPT, LLM_TEMPERATURE, LLM_MAX_TOKENS

# Per-call hard timeout enforced at the HTTP layer.
# SDK-level timeout=X doesn't always get respected by OpenAI-compatible endpoints
# (DeepSeek, Gemini, Groq) — so we bake the timeout into the httpx client itself.
_LLM_TIMEOUT_S = 30
_HTTPX_TIMEOUT = httpx.Timeout(
    timeout=_LLM_TIMEOUT_S,     # default for all phases
    connect=10.0,               # but cap connection setup separately
)


def _make_openai_client(api_key: str, base_url: Optional[str] = None) -> openai.OpenAI:
    http = httpx.Client(timeout=_HTTPX_TIMEOUT)
    if base_url:
        return openai.OpenAI(api_key=api_key, base_url=base_url, http_client=http)
    return openai.OpenAI(api_key=api_key, http_client=http)

_openai_client: Optional[openai.OpenAI] = None
_anthropic_client: Optional[anthropic.Anthropic] = None
_gemini_client: Optional[openai.OpenAI] = None
_deepseek_client: Optional[openai.OpenAI] = None
_xai_client: Optional[openai.OpenAI] = None
_groq_client: Optional[openai.OpenAI] = None


def _openai() -> openai.OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = _make_openai_client(settings.OPENAI_API_KEY)
    return _openai_client

def _anthropic() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(
            api_key=settings.ANTHROPIC_API_KEY,
            timeout=_LLM_TIMEOUT_S,
        )
    return _anthropic_client

def _gemini() -> openai.OpenAI:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = _make_openai_client(
            settings.GOOGLE_API_KEY,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
    return _gemini_client

def _deepseek() -> openai.OpenAI:
    global _deepseek_client
    if _deepseek_client is None:
        _deepseek_client = _make_openai_client(
            settings.DEEPSEEK_API_KEY,
            base_url="https://api.deepseek.com/v1",
        )
    return _deepseek_client

def _xai() -> openai.OpenAI:
    global _xai_client
    if _xai_client is None:
        _xai_client = _make_openai_client(
            settings.XAI_API_KEY,
            base_url="https://api.x.ai/v1",
        )
    return _xai_client

def _groq() -> openai.OpenAI:
    global _groq_client
    if _groq_client is None:
        _groq_client = _make_openai_client(
            settings.GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1",
        )
    return _groq_client


_RETRYABLE = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,   # 5xx
    openai.RateLimitError,        # 429 — Gemini's free tier still slips through
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.InternalServerError,
    anthropic.RateLimitError,
)


def _with_retry(fn, *, retries: int = 1, backoff: float = 1.5):
    """Call fn(), retry on transient API errors (5xx, timeout, rate-limit).
    Per-call timeout is set on the client (see _call_openai_compat); keep retries
    small so participants don't wait >1 minute total in the worst case.
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt < retries:
                wait = backoff * (attempt + 1)
                print(f"[LLM] transient error ({type(exc).__name__}), retry {attempt+1}/{retries} in {wait}s")
                time.sleep(wait)
                continue
            raise
    raise last_exc  # pragma: no cover


def call_llm(
    platform: str,
    conversation_history: list[dict],
    system_prompt: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> tuple[str, int]:
    actual_system = system_prompt if system_prompt is not None else SYSTEM_PROMPT
    actual_max_tokens = max_tokens if max_tokens is not None else LLM_MAX_TOKENS

    start = time.time()

    def _call():
        if platform == "gpt-4o":
            return _call_openai_compat(_openai(), platform, conversation_history, actual_system, actual_max_tokens)
        elif platform == "claude-sonnet-4-6":
            return _call_anthropic(conversation_history, actual_system, actual_max_tokens)
        elif platform == "gemini-2.5-flash":
            return _call_openai_compat(_gemini(), platform, conversation_history, actual_system, actual_max_tokens)
        elif platform == "deepseek-chat":
            return _call_openai_compat(_deepseek(), platform, conversation_history, actual_system, actual_max_tokens)
        elif platform == "openai/gpt-oss-120b":
            return _call_openai_compat(_groq(), platform, conversation_history, actual_system, actual_max_tokens)
        elif platform == "grok-4":
            return _call_openai_compat(_xai(), platform, conversation_history, actual_system, actual_max_tokens)
        else:
            raise ValueError(f"Unknown platform: {platform}")

    text = _with_retry(_call)

    elapsed_ms = int((time.time() - start) * 1000)
    return text, elapsed_ms


def _call_openai_compat(client, model, history, system_prompt, max_tokens):
    messages = [{"role": "system", "content": system_prompt}] + history
    t0 = time.time()
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=LLM_TEMPERATURE,
        max_tokens=max_tokens,
        timeout=_LLM_TIMEOUT_S,           # passed directly to .create()
    )
    print(f"[LLM] {model} returned in {time.time()-t0:.2f}s")
    return response.choices[0].message.content.strip()


def _call_anthropic(history, system_prompt, max_tokens):
    t0 = time.time()
    response = _anthropic().messages.create(
        model="claude-sonnet-4-6",
        system=system_prompt,
        messages=history,
        temperature=LLM_TEMPERATURE,
        max_tokens=max_tokens,
        timeout=_LLM_TIMEOUT_S,
    )
    print(f"[LLM] claude-sonnet-4-6 returned in {time.time()-t0:.2f}s")
    return response.content[0].text.strip()


# ── Health check ──────────────────────────────────────────────────────────────
# Fires a real, near-zero-cost request (max_tokens=1) at each provider so a
# bad/missing key or an unreachable endpoint shows up before a participant
# hits it. Kept out of call_llm's retry path — a health check should fail fast.

_HEALTH_TIMEOUT_S = 10


def _health_openai_compat(client, model: str) -> None:
    client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=1,
        timeout=_HEALTH_TIMEOUT_S,
    )


def _health_anthropic() -> None:
    _anthropic().messages.create(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=1,
        timeout=_HEALTH_TIMEOUT_S,
    )


_HEALTH_CHECKS = {
    "gpt-4o":            lambda: _health_openai_compat(_openai(), "gpt-4o"),
    "claude-sonnet-4-6": _health_anthropic,
    "gemini-2.5-flash":  lambda: _health_openai_compat(_gemini(), "gemini-2.5-flash"),
    "deepseek-chat":     lambda: _health_openai_compat(_deepseek(), "deepseek-chat"),
    "openai/gpt-oss-120b": lambda: _health_openai_compat(_groq(), "openai/gpt-oss-120b"),
    "grok-4":            lambda: _health_openai_compat(_xai(), "grok-4"),
}


def _run_one_check(name: str, fn) -> tuple[str, dict]:
    t0 = time.time()
    try:
        fn()
        return name, {"ok": True, "elapsed_ms": int((time.time() - t0) * 1000)}
    except Exception as exc:
        return name, {"ok": False, "elapsed_ms": int((time.time() - t0) * 1000), "error": str(exc)}


def check_all_providers() -> dict:
    """Ping every LLM provider concurrently with a 1-token request. Returns
    {platform: {ok, elapsed_ms, error?}} for all 6 platforms."""
    results: dict = {}
    with ThreadPoolExecutor(max_workers=len(_HEALTH_CHECKS)) as pool:
        futures = [pool.submit(_run_one_check, name, fn) for name, fn in _HEALTH_CHECKS.items()]
        for future in futures:
            name, result = future.result()
            results[name] = result
    return results


def get_embedding(text: str) -> list[float]:
    """Text embedding via OpenAI, used for the admin Research tab's semantic
    similarity ("attunement") measure — not part of the participant flow."""
    response = _openai().embeddings.create(model="text-embedding-3-small", input=text[:8000])
    return response.data[0].embedding
