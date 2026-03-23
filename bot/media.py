"""
Media processing for Hermes: voice transcription and image understanding.

Voice notes are transcribed using an audio-capable LLM via OpenRouter.
Images are analysed using a vision LLM via Amazon Bedrock.

Architecture notes:
  - Voice transcription converts the input audio to MP3 first (using ffmpeg)
    because the LLM API expects MP3 format for the input_audio content type.
  - Image understanding uses a separate provider (Bedrock) from the main
    chat model because vision models require specific multimodal capabilities
    that may not be available on the free OpenRouter models.
  - Both functions are async because they are called from the Telegram
    message handler which runs in an asyncio event loop.
  - Temporary files are cleaned up in finally blocks to prevent disk leaks.
"""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

# API configuration for the two providers used by media processing
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
BEDROCK_BASE_URL = os.getenv("BEDROCK_BASE_URL", "https://bedrock-mantle.us-east-1.api.aws/v1")
BEDROCK_API_KEY = os.getenv("BEDROCK_API_KEY", "")
# Model for voice transcription (must support audio input)
VOICE_TRANSCRIBE_MODEL = os.getenv("VOICE_TRANSCRIBE_MODEL", "openai/gpt-audio-mini")
# Model for image understanding (must support image/vision input)
BEDROCK_VISION_MODEL = os.getenv("BEDROCK_VISION_MODEL", "qwen.qwen3-vl-235b-a22b-instruct")


def _data_url(path: str | Path, mime_type: str) -> str:
    """
    Read a file and encode it as a base64 data URL.

    Used for embedding images in the LLM API request as inline data
    rather than requiring a hosted URL.
    """
    raw = Path(path).read_bytes()
    return f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}"


def _convert_audio_to_mp3(path: str | Path) -> Path:
    """
    Convert any audio file to MP3 using ffmpeg.

    Telegram sends voice notes as OGG/Opus files, but the transcription
    API expects MP3. The output is written to a temporary file that the
    caller is responsible for cleaning up.
    """
    source = Path(path)
    fd, out = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    out_path = Path(out)
    subprocess.run(
        [
            "ffmpeg",
            "-y",           # Overwrite output without asking
            "-i",
            str(source),
            "-vn",          # No video (audio only)
            "-acodec",
            "libmp3lame",   # Encode as MP3
            str(out_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return out_path


def _openrouter_client() -> AsyncOpenAI:
    """Create an AsyncOpenAI client configured for OpenRouter (voice transcription)."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not configured for voice transcription.")
    return AsyncOpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url=OPENROUTER_BASE_URL,
        default_headers={
            "HTTP-Referer": "https://github.com/hermes-slate-assistant",
            "X-Title": "Hermes Slate Assistant",
        },
    )


def _bedrock_client() -> AsyncOpenAI:
    """Create an AsyncOpenAI client configured for Bedrock (image understanding)."""
    if not BEDROCK_API_KEY:
        raise RuntimeError("BEDROCK_API_KEY not configured for image understanding.")
    return AsyncOpenAI(api_key=BEDROCK_API_KEY, base_url=BEDROCK_BASE_URL)


async def transcribe_voice(path: str | Path) -> str:
    """
    Transcribe a voice note audio file to text.

    Converts the audio to MP3, sends it to the transcription model as
    base64-encoded inline audio, and returns the plain text transcript.
    The temporary MP3 file is cleaned up after the API call.
    """
    converted = _convert_audio_to_mp3(path)
    try:
        b64 = base64.b64encode(converted.read_bytes()).decode("ascii")
        client = _openrouter_client()
        resp = await client.chat.completions.create(
            model=VOICE_TRANSCRIBE_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Transcribe this voice note into plain text only. Do not summarize."},
                        {"type": "input_audio", "input_audio": {"data": b64, "format": "mp3"}},
                    ],
                }
            ],
            max_tokens=800,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            raise RuntimeError("Voice transcription returned no text.")
        return text
    finally:
        # Always clean up the temporary MP3 file
        converted.unlink(missing_ok=True)


async def describe_image(path: str | Path, prompt: str = "") -> str:
    """
    Analyse an image and return a concise text description.

    The description is tailored for a personal assistant context:
    it focuses on actionable information like deadlines, instructions,
    UI errors, and to-do items visible in the image.

    An optional prompt parameter lets the user provide additional context
    about what they want to know about the image.
    """
    # Determine the correct MIME type for the image
    mime_type = "image/jpeg"
    suffix = Path(path).suffix.lower()
    if suffix == ".png":
        mime_type = "image/png"
    elif suffix == ".webp":
        mime_type = "image/webp"

    client = _bedrock_client()
    guidance = (
        "Extract the important information from this image for a personal assistant. "
        "Be concise and factual. Mention visible deadlines, instructions, UI errors, to-dos, or text worth acting on."
    )
    if prompt:
        guidance += f" User context: {prompt}"
    resp = await client.chat.completions.create(
        model=BEDROCK_VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": guidance},
                    {"type": "image_url", "image_url": {"url": _data_url(path, mime_type)}},
                ],
            }
        ],
        max_tokens=800,
        temperature=0.2,  # Low temperature for factual/extraction tasks
    )
    text = (resp.choices[0].message.content or "").strip()
    if not text:
        raise RuntimeError("Image understanding returned no text.")
    return text
