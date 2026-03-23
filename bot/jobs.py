"""
Background job manager for Hermes.

Long-running browser/server tasks can run in a separate sub-agent conversation
so the main chat stays responsive and can report status.
"""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Toronto")
MAX_ACTIVE_JOBS_PER_CHAT = max(1, int(os.getenv("MAX_ACTIVE_JOBS_PER_CHAT", "1")))
MAX_JOBS_PER_CHAT = max(MAX_ACTIVE_JOBS_PER_CHAT, int(os.getenv("MAX_JOBS_PER_CHAT", "25")))
MAX_PARTIAL_RETRIES = max(0, int(os.getenv("BACKGROUND_JOB_PARTIAL_RETRIES", "1")))

_lock = threading.RLock()
_jobs_by_chat: dict[str, list["BackgroundJob"]] = {}
_jobs_by_id: dict[str, "BackgroundJob"] = {}


@dataclass
class BackgroundJob:
    id: str
    chat_id: str
    worker_chat_id: str
    prompt: str
    state: str = "queued"
    last_status: str = "Queued"
    result: str = ""
    error: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    completed_at: Optional[datetime] = None
    status_history: list[str] = field(default_factory=list)
    task: Optional[asyncio.Task] = field(default=None, repr=False, compare=False)


def _job_id() -> str:
    return uuid.uuid4().hex[:8]


