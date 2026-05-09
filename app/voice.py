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
    result = client.audio.transcriptions.create(
        model=WHISPER_MODEL,
        file=buf,
    )
    return result.text.strip()


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
