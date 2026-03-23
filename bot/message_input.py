"""
Telegram message input normalisation for Hermes.

Converts various Telegram message types (text, voice notes, audio files,
photos, image documents) into a single text string that the LLM agent
can process.

Architecture notes:
  - Voice notes and audio files are downloaded from Telegram, transcribed
    via the media module, and the transcript is prepended/appended to any
    caption text.
  - Photos and image documents are downloaded, analysed by the vision
    model, and the description is combined with the caption.
  - Telegram provides photos as an array of different sizes; we always
    use the last one (highest resolution) for best analysis quality.
  - Image documents (e.g. PNG files sent as attachments rather than
    compressed photos) are detected by checking the MIME type.
  - Temporary files are cleaned up in finally blocks to prevent disk leaks.
  - Returns None for unsupported message types so the caller can prompt
    the user about what formats are accepted.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from bot import media


def image_document(message) -> bool:
    """Check if a Telegram message contains an image sent as a document attachment."""
    doc = getattr(message, "document", None)
    return bool(doc and getattr(doc, "mime_type", "").startswith("image/"))


async def download_telegram_file(bot, file_id: str, suffix: str) -> Path:
    """
    Download a file from Telegram's servers to a local temporary file.

    Returns the Path to the downloaded file. The caller is responsible
    for cleaning up the temporary file when done.
    """
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    out = Path(path)
    telegram_file = await bot.get_file(file_id)
    await telegram_file.download_to_drive(custom_path=str(out))
    return out


async def build_agent_input(message, bot) -> str | None:
    """
    Convert a Telegram message into a text string for the LLM agent.

    Handles four message types:
      1. Plain text - returned as-is
      2. Voice/audio - transcribed to text, combined with any caption
      3. Photo - analysed by vision model, combined with any caption
      4. Image document - same as photo but sent as file attachment

    Returns None if the message type is not supported (e.g. stickers,
    video, contacts, etc.).
    """
    if not message:
        return None

    # Plain text messages are the simplest case
    if getattr(message, "text", None):
        return message.text.strip()

    # Caption text that accompanies media messages
    caption = (getattr(message, "caption", "") or "").strip()

    # Voice notes and audio files: download, transcribe, combine with caption
    if getattr(message, "voice", None) or getattr(message, "audio", None):
        audio = message.voice or message.audio
        # Use the original file extension if available, default to .ogg
        # (Telegram voice notes are typically OGG/Opus format)
        suffix = Path(getattr(audio, "file_name", "") or "").suffix or ".ogg"
        path = await download_telegram_file(bot, audio.file_id, suffix)
        try:
            transcript = await media.transcribe_voice(path)
        finally:
            path.unlink(missing_ok=True)
        return f"{caption}\n\nVoice note transcript:\n{transcript}".strip()

    # Photos and image documents: download, analyse with vision model
    if getattr(message, "photo", None) or image_document(message):
        if getattr(message, "photo", None):
            # Telegram provides multiple photo sizes; use the last (highest res)
            file_id = message.photo[-1].file_id
            suffix = ".jpg"
        else:
            # Document-type images preserve the original filename and format
            file_id = message.document.file_id
            suffix = Path(message.document.file_name or "").suffix or ".jpg"
        path = await download_telegram_file(bot, file_id, suffix)
        try:
            image_context = await media.describe_image(path, caption)
        finally:
            path.unlink(missing_ok=True)
        if caption:
            return f"{caption}\n\nImage context:\n{image_context}"
        # No caption: use a generic prompt so the agent knows to help with the image
        return f"Please help with this image.\n\nImage context:\n{image_context}"

    # Unsupported message type
    return None
