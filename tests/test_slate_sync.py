import unittest
from unittest.mock import patch


def _consume_coro(_coro):
    _coro.close()
    return True


def _consume_coro_false(_coro):
    _coro.close()
    return False


class SlateSyncTests(unittest.TestCase):
    def test_remote_verify_command_uses_session_file(self):
        from slate.sync import _remote_verify_command

        cmd = _remote_verify_command("~/hermes", "./.venv/bin/python", "~/.hermes/slate_session.json")
        self.assertIn("cd '~/hermes'", cmd)
        self.assertIn("SLATE_SESSION_FILE='~/.hermes/slate_session.json'", cmd)
        self.assertIn("./.venv/bin/python -m slate.auth --check", cmd)

    @patch("slate.sync._run")
    @patch("slate.sync.asyncio.run", side_effect=_consume_coro)
    @patch("slate.sync.SESSION_FILE")
    def test_sync_session_runs_mkdir_copy_and_verify(self, session_file, _asyncio_run, run_cmd):
        from slate.sync import sync_session

        session_file.exists.return_value = True
        session_file.__str__.return_value = "/tmp/local-session.json"

        sync_session(
            host="ubuntu@example.com",
            key="~/.ssh/key.pem",
            remote_session_file="~/.hermes/slate_session.json",
            remote_repo="~/hermes",
            remote_python="./.venv/bin/python",
        )

        self.assertEqual(run_cmd.call_count, 3)
        mkdir_cmd = run_cmd.call_args_list[0].args[0]
        copy_cmd = run_cmd.call_args_list[1].args[0]
        verify_cmd = run_cmd.call_args_list[2].args[0]

        self.assertEqual(mkdir_cmd[0], "ssh")
        self.assertIn("ubuntu@example.com", mkdir_cmd)
        self.assertIn("mkdir -p '~/.hermes'", mkdir_cmd[-1])
        self.assertEqual(copy_cmd[0], "scp")
        self.assertIn("ubuntu@example.com:~/.hermes/slate_session.json", copy_cmd)
        self.assertEqual(copy_cmd[-1], "ubuntu@example.com:~/.hermes/slate_session.json")
        self.assertEqual(verify_cmd[0], "ssh")
        self.assertIn("ubuntu@example.com", verify_cmd)
        self.assertIn("SLATE_SESSION_FILE='~/.hermes/slate_session.json' ./.venv/bin/python -m slate.auth --check", verify_cmd[-1])

    @patch("slate.sync.asyncio.run", side_effect=_consume_coro_false)
    @patch("slate.sync.SESSION_FILE")
    def test_sync_session_skips_cleanly_when_expired(self, session_file, _asyncio_run):
        from slate.sync import sync_session

        session_file.exists.return_value = True

        with patch("slate.sync._run") as run_cmd:
            synced = sync_session(
                host="ubuntu@example.com",
                key="~/.ssh/key.pem",
                skip_if_expired=True,
            )

        self.assertFalse(synced)
        run_cmd.assert_not_called()


if __name__ == "__main__":
    unittest.main()
