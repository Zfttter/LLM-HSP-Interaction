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


def call_llm(
    platform: str,
    conversation_history: list[dict],
    system_prompt: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> tuple[str, int]:
    actual_system = system_prompt if system_prompt is not None else SYSTEM_PROMPT
    actual_max_tokens = max_tokens if max_tokens is not None else LLM_MAX_TOKENS

    start = time.time()

    if platform in ("gpt-4o", "gpt-4o-mini"):
        text = _call_openai_compat(_openai(), platform, conversation_history, actual_system, actual_max_tokens)
    elif platform == "claude-sonnet-4-6":
        text = _call_anthropic(conversation_history, actual_system, actual_max_tokens)
    elif platform == "gemini-2.0-flash":
        text = _call_openai_compat(_gemini(), platform, conversation_history, actual_system, actual_max_tokens)
    elif platform == "deepseek-chat":
        text = _call_openai_compat(_deepseek(), platform, conversation_history, actual_system, actual_max_tokens)
    elif platform == "llama-3.3-70b-versatile":
        text = _call_openai_compat(_groq(), platform, conversation_history, actual_system, actual_max_tokens)
    else:
        raise ValueError(f"Unknown platform: {platform}")

    elapsed_ms = int((time.time() - start) * 1000)
    return text, elapsed_ms


def _call_openai_compat(client, model, history, system_prompt, max_tokens):
    messages = [{"role": "system", "content": system_prompt}] + history
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=LLM_TEMPERATURE,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content.strip()


def _call_anthropic(history, system_prompt, max_tokens):
    response = _anthropic().messages.create(
        model="claude-sonnet-4-6",
        system=system_prompt,
        messages=history,
        temperature=LLM_TEMPERATURE,
        max_tokens=max_tokens,
    )
    return response.content[0].text.strip()
