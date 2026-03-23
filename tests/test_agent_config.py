import os
import tempfile
import unittest
from importlib import reload


class AgentConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_env = {
            "HOME": os.environ.get("HOME"),
            "LLM_PROVIDER": os.environ.get("LLM_PROVIDER"),
            "BEDROCK_API_KEY": os.environ.get("BEDROCK_API_KEY"),
            "BEDROCK_MODEL": os.environ.get("BEDROCK_MODEL"),
            "OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY"),
            "OPENROUTER_MODEL": os.environ.get("OPENROUTER_MODEL"),
            "BROWSER_BACKEND": os.environ.get("BROWSER_BACKEND"),
            "BROWSER_FALLBACK_BACKEND": os.environ.get("BROWSER_FALLBACK_BACKEND"),
            "BROWSER_USE_API_KEY": os.environ.get("BROWSER_USE_API_KEY"),
            "BROWSER_USE_PROFILE_ID": os.environ.get("BROWSER_USE_PROFILE_ID"),
            "BROWSERBASE_CONTEXT_ID": os.environ.get("BROWSERBASE_CONTEXT_ID"),
        }
        os.environ["HOME"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_bedrock_provider_uses_bedrock_models(self):
        os.environ["LLM_PROVIDER"] = "bedrock"
        os.environ["BEDROCK_API_KEY"] = "test-bedrock-key"
        os.environ["BEDROCK_MODEL"] = "qwen.qwen3-next-80b-a3b-instruct"

        import bot.agent as agent

        agent = reload(agent)
        self.assertEqual(agent.get_provider(), "bedrock")
        self.assertEqual(agent.get_provider_label(), "Amazon Bedrock")
        self.assertEqual(agent.get_preferred_model(), "qwen.qwen3-next-80b-a3b-instruct")
        self.assertIn("zai.glm-5", agent.get_fallback_chain())
        self.assertNotIn("openrouter/free", agent.get_fallback_chain())

    def test_provider_scoped_model_preference(self):
        os.environ["LLM_PROVIDER"] = "bedrock"
        os.environ["BEDROCK_API_KEY"] = "test-bedrock-key"

        import bot.agent as agent

        agent = reload(agent)
        agent.set_preferred_model("zai.glm-5")
        self.assertEqual(agent.get_preferred_model(), "zai.glm-5")

        os.environ["LLM_PROVIDER"] = "openrouter"
        os.environ["OPENROUTER_API_KEY"] = "test-openrouter-key"
        agent = reload(agent)
        self.assertEqual(agent.get_provider(), "openrouter")
        self.assertNotEqual(agent.get_preferred_model(), "zai.glm-5")

    def test_browser_runtime_note_reflects_browserbase(self):
        os.environ["LLM_PROVIDER"] = "bedrock"
        os.environ["BEDROCK_API_KEY"] = "test-bedrock-key"
        os.environ["BROWSER_BACKEND"] = "browserbase"
        os.environ["BROWSERBASE_CONTEXT_ID"] = "ctx_test"

        import bot.agent as agent

        agent = reload(agent)
        self.assertIn("Browserbase", agent._browser_runtime_note())
        self.assertIn("yes", agent._browser_runtime_note().lower())

    def test_browser_runtime_note_reflects_browser_use_with_fallback(self):
        os.environ["LLM_PROVIDER"] = "bedrock"
        os.environ["BEDROCK_API_KEY"] = "test-bedrock-key"
        os.environ["BROWSER_BACKEND"] = "browser-use"
        os.environ["BROWSER_FALLBACK_BACKEND"] = "browserbase"
        os.environ["BROWSER_USE_API_KEY"] = "bu_test_key"
        os.environ["BROWSER_USE_PROFILE_ID"] = "profile_test"

        import bot.agent as agent

        agent = reload(agent)
        note = agent._browser_runtime_note()
        self.assertIn("Browser Use", note)
        self.assertIn("fallback", note)
        self.assertIn("yes", note.lower())


if __name__ == "__main__":
    unittest.main()
