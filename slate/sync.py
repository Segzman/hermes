"""
Sync a locally authenticated Slate session to the remote Hermes server.

Usage:
    python -m slate.sync
    python -m slate.sync --host ubuntu@example.com --key ~/.ssh/key.pem

This command is intended to run on the user's Mac after completing
`python -m slate.auth` locally in a real browser.

Architecture:
  Because Sheridan uses Microsoft SSO with MFA, authentication must happen
  in a real browser on the user's local machine. However, the Hermes bot
  and checker run on a headless EC2 instance. This module bridges that gap
  by SCP-ing the session cookie file from the local Mac to the remote server.

  The sync flow:
    1. Verify the local session exists and is still valid (via is_logged_in)
    2. Create the remote directory if it doesn't exist (via SSH mkdir)
    3. Copy the session file via SCP
    4. Verify the session works on the remote server by running
       `python -m slate.auth --check` remotely via SSH

  All SSH/SCP commands disable StrictHostKeyChecking to avoid interactive
  prompts (acceptable for a known personal server).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from .auth import SESSION_FILE, is_logged_in

load_dotenv()

# Remote server configuration — all overridable via .env or CLI flags
DEFAULT_HOST = os.getenv("HERMES_HOST", "")
DEFAULT_SSH_KEY = os.getenv("HERMES_SSH_KEY", "")
DEFAULT_REMOTE_SESSION_FILE = os.getenv("HERMES_REMOTE_SLATE_SESSION_FILE", "~/.hermes/slate_session.json")
DEFAULT_REMOTE_REPO = os.getenv("HERMES_REMOTE_REPO", "~/hermes")
DEFAULT_REMOTE_PYTHON = os.getenv("HERMES_REMOTE_PYTHON", "./.venv/bin/python")


def _ssh_base(host: str, key: str = "") -> list[str]:
    """
    Build the base SSH command with optional key and host.
    Disables StrictHostKeyChecking to avoid interactive yes/no prompts
    (this is a known personal server, not a security-sensitive context).
    """
    cmd = ["ssh"]
    if key:
        cmd += ["-i", str(Path(key).expanduser())]
    cmd += ["-o", "StrictHostKeyChecking=no", host]
    return cmd


def _scp_base(key: str = "") -> list[str]:
    """Build the base SCP command with optional key and host-check bypass."""
    cmd = ["scp"]
    if key:
        cmd += ["-i", str(Path(key).expanduser())]
    cmd += ["-o", "StrictHostKeyChecking=no"]
    return cmd


def _remote_dir(path: str) -> str:
    """
    Extract the parent directory from a remote path that may contain '~'.

    Uses a placeholder trick because pathlib.Path normalizes '~' into a
    literal tilde directory on the local filesystem. We swap it out, compute
    the parent, and swap it back.
    """
    p = Path(path.replace("~", "/tmp/home-placeholder"))
    parent = str(p.parent)
    return parent.replace("/tmp/home-placeholder", "~", 1)


def _remote_verify_command(remote_repo: str, remote_python: str, remote_session_file: str) -> str:
    """
    Build the SSH command string to verify the session on the remote server.

    Changes to the repo directory, sets the SLATE_SESSION_FILE env var to
    point to the uploaded file, and runs `python -m slate.auth --check`.
    All arguments are shell-quoted to prevent injection.
    """
    repo = shlex.quote(remote_repo)
    py = shlex.quote(remote_python)
    session = shlex.quote(remote_session_file)
    return (
        f"cd {repo} && "
        f"SLATE_SESSION_FILE={session} {py} -m slate.auth --check"
    )


def _run(cmd: list[str]) -> None:
    """Run a subprocess and raise CalledProcessError on failure."""
    subprocess.run(cmd, check=True)


def sync_session(
    host: str,
    key: str = "",
    remote_session_file: str = DEFAULT_REMOTE_SESSION_FILE,
    remote_repo: str = DEFAULT_REMOTE_REPO,
    remote_python: str = DEFAULT_REMOTE_PYTHON,
    skip_if_expired: bool = False,
) -> bool:
    """
    Copy the local Slate session to the remote server and verify it.

    Returns True on success, False if skipped (when skip_if_expired is True
    and the session is missing/expired). Raises RuntimeError if the session
    is missing/expired and skip_if_expired is False.

    Steps:
      1. Check local session exists and is valid
      2. SSH: create remote directory
      3. SCP: copy session file
      4. SSH: run slate.auth --check on the remote to verify
    """
    if not host:
        raise RuntimeError("No remote host configured. Pass --host or set HERMES_HOST.")
    if not SESSION_FILE.exists():
        if skip_if_expired:
            print("No local Slate session found. Skipping sync.")
            return False
        raise RuntimeError("No local Slate session found. Run `python -m slate.auth` first.")
    if not asyncio.run(is_logged_in()):
        if skip_if_expired:
            print("Local Slate session is expired. Skipping sync.")
            return False
        raise RuntimeError("Local Slate session is expired. Run `python -m slate.auth` first.")

    # Step 1: Ensure the remote directory exists
    _run(_ssh_base(host, key) + [f"mkdir -p {shlex.quote(_remote_dir(remote_session_file))}"])
    # Step 2: Copy the session file to the remote server
    _run(_scp_base(key) + [str(SESSION_FILE), f"{host}:{remote_session_file}"])
    # Step 3: Verify the session works on the remote by running auth --check
    _run(_ssh_base(host, key) + [_remote_verify_command(remote_repo, remote_python, remote_session_file)])
    return True


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the sync command."""
    parser = argparse.ArgumentParser(description="Sync local Slate session to the Hermes server.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Remote SSH host, e.g. ubuntu@1.2.3.4")
    parser.add_argument("--key", default=DEFAULT_SSH_KEY, help="SSH private key path")
    parser.add_argument(
        "--remote-session-file",
        default=DEFAULT_REMOTE_SESSION_FILE,
        help="Remote path for slate_session.json",
    )
    parser.add_argument("--remote-repo", default=DEFAULT_REMOTE_REPO, help="Remote Hermes repo path")
    parser.add_argument("--remote-python", default=DEFAULT_REMOTE_PYTHON, help="Remote Python path from repo root")
    parser.add_argument(
        "--skip-if-expired",
        action="store_true",
        help="Exit cleanly instead of failing when the local Slate session is missing or expired.",
    )
    args = parser.parse_args(argv)

    try:
        synced = sync_session(
            host=args.host,
            key=args.key,
            remote_session_file=args.remote_session_file,
            remote_repo=args.remote_repo,
            remote_python=args.remote_python,
            skip_if_expired=args.skip_if_expired,
        )
    except subprocess.CalledProcessError as e:
        print(f"Sync failed: remote command exited with {e.returncode}", file=sys.stderr)
        return e.returncode or 1
    except Exception as e:
        print(f"Sync failed: {e}", file=sys.stderr)
        return 1

    if not synced:
        return 0
    print(f"Slate session synced to {args.host}:{args.remote_session_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
