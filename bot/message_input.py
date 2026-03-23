from __future__ import annotations

import os
import tempfile
from pathlib import Path

from bot import media


def image_document(message) -> bool:
    doc = getattr(message, "document", None)
    return bool(doc and getattr(doc, "mime_type", "").startswith("image/"))


async def download_telegram_file(bot, file_id: str, suffix: str) -> Path:
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    out = Path(path)
    telegram_file = await bot.get_file(file_id)
    await telegram_file.download_to_drive(custom_path=str(out))
    return out


async def build_agent_input(message, bot) -> str | None:
    if not message:
        return None

    if getattr(message, "text", None):
        return message.text.strip()

    caption = (getattr(message, "caption", "") or "").strip()

    if getattr(message, "voice", None) or getattr(message, "audio", None):
        audio = message.voice or message.audio
        suffix = Path(getattr(audio, "file_name", "") or "").suffix or ".ogg"
        path = await download_telegram_file(bot, audio.file_id, suffix)
        try:
            transcript = await media.transcribe_voice(path)
        finally:
            path.unlink(missing_ok=True)
        return f"{caption}\n\nVoice note transcript:\n{transcript}".strip()

    if getattr(message, "photo", None) or image_document(message):
        if getattr(message, "photo", None):
            file_id = message.photo[-1].file_id
            suffix = ".jpg"
        else:
            file_id = message.document.file_id
            suffix = Path(message.document.file_name or "").suffix or ".jpg"
        path = await download_telegram_file(bot, file_id, suffix)
        try:
            image_context = await media.describe_image(path, caption)
        finally:
            path.unlink(missing_ok=True)
        if caption:
            return f"{caption}\n\nImage context:\n{image_context}"
        return f"Please help with this image.\n\nImage context:\n{image_context}"

    return None