def _trim_prompt(prompt: str, limit: int = 90) -> str:
    text = " ".join((prompt or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _trim_text(text: str, limit: int = 220) -> str:
    value = " ".join((text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _looks_incomplete_result(text: str) -> bool:
    value = " ".join((text or "").strip().lower().split())
    if not value:
        return True
    hints = (
        "let me try again",
        "i'll try again",
        "i will try again",
        "trying again",
        "one moment",
        "hold on",
        "retry",
        "retrying",
        "browser session reset",
        "browser reset",
        "i'm trying again",
        "i am trying again",
        "i'll keep trying",
        "i will keep trying",
        "currently searching",
        "currently loading",
        "still fetching",
        "still loading",
    )
    concrete = (
        "found",
        "here are",
        "results:",
        "download saved",
        "uploaded",
        "completed",
        "done",
        "blocked because",
        "failed because",
        "couldn't complete",
        "could not complete",
    )
    return any(token in value for token in hints) and not any(token in value for token in concrete)


def _format_ts(dt: datetime) -> str:
    return dt.astimezone(LOCAL_TZ).strftime("%b %d %I:%M %p Toronto")


def _active_jobs(chat_id: str) -> list[BackgroundJob]:
    jobs = _jobs_by_chat.get(chat_id, [])
    return [job for job in jobs if job.state in {"queued", "running"}]


def should_background(message: str) -> bool:
    import re

    text = (message or "").strip().lower()
    if not text:
        return False
    meta_prefixes = (
        "are you ",
        "are u ",
        "do you ",
        "did you ",
        "what are you ",
        "what're you ",
        "what are u ",
        "which ",
        "who are you",
        "whats ",
        "what's ",
    )
    meta_topics = (
        "browser use",
        "browser-use",
        "browserbase",
        "browser base",
        "browser",
        "playwright",
        "server",
        "model",
        "openrouter",
        "bedrock",
    )
    if any(text.startswith(prefix) for prefix in meta_prefixes) and any(topic in text for topic in meta_topics):
        return False
    browser_hints = (
        "browser",
        "website",
        "amazon.",
        "click ",
        "type ",
        "upload ",
        "download ",
        "log in",
        "login",
        "search ",
        "find me ",
        "look up ",
        "go to ",
        "visit ",
    )
    terminal_hints = (
        "terminal",
        "server",
        "service",
        "journalctl",
        "systemctl",
        "logs",
        "restart ",
        "install ",
        "ssh ",
    )
    if any(token in text for token in browser_hints + terminal_hints):
        return True
    if re.search(r"\b[\w-]+\.(com|ca|org|net|io|ai)\b", text):
        return True
    if text.startswith("open ") and any(token in text for token in ("site", "page", "portal", "browser", ".")):
        return True
    return False


def is_status_query(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False
    hints = (
        "how's that going",
        "hows that going",
        "how is that going",
        "status",
        "update",
        "progress",
        "still running",
        "what's happening",
        "whats happening",
        "job",
        "task status",
    )
    return any(token in text for token in hints)


def has_active_jobs(chat_id: str) -> bool:
    with _lock:
        return bool(_active_jobs(chat_id))


def _store_job(job: BackgroundJob) -> None:
    jobs = _jobs_by_chat.setdefault(job.chat_id, [])
    jobs.insert(0, job)
    del jobs[MAX_JOBS_PER_CHAT:]
    _jobs_by_id[job.id] = job


def _set_job_state(job: BackgroundJob, state: str, status: str = "", result: str = "", error: str = "") -> None:
    with _lock:
        job.state = state
        if status:
            job.last_status = status
            if not job.status_history or job.status_history[-1] != status:
                job.status_history.append(status)
                del job.status_history[:-8]
        if result:
            job.result = result
        if error:
            job.error = error
        job.updated_at = datetime.now(tz=timezone.utc)
        if state in {"completed", "failed", "cancelled"}:
            job.completed_at = job.updated_at


def _match_job(chat_id: str, ref: str = "") -> Optional[BackgroundJob]:
    jobs = _jobs_by_chat.get(chat_id, [])
    if not jobs:
        return None
    needle = (ref or "").strip().lower()
    if not needle:
        active = [job for job in jobs if job.state in {"queued", "running"}]
        return active[0] if active else jobs[0]

    matches = []
    for job in jobs:
        if needle == job.id.lower():
            return job
        if needle in job.id.lower() or needle in job.prompt.lower():
            matches.append(job)
    return matches[0] if len(matches) == 1 else None


async def start_background_agent_job(
    chat_id: str,
    prompt: str,
    run_agent: Callable[[str, Callable[[str], Awaitable[None]]], Awaitable[str]],
    send_text: Callable[[str, str], Awaitable[None]],
    note_event: Optional[Callable[[str, str], None]] = None,
) -> BackgroundJob:
    with _lock:
        if len(_active_jobs(chat_id)) >= MAX_ACTIVE_JOBS_PER_CHAT:
            raise RuntimeError("Too many active background jobs. Wait for one to finish or cancel one.")
        job = BackgroundJob(
            id=_job_id(),
            chat_id=chat_id,
            worker_chat_id=f"{chat_id}::job:{_job_id()}",
            prompt=prompt,
        )
        _store_job(job)

    async def _runner() -> None:
        async def _status_cb(text: str) -> None:
            text = (text or "").strip()
            if not text:
                return
            _set_job_state(job, job.state, status=text)
            await send_text(chat_id, f"ℹ️ Sub-agent `{job.id}`: {text}")

        try:
            _set_job_state(job, "running", "Working")
            if note_event:
                note_event(chat_id, f"Background sub-agent `{job.id}` started for: {_trim_prompt(prompt)}")
            await send_text(
                chat_id,
                f"🤖 Started background sub-agent `{job.id}`.\nYou can keep chatting while I work on:\n{_trim_prompt(prompt)}",
            )
            attempts = 0
            while True:
                result = await run_agent(job.worker_chat_id, _status_cb)
                if not _looks_incomplete_result(result):
                    break
                if attempts >= MAX_PARTIAL_RETRIES:
                    _set_job_state(job, "failed", status="Stopped before finishing", error=result)
                    if note_event:
                        note_event(chat_id, f"Background sub-agent `{job.id}` stopped before finishing: {_trim_text(result)}")
                    await send_text(
                        chat_id,
                        f"⚠️ Sub-agent `{job.id}` stopped before finishing.\n\n{result}",
                    )
                    return
                attempts += 1
                _set_job_state(job, "running", status="Retrying to finish the task")
                if note_event:
                    note_event(chat_id, f"Background sub-agent `{job.id}` returned a partial step and is retrying.")
                await send_text(
                    chat_id,
                    f"ℹ️ Sub-agent `{job.id}` returned a partial step. Retrying to finish the task.",
                )
            _set_job_state(job, "completed", status="Completed", result=result)
            if note_event:
                note_event(chat_id, f"Background sub-agent `{job.id}` completed: {_trim_text(result)}")
            await send_text(chat_id, f"✅ Sub-agent `{job.id}` finished.\n\n{result}")
        except asyncio.CancelledError:
            _set_job_state(job, "cancelled", status="Cancelled")
            if note_event:
                note_event(chat_id, f"Background sub-agent `{job.id}` was cancelled.")
            await send_text(chat_id, f"🛑 Sub-agent `{job.id}` was cancelled.")
            raise
        except Exception as e:
            _set_job_state(job, "failed", status="Failed", error=str(e))
            if note_event:
                note_event(chat_id, f"Background sub-agent `{job.id}` failed: {e}")
            await send_text(chat_id, f"⚠️ Sub-agent `{job.id}` failed: {e}")

    task = asyncio.create_task(_runner())
    with _lock:
        job.task = task
    return job


def list_jobs_text(chat_id: str, include_done: bool = False, limit: int = 10) -> str:
    with _lock:
        jobs = list(_jobs_by_chat.get(chat_id, []))
    if not include_done:
        jobs = [job for job in jobs if job.state in {"queued", "running"}]
    if not jobs:
        return "No background jobs."

    lines = ["Background jobs:\n"]
    for job in jobs[: max(1, limit)]:
        when = _format_ts(job.updated_at if job.completed_at else job.created_at)
        lines.append(
            f"• `{job.id}` [{job.state}] {when}\n"
            f"  {_trim_prompt(job.prompt, 80)}\n"
            f"  Status: {job.last_status}"
        )
    return "\n".join(lines)


def job_status_text(chat_id: str, ref: str = "") -> str:
    with _lock:
        job = _match_job(chat_id, ref)
    if not job:
        return "No matching background job."

    lines = [
        f"Job `{job.id}`",
        f"State: {job.state}",
        f"Started: {_format_ts(job.created_at)}",
        f"Last status: {job.last_status}",
        f"Prompt: {_trim_prompt(job.prompt, 140)}",
    ]
    if job.completed_at:
        lines.append(f"Finished: {_format_ts(job.completed_at)}")
    if job.error:
        lines.append(f"Error: {job.error}")
    if job.result:
        lines.extend(["", job.result[:900]])
    return "\n".join(lines)


def cancel_job(chat_id: str, ref: str = "") -> str:
    with _lock:
        job = _match_job(chat_id, ref)
        if not job:
            return "No matching background job."
        task = job.task
        if job.state not in {"queued", "running"} or task is None or task.done():
            return f"Background job `{job.id}` is not running."
        task.cancel()
        _set_job_state(job, "cancelled", status="Cancellation requested")
    return f"Cancellation requested for background job `{job.id}`."


def context_summary(chat_id: str, limit: int = 3) -> str:
    with _lock:
        jobs = list(_jobs_by_chat.get(chat_id, []))
    if not jobs:
        return ""
    lines = []
    for job in jobs[: max(1, limit)]:
        lines.append(f"- {job.id} [{job.state}] {_trim_prompt(job.prompt, 70)} :: {job.last_status}")
    return "\n".join(lines)
