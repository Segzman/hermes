import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parent.parent / "deploy" / "run_with_aws_env.py"
SPEC = importlib.util.spec_from_file_location("run_with_aws_env", MODULE_PATH)
aws_env = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(aws_env)


class FakePaginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kwargs):
        return self.pages


class FakeSSMClient:
    def __init__(self, pages):
        self.pages = pages

    def get_paginator(self, name):
        assert name == "get_parameters_by_path"
        return FakePaginator(self.pages)


class FakeSecretsClient:
    def __init__(self, payload):
        self.payload = payload

    def get_secret_value(self, SecretId):
        return self.payload


class FakeSession:
    def __init__(self, ssm_pages=None, secret_payload=None):
        self.ssm_pages = ssm_pages or []
        self.secret_payload = secret_payload or {}

    def client(self, name):
        if name == "ssm":
            return FakeSSMClient(self.ssm_pages)
        if name == "secretsmanager":
            return FakeSecretsClient(self.secret_payload)
        raise AssertionError(name)


class AwsEnvTests(unittest.TestCase):
    def test_sanitize_env_name(self):
        self.assertEqual(aws_env._sanitize_env_name("foo/bar.baz-key"), "FOO_BAR_BAZ_KEY")

    def test_parse_secret_string_json(self):
        values = aws_env._parse_secret_string('{"telegram_bot_token":"abc","chat-id":"123"}')
        self.assertEqual(values["TELEGRAM_BOT_TOKEN"], "abc")
        self.assertEqual(values["CHAT_ID"], "123")

    def test_parse_secret_string_dotenv(self):
        values = aws_env._parse_secret_string("OPENROUTER_API_KEY=abc\nTELEGRAM_CHAT_ID=123\n")
        self.assertEqual(values["OPENROUTER_API_KEY"], "abc")
        self.assertEqual(values["TELEGRAM_CHAT_ID"], "123")

    def test_load_ssm_values_uses_relative_names(self):
        session = FakeSession(
            ssm_pages=[
                {
                    "Parameters": [
                        {"Name": "/hermes/prod/TELEGRAM_BOT_TOKEN", "Value": "bot-token"},
                        {"Name": "/hermes/prod/browser/password", "Value": "pw"},
                    ]
                }
            ]
        )
        values = aws_env._load_ssm_values(session, "/hermes/prod")
        self.assertEqual(values["TELEGRAM_BOT_TOKEN"], "bot-token")
        self.assertEqual(values["BROWSER_PASSWORD"], "pw")

    def test_load_aws_env_merges_ssm_and_secret_manager(self):
        fake_session = FakeSession(
            ssm_pages=[{"Parameters": [{"Name": "/hermes/prod/TELEGRAM_BOT_TOKEN", "Value": "bot-token"}]}],
            secret_payload={"SecretString": '{"OPENROUTER_API_KEY":"or-key"}'},
        )
        with patch.dict(
            os.environ,
            {
                "AWS_REGION": "us-east-1",
                "HERMES_AWS_SSM_PATH": "/hermes/prod",
                "HERMES_AWS_SECRET_ID": "hermes/prod/shared",
            },
            clear=False,
        ), patch.object(aws_env.boto3, "Session", return_value=fake_session):
            loaded = aws_env.load_aws_env()
            self.assertEqual(loaded["TELEGRAM_BOT_TOKEN"], "bot-token")
            self.assertEqual(loaded["OPENROUTER_API_KEY"], "or-key")
            self.assertEqual(os.environ["TELEGRAM_BOT_TOKEN"], "bot-token")
            self.assertEqual(os.environ["OPENROUTER_API_KEY"], "or-key")

    def test_main_loads_env_file_then_runs_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("HERMES_AWS_SSM_PATH=\n", encoding="utf-8")
            with patch.object(aws_env, "DEFAULT_ENV_FILE", env_file), \
                 patch.object(aws_env, "load_aws_env", return_value={}), \
                 patch.object(aws_env.runpy, "run_module") as run_module_mock:
                rc = aws_env.main(["bot.telegram_bot"])

        self.assertEqual(rc, 0)
        run_module_mock.assert_called_once_with("bot.telegram_bot", run_name="__main__")


if __name__ == "__main__":
    unittest.main()
