#!/usr/bin/env python3
"""
Load local .env plus optional AWS SSM / Secrets Manager values, then run a module.

Usage:
  python deploy/run_with_aws_env.py bot.telegram_bot
  python deploy/run_with_aws_env.py slate.checker --watch

Architecture:
  This script is the entry point for running Hermes modules on EC2. It
  bridges local development (.env files) with production secrets management
  (AWS SSM Parameter Store and Secrets Manager).

  Loading order:
    1. Local .env file (project root) — loaded first via python-dotenv
    2. AWS SSM Parameter Store — if HERMES_AWS_SSM_PATH is set, all parameters
       under that path prefix are loaded as environment variables
    3. AWS Secrets Manager — if HERMES_AWS_SECRET_ID is set, the secret is
       fetched and parsed into environment variables

  Secret formats supported:
    - JSON object: {"KEY": "value", ...} — each key becomes an env var
    - dotenv format: KEY=value lines — parsed via python-dotenv
    - Raw string: stored under the name from HERMES_AWS_SECRET_ENV_NAME

  After loading all environment values, the specified Python module is
  executed via runpy.run_module (equivalent to `python -m <module>`).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import runpy
import sys
from io import StringIO
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import dotenv_values, load_dotenv

# Project root directory (one level up from deploy/)
ROOT_DIR = Path(__file__).resolve().parent.parent

# Default .env file at the project root
DEFAULT_ENV_FILE = ROOT_DIR / ".env"

# Regex for characters that are NOT valid in environment variable names.
# Used by _sanitize_env_name to strip invalid characters.
ENV_NAME_RE = re.compile(r"[^A-Z0-9_]")


def _sanitize_env_name(name: str) -> str:
    """
    Convert an arbitrary string into a valid environment variable name.

    Transformation rules:
      - Slashes become double underscores (for SSM path segments)
      - Hyphens and dots become single underscores
      - Everything is uppercased
      - Non-alphanumeric characters (except underscore) are replaced
      - Consecutive underscores are collapsed
      - Leading/trailing underscores are stripped

    Example: "/hermes/prod/db-host" -> "HERMES__PROD__DB_HOST"
    """
    cleaned = name.strip().replace("/", "__").replace("-", "_").replace(".", "_")
    cleaned = ENV_NAME_RE.sub("_", cleaned.upper())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        raise RuntimeError(f"Could not derive an environment variable name from `{name}`.")
    return cleaned


def _ssm_path_to_env_name(prefix: str, full_name: str) -> str:
    """
    Convert an SSM parameter path to an environment variable name by
    stripping the common prefix and sanitizing the remainder.

    Example: prefix="/hermes/prod/", full_name="/hermes/prod/DB_HOST" -> "DB_HOST"
    """
    relative = full_name
    if prefix and full_name.startswith(prefix):
        relative = full_name[len(prefix):]
    relative = relative.lstrip("/")
    if not relative:
        # Edge case: the parameter name equals the prefix itself
        relative = Path(full_name).name
    return _sanitize_env_name(relative)


def _load_ssm_values(session: boto3.Session, path_prefix: str) -> dict[str, str]:
    """
    Load all SSM Parameter Store values under a given path prefix.

    Uses a paginator to handle parameter stores with many values.
    WithDecryption=True ensures SecureString parameters are returned decrypted.
    Returns a dict of {sanitized_env_name: value}.
    """
    prefix = (path_prefix or "").strip()
    if not prefix:
        return {}

    client = session.client("ssm")
    paginator = client.get_paginator("get_parameters_by_path")
    result: dict[str, str] = {}
    for page in paginator.paginate(Path=prefix, Recursive=True, WithDecryption=True):
        for item in page.get("Parameters", []):
            name = item["Name"]
            value = item["Value"]
            result[_ssm_path_to_env_name(prefix, name)] = value
    return result


def _parse_secret_string(secret_string: str) -> dict[str, str]:
    """
    Parse a Secrets Manager secret string into environment variable pairs.

    Tries three formats in order:
      1. JSON object: {"KEY": "value"} — most common for multi-value secrets
      2. dotenv format: KEY=value lines — convenient for migrating from .env files
      3. Raw string: entire value stored under HERMES_AWS_SECRET_ENV_NAME —
         used for single opaque secrets like API keys

    Raises RuntimeError if the format cannot be determined and
    HERMES_AWS_SECRET_ENV_NAME is not set.
    """
    raw = (secret_string or "").strip()
    if not raw:
        return {}

    # Attempt 1: Parse as JSON object
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        return {_sanitize_env_name(str(k)): str(v) for k, v in parsed.items()}

    # Attempt 2: Parse as dotenv KEY=value format
    dotenv_map = dotenv_values(stream=StringIO(secret_string))
    dotenv_map = {str(k): str(v) for k, v in dotenv_map.items() if v is not None}
    if dotenv_map:
        return {_sanitize_env_name(k): v for k, v in dotenv_map.items()}

    # Attempt 3: Treat as a single raw secret value
    env_name = os.getenv("HERMES_AWS_SECRET_ENV_NAME", "").strip()
    if not env_name:
        raise RuntimeError(
            "Secrets Manager secret must be a JSON object, dotenv-style KEY=value content, "
            "or set HERMES_AWS_SECRET_ENV_NAME for a single raw secret."
        )
    return {_sanitize_env_name(env_name): secret_string}


def _load_secrets_manager_values(session: boto3.Session, secret_id: str) -> dict[str, str]:
    """
    Fetch and parse a secret from AWS Secrets Manager.

    Only string secrets are supported (not binary). The secret value is
    parsed into environment variable pairs via _parse_secret_string.
    """
    secret_ref = (secret_id or "").strip()
    if not secret_ref:
        return {}

    client = session.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_ref)
    if "SecretString" in response:
        return _parse_secret_string(response["SecretString"])
    raise RuntimeError("Binary Secrets Manager secrets are not supported in this loader.")


def load_aws_env() -> dict[str, str]:
    """
    Load environment variables from AWS SSM and/or Secrets Manager.

    Configuration is driven by these environment variables:
      - AWS_REGION / AWS_DEFAULT_REGION / HERMES_AWS_REGION — AWS region
      - HERMES_AWS_SSM_PATH — SSM Parameter Store path prefix to load
      - HERMES_AWS_SECRET_ID — Secrets Manager secret ARN or name

    If neither SSM nor Secrets Manager is configured, returns an empty dict.
    Loaded values are injected into os.environ for use by downstream modules.
    """
    # Determine AWS region from multiple possible env vars (in priority order)
    region = (
        os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or os.getenv("HERMES_AWS_REGION")
        or ""
    ).strip() or None
    ssm_path = os.getenv("HERMES_AWS_SSM_PATH", "").strip()
    secret_id = os.getenv("HERMES_AWS_SECRET_ID", "").strip()

    # Early return if no AWS secret sources are configured
    if not ssm_path and not secret_id:
        return {}

    session = boto3.Session(region_name=region)
    loaded: dict[str, str] = {}
    try:
        # Load from both sources — Secrets Manager values override SSM if keys overlap
        loaded.update(_load_ssm_values(session, ssm_path))
        loaded.update(_load_secrets_manager_values(session, secret_id))
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"Failed to load AWS secrets: {exc}") from exc

    # Inject loaded values into the process environment
    os.environ.update(loaded)
    return loaded


def main(argv: list[str] | None = None) -> int:
    """
    Main entry point: load .env + AWS secrets, then run the specified module.

    The module is run via runpy.run_module with run_name="__main__", which
    is equivalent to `python -m <module>`. Any extra arguments after the
    module name are forwarded to the module's sys.argv.
    """
    # Ensure the project root is on the Python path
    sys.path.insert(0, str(ROOT_DIR))
    # Load the local .env file first (AWS env vars may reference these)
    load_dotenv(DEFAULT_ENV_FILE)

    parser = argparse.ArgumentParser()
    parser.add_argument("module", help="Python module to run")
    parser.add_argument("module_args", nargs=argparse.REMAINDER, help="Arguments forwarded to the module")
    args = parser.parse_args(argv)

    # Strip the optional "--" separator between this script's args and the module's args
    if args.module_args and args.module_args[0] == "--":
        module_args = args.module_args[1:]
    else:
        module_args = args.module_args

    # Load AWS-backed environment variables (SSM + Secrets Manager)
    loaded = load_aws_env()
    if loaded:
        print(f"Loaded {len(loaded)} AWS-backed environment values.", flush=True)

    # Set sys.argv to what the target module expects and run it
    sys.argv = [args.module, *module_args]
    runpy.run_module(args.module, run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
