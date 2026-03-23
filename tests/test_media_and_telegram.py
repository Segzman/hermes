import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class SlateSubmissionParsingTests(unittest.TestCase):
    def test_submission_records_show_user_submission(self):
        from slate.client import _submission_records_show_user_submission

        data = [
            {
                "Entity": {"EntityId": 123},
                "Submissions": [{"Id": 1}],
            }
        ]
        self.assertTrue(_submission_records_show_user_submission(data, 123))
        self.assertFalse(_submission_records_show_user_submission(data, 999))

    def test_discussion_posts_show_user_submission(self):
        from slate.client import _discussion_posts_show_user_submission

        data = [
            {"PostingUserId": 123, "IsDeleted": False},
            {"PostingUserId": 456, "IsDeleted": False},
        ]
        self.assertTrue(_discussion_posts_show_user_submission(data, 123))
        self.assertFalse(_discussion_posts_show_user_submission(data, 999))


class TelegramMediaInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_agent_input_from_voice_note(self):
        from bot.message_input import build_agent_input

        message = SimpleNamespace(
            text=None,
            caption="turn this into a reminder",
            voice=SimpleNamespace(file_id="voice123", file_name=None),
            audio=None,
            photo=None,
            document=None,
        )
        bot = SimpleNamespace()

        with patch("bot.message_input.download_telegram_file", AsyncMock()) as download_mock, \
             patch("bot.message_input.media.transcribe_voice", AsyncMock(return_value="submit assignment tomorrow")):
            tmp = tempfile.NamedTemporaryFile(suffix=".ogg", delete=False)
            tmp.close()
            download_mock.return_value = Path(tmp.name)
            text = await build_agent_input(message, bot)

        self.assertIn("Voice note transcript:", text)
        self.assertIn("submit assignment tomorrow", text)

    async def test_build_agent_input_from_photo(self):
        from bot.message_input import build_agent_input

        message = SimpleNamespace(
            text=None,
            caption="what does this screen mean?",
            voice=None,
            audio=None,
            photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="large")],
            document=None,
        )
        bot = SimpleNamespace()

        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.close()
        with patch("bot.message_input.download_telegram_file", AsyncMock(return_value=Path(tmp.name))), \
             patch("bot.message_input.media.describe_image", AsyncMock(return_value="The screen shows a 403 Forbidden error.")):
            text = await build_agent_input(message, bot)

        self.assertIn("Image context:", text)
        self.assertIn("403 Forbidden", text)


class TelegramBackgroundDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_message_starts_background_job_for_long_task(self):
        try:
            from bot import telegram_bot
        except ModuleNotFoundError as exc:
            self.skipTest(f"telegram dependency unavailable locally: {exc}")

        message = SimpleNamespace(
            text="open amazon.ca and find me a charger",
            caption=None,
            voice=None,
            audio=None,
            photo=None,
            document=None,
            reply_text=AsyncMock(),
            chat=SimpleNamespace(send_action=AsyncMock()),
        )
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=123), message=message)
        ctx = SimpleNamespace(bot=SimpleNamespace())

        with patch("bot.telegram_bot._allowed", return_value=True), \
             patch("bot.telegram_bot.build_agent_input", AsyncMock(return_value="open amazon.ca and find me a charger")), \
             patch("bot.telegram_bot.jobs.start_background_agent_job", AsyncMock()) as start_mock, \
             patch("bot.telegram_bot.agent.chat", AsyncMock()) as chat_mock:
            await telegram_bot.handle_message(update, ctx)

        start_mock.assert_awaited_once()
        chat_mock.assert_not_awaited()
        message.chat.send_action.assert_not_awaited()

    async def test_handle_message_status_query_reads_active_job_status_directly(self):
        try:
            from bot import telegram_bot
        except ModuleNotFoundError as exc:
            self.skipTest(f"telegram dependency unavailable locally: {exc}")

        message = SimpleNamespace(
            text="Progress",
            caption=None,
            voice=None,
            audio=None,
            photo=None,
            document=None,
            reply_text=AsyncMock(),
            chat=SimpleNamespace(send_action=AsyncMock()),
        )
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=123), message=message)
        ctx = SimpleNamespace(bot=SimpleNamespace())

        with patch("bot.telegram_bot._allowed", return_value=True), \
             patch("bot.telegram_bot.build_agent_input", AsyncMock(return_value="Progress")), \
             patch("bot.telegram_bot.jobs.has_active_jobs", return_value=True), \
             patch("bot.telegram_bot.jobs.job_status_text", return_value="Job `abcd1234`\nState: running"), \
             patch("bot.telegram_bot.jobs.start_background_agent_job", AsyncMock()) as start_mock, \
             patch("bot.telegram_bot.agent.chat", AsyncMock()) as chat_mock:
            await telegram_bot.handle_message(update, ctx)

        start_mock.assert_not_awaited()
        chat_mock.assert_not_awaited()
        message.chat.send_action.assert_not_awaited()
        message.reply_text.assert_awaited()


if __name__ == "__main__":
    unittest.main()
