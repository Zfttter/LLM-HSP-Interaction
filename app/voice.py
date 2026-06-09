"""
Voice pipeline — Whisper (STT) + OpenAI TTS.
Identical for all 6 LLM platforms — voice quality is a controlled variable.
"""
import io
import base64

import openai

from app.config import settings, TTS_VOICE, TTS_MODEL, WHISPER_MODEL


def transcribe_audio(audio_bytes: bytes) -> str:
    """Send raw audio bytes to Whisper-1. Returns transcript string."""
    client = openai.OpenAI(api_key=settings.OPENAI_API_KEY)
    buf = io.BytesIO(audio_bytes)
    buf.name = "audio.webm"           # Whisper needs a filename hint
    try:
        result = client.audio.transcriptions.create(
            model=WHISPER_MODEL,
            file=buf,
            language="en",            # study is English-only — don't let Whisper guess
            temperature=0,
        )
    except Exception as exc:
        print(f"[Whisper] transcription failed (bytes={len(audio_bytes)}): {exc}")
        raise

    text = (result.text or "").strip()
    print(f"[Whisper] audio={len(audio_bytes)}B → {len(text)} chars: {text[:120]!r}")
    return text


def text_to_speech(text: str, voice: str = TTS_VOICE) -> str:
    """Convert text to speech. Returns base64-encoded MP3 string."""
    client = openai.OpenAI(api_key=settings.OPENAI_API_KEY)
    response = client.audio.speech.create(
        model=TTS_MODEL,
        voice=voice,
        input=text,
        response_format="mp3",
    )
    return base64.b64encode(response.content).decode("utf-8")
