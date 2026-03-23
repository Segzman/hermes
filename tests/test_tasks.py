import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from importlib import reload
from pathlib import Path
from unittest.mock import patch


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        base = datetime(2026, 3, 20, 3, 30, tzinfo=timezone.utc)
        return base.astimezone(tz) if tz else base.replace(tzinfo=None)


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "tasks.db")
        os.environ["TASKS_DB"] = self.db_path

        import bot.tasks as tasks
        import bot.tools as tools

        self.tasks = reload(tasks)
        self.tools = reload(tools)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("TASKS_DB", None)

    def test_add_and_list_tasks_orders_by_priority_then_due(self):
        later = datetime.now(tz=timezone.utc) + timedelta(days=2)
        sooner = datetime.now(tz=timezone.utc) + timedelta(days=1)

        self.tasks.add_task("medium later", due_at=later, priority="medium")
        self.tasks.add_task("high sooner", due_at=sooner, priority="high")
        self.tasks.add_task("low no due", priority="low")

        items = self.tasks.list_tasks()
        self.assertEqual([item.title for item in items], ["high sooner", "medium later", "low no due"])

    def test_complete_and_reopen_task(self):
        task = self.tasks.add_task("finish report")

        done = self.tasks.set_task_status(str(task.id), "done")
        self.assertIsNotNone(done)
        self.assertEqual(done.status, "done")
        self.assertEqual(len(self.tasks.list_tasks(status="open")), 0)
        self.assertEqual(len(self.tasks.list_tasks(status="done")), 1)

        reopened = self.tasks.set_task_status(str(task.id), "open")
        self.assertIsNotNone(reopened)
        self.assertEqual(reopened.status, "open")
        self.assertEqual(len(self.tasks.list_tasks(status="open")), 1)

    def test_delete_task_by_partial_title(self):
        self.tasks.add_task("write discussion post")

        deleted = self.tasks.delete_task("discussion")
        self.assertIsNotNone(deleted)
        self.assertEqual(deleted.title, "write discussion post")
        self.assertEqual(self.tasks.list_tasks(), [])

    def test_add_task_tool_parses_due_string(self):
        fixed_due = datetime(2026, 3, 21, 14, 0, tzinfo=timezone.utc)
        with patch("bot.reminders.parse_when", return_value=fixed_due):
            result = self.tools.add_task("study quiz", due="tomorrow at 10am", priority="high")
        self.assertIn("Task added", result)
        items = self.tasks.list_tasks()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "study quiz")
        self.assertEqual(items[0].priority, "high")
        self.assertEqual(items[0].due_at, fixed_due)

    def test_due_tomorrow_label_uses_toronto_calendar_day(self):
        due_at = datetime(2026, 3, 20, 5, 0, tzinfo=timezone.utc)
        task = self.tasks.add_task("midnight boundary", due_at=due_at, priority="high")

        with patch("bot.tasks.datetime", FrozenDateTime):
            summary = task.summary()

        self.assertIn("Due tomorrow 01:00 AM Toronto", summary)


if __name__ == "__main__":
    unittest.main()
