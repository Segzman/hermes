import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timezone
from importlib import reload
from unittest.mock import patch


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        base = datetime(2026, 3, 20, 15, 0, tzinfo=timezone.utc)
        return base.astimezone(tz) if tz else base.replace(tzinfo=None)


class ReminderParsingTests(unittest.TestCase):
    def setUp(self):
        import bot.reminders as reminders

        self.reminders = reload(reminders)

    def test_parse_when_interprets_explicit_datetime_in_toronto(self):
        parsed = self.reminders.parse_when("2026-03-21 10:00")
        self.assertEqual(parsed, datetime(2026, 3, 21, 14, 0, tzinfo=timezone.utc))

    def test_parse_when_tomorrow_at_time_uses_toronto_clock(self):
        with patch("bot.reminders.datetime", FrozenDateTime):
            parsed = self.reminders.parse_when("tomorrow at 10am")
        self.assertEqual(parsed, datetime(2026, 3, 21, 14, 0, tzinfo=timezone.utc))


class FakeJob:
    def __init__(self, job_id, name, next_run_time):
        self.id = job_id
        self.name = name
        self.next_run_time = next_run_time
        self.removed = False

    def remove(self):
        self.removed = True


class FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def add_job(self, func, trigger, args, id, name, replace_existing, misfire_grace_time):
        self.jobs[id] = FakeJob(id, name, trigger.run_date)

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def get_jobs(self):
        return list(self.jobs.values())


class SlateReminderSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import bot.reminders as reminders

        self.reminders = reload(reminders)
        self.temp_dir = TemporaryDirectory()
        self.reminders.SLATE_REMINDER_STATE = Path(self.temp_dir.name) / "slate_reminders.json"
        self.reminders._scheduler = FakeScheduler()
        self.sent = []

        async def _send(chat_id, text):
            self.sent.append((chat_id, text))

        self.reminders._send_fn = _send

    def tearDown(self):
        self.temp_dir.cleanup()

    def _assignment(self, *, item_id="n2", name="Assignment N2", due_at=None, submitted=False):
        from slate.models import Assignment, Course

        course = Course(id="1462805", name="Web", code="1261_27298", url="https://example.com")
        return Assignment(
            id=item_id,
            name=name,
            course=course,
            due_date=due_at or datetime(2026, 3, 25, 3, 59, tzinfo=timezone.utc),
            instructions="",
            is_submitted=submitted,
        )

    async def test_sync_slate_reminders_adds_future_items(self):
        item = self._assignment()

        await self.reminders.sync_slate_reminders([item], chat_id="123")

        state = self.reminders._load_slate_state()
        key = self.reminders._slate_item_key(item)
        job = self.reminders._scheduler.get_job(self.reminders._slate_job_id(key))
        self.assertIn(key, state)
        self.assertIsNotNone(job)
        self.assertEqual(job.name, "Assignment N2 (1261_27298)")
        self.assertEqual(self.sent, [])

    async def test_sync_slate_reminders_notifies_when_due_date_changes(self):
        item = self._assignment()
        await self.reminders.sync_slate_reminders([item], chat_id="123")

        moved = self._assignment(due_at=datetime(2026, 3, 26, 3, 59, tzinfo=timezone.utc))
        await self.reminders.sync_slate_reminders([moved], chat_id="123")

        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "123")
        self.assertIn("Slate update", self.sent[0][1])
        self.assertIn("Assignment N2", self.sent[0][1])

    async def test_sync_slate_reminders_removes_submitted_items_and_notifies(self):
        item = self._assignment()
        key = self.reminders._slate_item_key(item)
        await self.reminders.sync_slate_reminders([item], chat_id="123")

        await self.reminders.sync_slate_reminders([], chat_id="123")

        state = self.reminders._load_slate_state()
        job = self.reminders._scheduler.get_job(self.reminders._slate_job_id(key))
        self.assertEqual(state, {})
        self.assertTrue(job.removed)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("stopped tracking", self.sent[0][1])


class ReminderCleanupTests(unittest.TestCase):
    def setUp(self):
        import bot.reminders as reminders

        self.reminders = reload(reminders)
        self.temp_dir = TemporaryDirectory()
        self.reminders.SLATE_REMINDER_STATE = Path(self.temp_dir.name) / "slate_reminders.json"
        self.reminders._scheduler = FakeScheduler()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_list_reminders_ignores_slate_jobs(self):
        self.reminders._scheduler.jobs["reminder_test"] = FakeJob(
            "reminder_test",
            "Call mom",
            datetime(2026, 3, 21, 14, 0, tzinfo=timezone.utc),
        )
        self.reminders._scheduler.jobs["slate_test"] = FakeJob(
            "slate_test",
            "Assignment 3",
            datetime(2026, 3, 21, 15, 0, tzinfo=timezone.utc),
        )

        text = self.reminders.list_reminders()

        self.assertIn("Call mom", text)
        self.assertNotIn("Assignment 3", text)

    def test_clear_slate_reminders_removes_jobs_and_state(self):
        state = {
            "1462805:assignment:1071357": {
                "id": "1071357",
                "name": "Assignment 3",
                "course_code": "1261_27298",
                "due_at": "2026-03-22T03:59:00+00:00",
            }
        }
        self.reminders._save_slate_state(state)
        self.reminders._scheduler.jobs["slate_test"] = FakeJob(
            "slate_test",
            "Assignment 3",
            datetime(2026, 3, 21, 15, 0, tzinfo=timezone.utc),
        )

        removed = self.reminders.clear_slate_reminders()

        self.assertEqual(removed, 1)
        self.assertTrue(self.reminders._scheduler.jobs["slate_test"].removed)
        self.assertFalse(self.reminders.SLATE_REMINDER_STATE.exists())


if __name__ == "__main__":
    unittest.main()
