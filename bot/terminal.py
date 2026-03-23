"""
Bounded shell execution for Hermes.

Commands run in their own process group so Hermes can aggressively kill any
leftover children after completion or timeout.

Architecture notes:
  - Each command runs in a new process group (start_new_session=True) so
    that the entire group can be killed with os.killpg on timeout, ensuring
    no zombie processes are left behind.
  - On timeout, SIGTERM is sent first (graceful shutdown), then SIGKILL
    after 2 seconds if the process does not exit.
  - A final SIGTERM is always sent in the finally block as a safety net
    to catch any edge cases where the process survived.
  - Output is truncated to TERMINAL_MAX_OUTPUT_CHARS to prevent the LLM
    from receiving enormous outputs that would blow up the token budget.
  - The timeout is clamped to TERMINAL_MAX_TIMEOUT_SECONDS to prevent
    the agent from requesting unreasonably long timeouts.
  - Service operations (status, restart, logs) use well-known systemctl/
    journalctl commands with fixed timeouts, since these are common
    operations the agent performs.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Default working directory for commands
TERMINAL_DEFAULT_CWD = Path(os.path.expanduser(os.getenv("TERMINAL_DEFAULT_CWD", "~/hermes")))
# Shell to use for executing commands (login shell via -lc for env setup)
TERMINAL_SHELL = os.getenv("TERMINAL_SHELL", "/bin/bash")
# Default timeout (seconds) if none is specified by the agent
TERMINAL_TIMEOUT_SECONDS = max(1, int(os.getenv("TERMINAL_TIMEOUT_SECONDS", "20")))
# Absolute maximum timeout the agent is allowed to request
TERMINAL_MAX_TIMEOUT_SECONDS = max(
    TERMINAL_TIMEOUT_SECONDS,
    int(os.getenv("TERMINAL_MAX_TIMEOUT_SECONDS", "120")),
)
# Maximum output characters to return (prevents token budget blowout)
TERMINAL_MAX_OUTPUT_CHARS = max(1000, int(os.getenv("TERMINAL_MAX_OUTPUT_CHARS", "12000")))


def _resolve_cwd(cwd: str = "") -> Path:
    """
    Resolve the working directory for a command.

    If empty, uses the default CWD. Relative paths are resolved against
    the default CWD. The directory must exist and be a directory.
    """
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
    """
    Clamp the timeout to a safe range.

    Returns the default timeout if None, otherwise clamps between 1 and
    TERMINAL_MAX_TIMEOUT_SECONDS.
    """
    if timeout_seconds is None:
        return TERMINAL_TIMEOUT_SECONDS
    return max(1, min(int(timeout_seconds), TERMINAL_MAX_TIMEOUT_SECONDS))


def _validate_service_name(service: str) -> str:
    """
    Validate a systemd service name to prevent command injection.

    Only allows alphanumeric characters, dots, underscores, hyphens, and @.
    These are the characters allowed in systemd unit names.
    """
    name = (service or "").strip()
    if not name:
        raise RuntimeError("Service name is required.")
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", name):
        raise RuntimeError(f"Invalid service name: {service}")
    return name


def _truncate_output(text: str) -> str:
    """Truncate command output to the configured maximum character limit."""
    if len(text) <= TERMINAL_MAX_OUTPUT_CHARS:
        return text
    keep = TERMINAL_MAX_OUTPUT_CHARS - 16
    return text[:keep] + "\n...[truncated]"


def _kill_process_group(pid: int, sig: int) -> None:
    """
    Send a signal to an entire process group.

    Uses os.killpg to kill all processes in the group, not just the leader.
    ProcessLookupError is expected when the process already exited.
    """
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def run_command(command: str, cwd: str = "", timeout_seconds: int | None = None) -> dict:
    """
    Run a shell command with bounded execution time.

    The command is run via the configured shell (bash -lc) to get the
    user's login environment. start_new_session=True creates a new
    process group so the entire tree can be killed cleanly.

    Returns a dict with:
      - command: the command that was run
      - cwd: the working directory used
      - exit_code: process exit code (0 = success)
      - timed_out: whether the command was killed due to timeout
      - output: stdout+stderr combined, truncated to the char limit
    """
    cmd = (command or "").strip()
    if not cmd:
        raise RuntimeError("Command is required.")

    workdir = _resolve_cwd(cwd)
    timeout = _clamp_timeout(timeout_seconds)
    # start_new_session=True creates a new process group for clean killing
    proc = subprocess.Popen(
        [TERMINAL_SHELL, "-lc", cmd],
        cwd=str(workdir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # Merge stderr into stdout
        text=True,
        start_new_session=True,
    )

    timed_out = False
    output = ""
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        # Graceful shutdown: try SIGTERM first
        _kill_process_group(proc.pid, signal.SIGTERM)
        try:
            # Give the process 2 seconds to clean up after SIGTERM
            output, _ = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            # Forceful shutdown: SIGKILL if SIGTERM didn't work
            _kill_process_group(proc.pid, signal.SIGKILL)
            output, _ = proc.communicate()
    finally:
        # Safety net: always try to clean up the process group
        _kill_process_group(proc.pid, signal.SIGTERM)

    return {
        "command": cmd,
        "cwd": str(workdir),
        "exit_code": int(proc.returncode or 0),
        "timed_out": timed_out,
        "output": _truncate_output((output or "").strip()),
    }


def service_status(service: str) -> dict:
    """
    Check the status of a systemd service.

    Uses 'systemctl status' with --no-pager and pipes through head
    to limit output to 40 lines (status output can be verbose).
    """
    name = _validate_service_name(service)
    return run_command(
        command=f"systemctl status {name} --no-pager -l | head -n 40",
        cwd="/home/ubuntu",
        timeout_seconds=20,
    )


def service_restart(service: str) -> dict:
    """
    Restart a systemd service and verify it came back up.

    Runs 'systemctl restart' followed by 'systemctl is-active' to
    confirm the service is running after restart.
    """
    name = _validate_service_name(service)
    return run_command(
        command=f"sudo systemctl restart {name} && systemctl is-active {name}",
        cwd="/home/ubuntu",
        timeout_seconds=30,
    )


def tail_logs(service: str, lines: int = 100) -> dict:
    """
    Read recent journal logs for a systemd service.

    The line count is clamped to 1-400 to prevent excessive output.
    """
    name = _validate_service_name(service)
    count = max(1, min(int(lines), 400))
    return run_command(
        command=f"journalctl -u {name} -n {count} --no-pager",
        cwd="/home/ubuntu",
        timeout_seconds=20,
    )
