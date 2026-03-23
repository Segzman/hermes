"""
Bounded shell execution for Hermes.

Commands run in their own process group so Hermes can aggressively kill any
leftover children after completion or timeout.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TERMINAL_DEFAULT_CWD = Path(os.path.expanduser(os.getenv("TERMINAL_DEFAULT_CWD", "~/hermes")))
TERMINAL_SHELL = os.getenv("TERMINAL_SHELL", "/bin/bash")
TERMINAL_TIMEOUT_SECONDS = max(1, int(os.getenv("TERMINAL_TIMEOUT_SECONDS", "20")))
TERMINAL_MAX_TIMEOUT_SECONDS = max(
    TERMINAL_TIMEOUT_SECONDS,
    int(os.getenv("TERMINAL_MAX_TIMEOUT_SECONDS", "120")),
)
TERMINAL_MAX_OUTPUT_CHARS = max(1000, int(os.getenv("TERMINAL_MAX_OUTPUT_CHARS", "12000")))


def _resolve_cwd(cwd: str = "") -> Path:
    raw = (cwd or "").strip()
    if not raw:
        path = TERMINAL_DEFAULT_CWD
    else:
        path = Path(os.path.expanduser(raw))
        if not path.is_absolute():
            path = TERMINAL_DEFAULT_CWD / path
    path = path.resolve()
    if not path.exists():
        raise RuntimeError(f"Working directory does not exist: {path}")
    if not path.is_dir():
        raise RuntimeError(f"Working directory is not a directory: {path}")
    return path


def _clamp_timeout(timeout_seconds: int | None) -> int:
    if timeout_seconds is None:
        return TERMINAL_TIMEOUT_SECONDS
    return max(1, min(int(timeout_seconds), TERMINAL_MAX_TIMEOUT_SECONDS))


def _validate_service_name(service: str) -> str:
    name = (service or "").strip()
    if not name:
        raise RuntimeError("Service name is required.")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", name):
        raise RuntimeError(f"Invalid service name: {service}")
    return name


def _truncate_output(text: str) -> str:
    if len(text) <= TERMINAL_MAX_OUTPUT_CHARS:
        return text
    keep = TERMINAL_MAX_OUTPUT_CHARS - 16
    return text[:keep] + "\n...[truncated]"


def _kill_process_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def run_command(command: str, cwd: str = "", timeout_seconds: int | None = None) -> dict:
    cmd = (command or "").strip()
    if not cmd:
        raise RuntimeError("Command is required.")

    workdir = _resolve_cwd(cwd)
    timeout = _clamp_timeout(timeout_seconds)
    proc = subprocess.Popen(
        [TERMINAL_SHELL, "-lc", cmd],
        cwd=str(workdir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    timed_out = False
    output = ""
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(proc.pid, signal.SIGTERM)
        try:
            output, _ = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc.pid, signal.SIGKILL)
            output, _ = proc.communicate()
    finally:
        _kill_process_group(proc.pid, signal.SIGTERM)

    return {
        "command": cmd,
        "cwd": str(workdir),
        "exit_code": int(proc.returncode or 0),
        "timed_out": timed_out,
        "output": _truncate_output((output or "").strip()),
    }


def service_status(service: str) -> dict:
    name = _validate_service_name(service)
    return run_command(
        command=f"systemctl status {name} --no-pager -l | head -n 40",
        cwd="/home/ubuntu",
        timeout_seconds=20,
    )


def service_restart(service: str) -> dict:
    name = _validate_service_name(service)
    return run_command(
        command=f"sudo systemctl restart {name} && systemctl is-active {name}",
        cwd="/home/ubuntu",
        timeout_seconds=30,
    )


def tail_logs(service: str, lines: int = 100) -> dict:
    name = _validate_service_name(service)
    count = max(1, min(int(lines), 400))
    return run_command(
        command=f"journalctl -u {name} -n {count} --no-pager",
        cwd="/home/ubuntu",
        timeout_seconds=20,
    )
