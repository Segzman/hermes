import unittest
from datetime import datetime, timezone
from importlib import reload
from unittest.mock import patch


class ApplePayloadTests(unittest.TestCase):
    def test_make_vtodo_includes_priority_location_and_people(self):
        from bot.apple import _make_vtodo

        payload = _make_vtodo(
            title="Renew passport",
            due=datetime(2026, 3, 22, 14, 0, tzinfo=timezone.utc),
            notes="Bring forms",
            priority="high",
            urgent=True,
            location="Service Canada",
            people=["alice@example.com", "Mom"],
        )

        self.assertIn("SUMMARY:Renew passport", payload)
        self.assertIn("PRIORITY:1", payload)
        self.assertIn("LOCATION:Service Canada", payload)
        self.assertIn("ATTENDEE:mailto:alice@example.com", payload)
        self.assertIn("People: Mom", payload)


class AppleCalendarSelectionTests(unittest.TestCase):
    def test_find_calendar_requires_exact_preferred_match(self):
        from bot.apple import _find_calendar

        class FakeCalendar:
            def __init__(self, name, supported):
                self.name = name
                self._supported = supported

            def get_supported_components(self):
                return self._supported

        class FakePrincipal:
            def calendars(self):
                return [FakeCalendar("Reminders ⚠️", ["VTODO"])]

        with self.assertRaisesRegex(RuntimeError, 'Hermes'):
            _find_calendar(FakePrincipal(), "VTODO", "Hermes")


class AppleToolTests(unittest.TestCase):
    def setUp(self):
        import bot.tools as tools

        self.tools = reload(tools)

    def test_set_apple_reminder_passes_rich_fields(self):
        due_at = datetime(2026, 3, 22, 14, 0, tzinfo=timezone.utc)
        with patch("bot.reminders.parse_when", return_value=due_at), \
             patch("bot.apple.create_rich_reminder", return_value={"calendar_name": "Reminders", "subtasks_created": 2}) as create_mock:
            result = self.tools.set_apple_reminder(
                title="Renew passport",
                when="tomorrow at 10am",
                priority="high",
                urgent=True,
                location="Service Canada",
                people=["alice@example.com"],
                subtasks=["Photo", "Forms"],
            )

        create_mock.assert_called_once()
        kwargs = create_mock.call_args.kwargs
        self.assertEqual(kwargs["title"], "Renew passport")
        self.assertEqual(kwargs["due"], due_at)
        self.assertEqual(kwargs["priority"], "high")
        self.assertTrue(kwargs["urgent"])
        self.assertEqual(kwargs["location"], "Service Canada")
        self.assertEqual(kwargs["people"], ["alice@example.com"])
        self.assertEqual(kwargs["subtasks"], ["Photo", "Forms"])
        self.assertIn("Added to Apple Reminders", result)
        self.assertIn("subtasks=2", result)

    def test_add_apple_calendar_event_formats_success(self):
        start_at = datetime(2026, 3, 22, 19, 0, tzinfo=timezone.utc)
        end_at = datetime(2026, 3, 22, 20, 0, tzinfo=timezone.utc)
        with patch("bot.reminders.parse_when", side_effect=[start_at, end_at]), \
             patch("bot.apple.create_calendar_event", return_value={"calendar_name": "Personal", "uid": "event-1234567890"}):
            result = self.tools.add_apple_calendar_event(
                title="Dentist",
                start="tomorrow at 3pm",
                end="tomorrow at 4pm",
            )
        self.assertIn("Added to Apple Calendar", result)
        self.assertIn("Dentist", result)
        self.assertIn("Toronto", result)

    def test_list_apple_calendar_events_formats_upcoming_items(self):
        from bot.apple import AppleCalendarEvent

        items = [
            AppleCalendarEvent(
                title="Dentist",
                start_at=datetime(2026, 3, 22, 19, 0, tzinfo=timezone.utc),
                end_at=datetime(2026, 3, 22, 20, 0, tzinfo=timezone.utc),
                calendar_name="Personal",
                location="Downtown",
            )
        ]
        with patch("bot.apple.list_upcoming_calendar_events", return_value=items):
            result = self.tools.list_apple_calendar_events(days=7)
        self.assertIn("Apple Calendar", result)
        self.assertIn("Dentist", result)
        self.assertIn("Downtown", result)


if __name__ == "__main__":
    unittest.main()
