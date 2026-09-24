"""BaseAgent.handle()'s except-Exception branch used to swallow the real error
entirely, replying with a generic apology and only ever logging the exception
server-side (agents/agent_orchestrator.py). That made a genuine code bug and a
transient LLM API hiccup indistinguishable from the outside. These tests cover
the sanitized diagnostic (category/error_type/http_status — never the raw
exception message) that now rides along on AgentResponse/OrchestratorResult,
mirroring the discipline GoalInterpreter.interpret already uses for its own
failures (agents/goal_interpreter.py)."""
import asyncio
import unittest

from agents.agent_orchestrator import AgentType, GeneralAgent, Request


class _RaisingClient:
    def __init__(self, exc):
        self._exc = exc

    async def create(self, **kwargs):
        raise self._exc


class HandleDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_failure_reports_sanitized_category_and_type(self):
        agent = GeneralAgent(client=_RaisingClient(RuntimeError("secret upstream body, do not leak")), model="x")
        response = await agent.handle(Request(message="hi", user_id="u", conv_id="c"))
        self.assertFalse(response.success)
        self.assertEqual(response.content, "抱歉，处理您的请求时出现问题，请稍后重试。")
        self.assertEqual(response.error_diagnostic["category"], "agent_call_failed")
        self.assertEqual(response.error_diagnostic["error_type"], "RuntimeError")
        self.assertNotIn("secret upstream body", str(response.error_diagnostic))

    async def test_timeout_gets_its_own_category(self):
        agent = GeneralAgent(client=_RaisingClient(asyncio.TimeoutError()), model="x")
        response = await agent.handle(Request(message="hi", user_id="u", conv_id="c"))
        self.assertEqual(response.error_diagnostic["category"], "timeout")

    async def test_http_status_is_captured_when_present(self):
        exc = RuntimeError("rate limited")
        exc.status_code = 429
        agent = GeneralAgent(client=_RaisingClient(exc), model="x")
        response = await agent.handle(Request(message="hi", user_id="u", conv_id="c"))
        self.assertEqual(response.error_diagnostic["http_status"], 429)

    async def test_success_leaves_diagnostic_none(self):
        class OkClient:
            async def create(self, **kwargs):
                return "ok"
        agent = GeneralAgent(client=OkClient(), model="x")
        response = await agent.handle(Request(message="hi", user_id="u", conv_id="c"))
        self.assertTrue(response.success)
        self.assertIsNone(response.error_diagnostic)


if __name__ == "__main__":
    unittest.main()
