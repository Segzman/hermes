import asyncio
import unittest


class BackgroundJobHeuristicsTests(unittest.TestCase):
    def test_should_background_detects_browser_and_terminal_work(self):
        from bot import jobs

        self.assertTrue(jobs.should_background("open amazon.ca and find me a charger"))
        self.assertTrue(jobs.should_background("run journalctl for hermes-bot.service"))
        self.assertFalse(jobs.should_background("what should I eat tonight"))
        self.assertFalse(jobs.should_background("are u using browser base"))
        self.assertFalse(jobs.should_background("are u using browser use"))

    def test_is_status_query_detects_progress_questions(self):
        from bot import jobs

        self.assertTrue(jobs.is_status_query("how's that going"))
        self.assertTrue(jobs.is_status_query("status update"))
        self.assertFalse(jobs.is_status_query("open example.com"))

    def test_incomplete_result_detection_flags_retry_language(self):
        from bot import jobs

        self.assertTrue(jobs._looks_incomplete_result("Browser session reset. Let me try again."))
        self.assertTrue(jobs._looks_incomplete_result("Browser reset. Trying again — opening Food Basics homepage now."))
        self.assertFalse(jobs._looks_incomplete_result("Found 3 charger options on Amazon.ca."))


class BackgroundJobLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from bot import jobs

        jobs._jobs_by_chat.clear()
        jobs._jobs_by_id.clear()

    async def test_background_job_completion_is_tracked(self):
        from bot import jobs

        sent = []
        noted = []

        async def run_agent(worker_chat_id, status_cb):
            self.assertIn("::job:", worker_chat_id)
            await status_cb("Opening the page")
            return "Finished the task successfully."

        async def send_text(chat_id, text):
            sent.append((chat_id, text))

        job = await jobs.start_background_agent_job(
            chat_id="123",
            prompt="open amazon.ca and find a charger",
            run_agent=run_agent,
            send_text=send_text,
            note_event=lambda chat_id, text: noted.append((chat_id, text)),
        )
        await asyncio.wait_for(job.task, timeout=1)

        self.assertEqual(job.state, "completed")
        self.assertIn("Completed", jobs.job_status_text("123", job.id))
        self.assertIn("Finished the task successfully.", jobs.job_status_text("123", job.id))
        self.assertTrue(any("started" in text.lower() for _, text in noted))
        self.assertTrue(any("completed" in text.lower() for _, text in noted))
        self.assertTrue(any("Started background sub-agent" in text for _, text in sent))
        self.assertTrue(any("finished" in text.lower() for _, text in sent))

    async def test_cancel_background_job_marks_job_cancelled(self):
        from bot import jobs

        gate = asyncio.Event()

        async def run_agent(worker_chat_id, status_cb):
            await status_cb("Working")
            await gate.wait()
            return "unreachable"

        async def send_text(chat_id, text):
            return None

        job = await jobs.start_background_agent_job(
            chat_id="123",
            prompt="open a long-running page",
            run_agent=run_agent,
            send_text=send_text,
        )
        await asyncio.sleep(0)

        cancel_text = jobs.cancel_job("123", job.id)
        self.assertIn("Cancellation requested", cancel_text)
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(job.task, timeout=1)
        self.assertEqual(job.state, "cancelled")

    async def test_partial_result_is_retried_before_completion(self):
        from bot import jobs

        sent = []
        attempts = {"count": 0}

        async def run_agent(worker_chat_id, status_cb):
            attempts["count"] += 1
            if attempts["count"] == 1:
                return "Browser session reset. Let me try again."
            await status_cb("Found the results")
            return "Found 3 charger options on Amazon.ca."

        async def send_text(chat_id, text):
            sent.append(text)

        job = await jobs.start_background_agent_job(
            chat_id="123",
            prompt="open amazon.ca and find a charger",
            run_agent=run_agent,
            send_text=send_text,
        )
        await asyncio.wait_for(job.task, timeout=1)

        self.assertEqual(attempts["count"], 2)
        self.assertEqual(job.state, "completed")
        self.assertTrue(any("partial step" in text.lower() for text in sent))


if __name__ == "__main__":
    unittest.main()
