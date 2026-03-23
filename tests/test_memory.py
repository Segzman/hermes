import tempfile
import unittest
from pathlib import Path


class MemoryModuleTests(unittest.TestCase):
    def test_save_and_recall_with_tags_and_description(self):
        from bot import memory

        with tempfile.TemporaryDirectory() as tmp:
            original_dir = memory.MEMORY_DIR
            original_index = memory.MEMORY_INDEX
            try:
                memory.MEMORY_DIR = Path(tmp)
                memory.MEMORY_INDEX = memory.MEMORY_DIR / "MEMORY.md"
                save_result = memory.save(
                    name="Coffee order",
                    content="Large iced coffee with oat milk.",
                    memory_type="user",
                    description="Preferred coffee order",
                    tags=["food", "morning"],
                )
                recall_result = memory.recall("coffee")
            finally:
                memory.MEMORY_DIR = original_dir
                memory.MEMORY_INDEX = original_index

        self.assertIn("Memory saved", save_result)
        self.assertIn("Coffee order", recall_result)
        self.assertIn("food", recall_result)

    def test_list_all_filters_by_type(self):
        from bot import memory

        with tempfile.TemporaryDirectory() as tmp:
            original_dir = memory.MEMORY_DIR
            original_index = memory.MEMORY_INDEX
            try:
                memory.MEMORY_DIR = Path(tmp)
                memory.MEMORY_INDEX = memory.MEMORY_DIR / "MEMORY.md"
                memory.save("Gym days", "Mon Wed Fri", memory_type="routine")
                memory.save("API host", "prod.example.com", memory_type="project")
                result = memory.list_all(memory_type="routine")
            finally:
                memory.MEMORY_DIR = original_dir
                memory.MEMORY_INDEX = original_index

        self.assertIn("Gym days", result)
        self.assertNotIn("API host", result)

    def test_prompt_index_prioritizes_memories(self):
        from bot import memory

        with tempfile.TemporaryDirectory() as tmp:
            original_dir = memory.MEMORY_DIR
            original_index = memory.MEMORY_INDEX
            try:
                memory.MEMORY_DIR = Path(tmp)
                memory.MEMORY_INDEX = memory.MEMORY_DIR / "MEMORY.md"
                memory.save("Tone", "Be concise", memory_type="feedback")
                memory.save("Friend", "Ava likes tea", memory_type="contact", tags=["friend"])
                prompt = memory.get_index_for_prompt()
            finally:
                memory.MEMORY_DIR = original_dir
                memory.MEMORY_INDEX = original_index

        self.assertIn("[feedback] Tone", prompt)
        self.assertIn("[contact] Friend", prompt)


if __name__ == "__main__":
    unittest.main()
