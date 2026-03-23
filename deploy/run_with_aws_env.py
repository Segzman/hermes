#!/usr/bin/env python3
"""
Load local .env plus optional AWS SSM / Secrets Manager values, then run a module.

Usage:
  python deploy/run_with_aws_env.py bot.telegram_bot
  python deploy/run_with_aws_env.py slate.checker --watch
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

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = ROOT_DIR / ".env"
ENV_NAME_RE = re.compile(r"[^A-Z0-9_]")


def _sanitize_env_name(name: str) -> str:
    cleaned = name.strip().replace("/", "__").replace("-", "_").replace(".", "_")
    cleaned = ENV_NAME_RE.sub("_", cleaned.upper())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        raise RuntimeError(f"Could not derive an environment variable name from `{name}`.")
    return cleaned


def _ssm_path_to_env_name(prefix: str, full_name: str) -> str:
    relative = full_name
    if prefix and full_name.startswith(prefix):
        relative = full_name[len(prefix):]
    relative = relative.lstrip("/")
    if not relative:
        relative = Path(full_name).name
    return _sanitize_env_name(relative)


def _load_ssm_values(session: boto3.Session, path_prefix: str) -> dict[str, str]:
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
    raw = (secret_string or "").strip()
    if not raw:
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        return {_sanitize_env_name(str(k)): str(v) for k, v in parsed.items()}

    dotenv_map = dotenv_values(stream=StringIO(secret_string))
    dotenv_map = {str(k): str(v) for k, v in dotenv_map.items() if v is not None}
    if dotenv_map:
        return {_sanitize_env_name(k): v for k, v in dotenv_map.items()}

    env_name = os.getenv("HERMES_AWS_SECRET_ENV_NAME", "").strip()
    if not env_name:
        raise RuntimeError(
            "Secrets Manager secret must be a JSON object, dotenv-style KEY=value content, "
            "or set HERMES_AWS_SECRET_ENV_NAME for a single raw secret."
        )
    return {_sanitize_env_name(env_name): secret_string}


def _load_secrets_manager_values(session: boto3.Session, secret_id: str) -> dict[str, str]:
    secret_ref = (secret_id or "").strip()
    if not secret_ref:
        return {}

    client = session.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_ref)
    if "SecretString" in response:
        return _parse_secret_string(response["SecretString"])
    raise RuntimeError("Binary Secrets Manager secrets are not supported in this loader.")


def load_aws_env() -> dict[str, str]:
    region = (
        os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or os.getenv("HERMES_AWS_REGION")
        or ""
    ).strip() or None
    ssm_path = os.getenv("HERMES_AWS_SSM_PATH", "").strip()
    secret_id = os.getenv("HERMES_AWS_SECRET_ID", "").strip()

    if not ssm_path and not secret_id:
        return {}

    session = boto3.Session(region_name=region)
    loaded: dict[str, str] = {}
    try:
        loaded.update(_load_ssm_values(session, ssm_path))
        loaded.update(_load_secrets_manager_values(session, secret_id))
    except (BotoCoreError, ClientError) as exc:
        raise RuntimeError(f"Failed to load AWS secrets: {exc}") from exc

    os.environ.update(loaded)
    return loaded


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(ROOT_DIR))
    load_dotenv(DEFAULT_ENV_FILE)

    parser = argparse.ArgumentParser()
    parser.add_argument("module", help="Python module to run")
    parser.add_argument("module_args", nargs=argparse.REMAINDER, help="Arguments forwarded to the module")
    args = parser.parse_args(argv)

    if args.module_args and args.module_args[0] == "--":
        module_args = args.module_args[1:]
    else:
        module_args = args.module_args

    loaded = load_aws_env()
    if loaded:
        print(f"Loaded {len(loaded)} AWS-backed environment values.", flush=True)

    sys.argv = [args.module, *module_args]
    runpy.run_module(args.module, run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
