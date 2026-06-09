"""
LLM service — routes to the correct provider based on platform name.
"""
import time
from typing import Optional

import openai
import anthropic

from app.config import settings, SYSTEM_PROMPT, LLM_TEMPERATURE, LLM_MAX_TOKENS

_openai_client: Optional[openai.OpenAI] = None
_anthropic_client: Optional[anthropic.Anthropic] = None
_gemini_client: Optional[openai.OpenAI] = None
_deepseek_client: Optional[openai.OpenAI] = None
_groq_client: Optional[openai.OpenAI] = None


def _openai() -> openai.OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = openai.OpenAI(api_key=settings.OPENAI_API_KEY)
    return _openai_client

def _anthropic() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _anthropic_client

def _gemini() -> openai.OpenAI:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = openai.OpenAI(
            api_key=settings.GOOGLE_API_KEY,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
    return _gemini_client

def _deepseek() -> openai.OpenAI:
    global _deepseek_client
    if _deepseek_client is None:
        _deepseek_client = openai.OpenAI(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url="https://api.deepseek.com/v1",
        )
    return _deepseek_client

def _groq() -> openai.OpenAI:
    global _groq_client
    if _groq_client is None:
        _groq_client = openai.OpenAI(
            api_key=settings.GROQ_API_KEY,
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
        if platform in ("gpt-4o", "gpt-4o-mini"):
            return _call_openai_compat(_openai(), platform, conversation_history, actual_system, actual_max_tokens)
        elif platform == "claude-sonnet-4-6":
            return _call_anthropic(conversation_history, actual_system, actual_max_tokens)
        elif platform == "gemini-2.5-flash":
            return _call_openai_compat(_gemini(), platform, conversation_history, actual_system, actual_max_tokens)
        elif platform == "deepseek-chat":
            return _call_openai_compat(_deepseek(), platform, conversation_history, actual_system, actual_max_tokens)
        elif platform == "llama-3.3-70b-versatile":
            return _call_openai_compat(_groq(), platform, conversation_history, actual_system, actual_max_tokens)
        else:
            raise ValueError(f"Unknown platform: {platform}")

    text = _with_retry(_call)

    elapsed_ms = int((time.time() - start) * 1000)
    return text, elapsed_ms


# Per-call hard timeout. SDK default is 600s (10 min!) — far too long for a
# voice study where participants are watching a "thinking…" indicator.
_LLM_TIMEOUT_S = 30


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
