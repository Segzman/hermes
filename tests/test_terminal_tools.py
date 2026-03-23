import tempfile
import unittest
from pathlib import Path


class TerminalModuleTests(unittest.TestCase):
    def test_run_command_executes_and_returns_output(self):
        from bot import terminal

        result = terminal.run_command("printf hello", timeout_seconds=5)

        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["timed_out"])
        self.assertEqual(result["output"], "hello")

    def test_resolve_cwd_rejects_missing_directory(self):
        from bot import terminal

        with self.assertRaisesRegex(RuntimeError, "does not exist"):
            terminal._resolve_cwd("/definitely/missing/hermes-path")

    def test_resolve_cwd_accepts_relative_path_under_default(self):
        from bot import terminal

        with tempfile.TemporaryDirectory() as tmp:
            original_default = terminal.TERMINAL_DEFAULT_CWD
            try:
                terminal.TERMINAL_DEFAULT_CWD = Path(tmp)
                subdir = Path(tmp) / "work"
                subdir.mkdir()
                resolved = terminal._resolve_cwd("work")
            finally:
                terminal.TERMINAL_DEFAULT_CWD = original_default

        self.assertEqual(resolved, subdir.resolve())

    def test_validate_service_name_rejects_bad_input(self):
        from bot import terminal

        with self.assertRaisesRegex(RuntimeError, "Invalid service name"):
            terminal._validate_service_name("bad; rm -rf /")


if __name__ == "__main__":
    unittest.main()
