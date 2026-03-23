import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch


class SlateFilterTests(unittest.TestCase):
    def test_relevant_course_filter_excludes_vc_and_surveys(self):
        from slate.client import _is_relevant_course

        self.assertFalse(_is_relevant_course("1472949_VC", "Capstone-Thesis Proposal Seminar W2026"))
        self.assertFalse(_is_relevant_course("12345", "Take the Program Review Survey Now!"))
        self.assertTrue(_is_relevant_course("1261_27298", "PROG27545 Web Application Design & Implementation"))

    def test_deliverable_filter_drops_old_overdue_and_no_deadline_items(self):
        from bot.tools import _filter_deliverables
        from slate.models import Assignment, Course

        course = Course(id="1", name="Web", code="1261_27298", url="https://example.com")
        vc_course = Course(id="2", name="Virtual Community", code="1472949_VC", url="https://example.com")
        now = datetime.now(tz=timezone.utc)
        items = [
            Assignment(id="old", name="old overdue", course=course, due_date=now - timedelta(days=20), instructions=""),
            Assignment(id="soon", name="due soon", course=course, due_date=now + timedelta(days=2), instructions=""),
            Assignment(id="none", name="no deadline", course=course, due_date=None, instructions=""),
            Assignment(id="vc", name="vc item", course=vc_course, due_date=now + timedelta(days=1), instructions=""),
        ]

        filtered = _filter_deliverables(items)
        self.assertEqual([item.id for item in filtered], ["soon"])

    def test_merge_calendar_backfills_due_dates_and_adds_missing_items(self):
        from bot.tools import _merge_calendar
        from slate.models import Assignment, Course

        course = Course(id="1462805", name="Web", code="1261_27298", url="https://example.com")
        existing = Assignment(
            id="1071357",
            name="Assignment 3",
            course=course,
            due_date=None,
            instructions="",
        )

        data = {
            "courses": [course],
            "assignments": [existing],
            "quizzes": [],
            "discussions": [],
            "calendar_events": [
                {
                    "Title": "Assignment 3",
                    "AssociatedOrgUnitId": "1462805",
                    "EndDateTime": "2026-03-21T03:59:00.000Z",
                    "AssociatedEntity": {
                        "AssociatedEntityType": "D2L.LE.Dropbox.Dropbox",
                        "AssociatedEntityId": "1071357",
                    },
                },
                {
                    "Title": "Activity 16 - A basic chatbot",
                    "AssociatedOrgUnitId": "1462805",
                    "EndDateTime": "2026-03-20T03:59:00.000Z",
                    "AssociatedEntity": {
                        "AssociatedEntityType": "D2L.LE.Dropbox.Dropbox",
                        "AssociatedEntityId": "1072440",
                    },
                },
            ],
        }

        merged = _merge_calendar(data)
        self.assertEqual(len(merged), 2)
        self.assertIsNotNone(existing.due_date)
        self.assertEqual({item.id for item in merged}, {"1071357", "1072440"})


class SlateSubmissionResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_everything_marks_assignment_submitted_even_without_direct_due_date(self):
        from slate.client import SlateClient
        from slate.models import Assignment, Course

        course = Course(id="1462912", name="Mobile", code="1261_27274", url="https://example.com")
        assignment = Assignment(
            id="1048236",
            name="Assignment N2",
            course=course,
            due_date=None,
            instructions="",
        )

        with patch("slate.client.SESSION_FILE") as session_file:
            session_file.exists.return_value = True
            client = SlateClient()

        client.get_courses = AsyncMock(return_value=[course])
        client.get_assignments = AsyncMock(return_value=[assignment])
        client.get_quizzes = AsyncMock(return_value=[])
        client.get_discussions = AsyncMock(return_value=[])
        client.get_announcements = AsyncMock(return_value=[])
        client.get_grade_updates = AsyncMock(return_value=[])
        client.get_calendar_events = AsyncMock(return_value=[
            {
                "Title": "Assignment N2",
                "AssociatedOrgUnitId": "1462912",
                "EndDateTime": "2026-03-19T03:59:00.000Z",
                "AssociatedEntity": {
                    "AssociatedEntityType": "D2L.LE.Dropbox.Dropbox",
                    "AssociatedEntityId": "1048236",
                },
            }
        ])
        client.get_messages = AsyncMock(return_value=[])
        client._assignment_has_submission = AsyncMock(return_value=True)
        client._discussion_has_submission = AsyncMock(return_value=False)

        data = await client.get_everything()

        self.assertTrue(data["assignments"][0].is_submitted)
        client._assignment_has_submission.assert_awaited_once_with("1462912", "1048236")


if __name__ == "__main__":
    unittest.main()
