"""Tier 1.5: GoalInterpreter runs first and decides routing; IntentRecognizer's
real model call must only fire on the "chat" fallback (no business goal at
all) — every other route derives a zero-cost telemetry label from the already
-parsed goals instead of paying for a second real model call. See
wiki/product_positioning.md and the plan context for why this is safe:
AgentOrchestrator only consults IntentRecognizer's output on the chat route,
GoalInterpreter's prompt/schema are untouched, and evaluation/evaluator.py's
independent intent-accuracy harness never goes through ConversationService."""
import tempfile
import unittest
from unittest.mock import AsyncMock

from agents.action_runtime import ActionRuntime
from agents.conversation_service import ConversationService, _telemetry_intent
from agents.goal_interpreter import GoalProposal


class Scripted:
    """Deterministic goal interpreter stub: returns whatever this test wants."""
    client = True

    def __init__(self, goals=None, unsupported=None, policy_topics=None):
        self.goals = goals or {}
        self.unsupported = unsupported or []
        self.policy_topics = policy_topics or []
        self.calls = 0

    async def interpret(self, message, context):
        self.calls += 1
        return (GoalProposal(goals=self.goals, unsupported_requests=self.unsupported,
                              policy_topics=self.policy_topics),
                {"mode": "mock", "calls": 0, "usage": []})


class RecognizerCallCountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = ActionRuntime(self.temp.name + "/db", allow_fallback=False)
        self.order = self.runtime.seed("user")

    def _service(self, **goal_kwargs):
        self.runtime.goal_interpreter = Scripted(**goal_kwargs)
        service = ConversationService(self.runtime)
        service.recognizer.recognize = AsyncMock()
        return service

    async def test_action_route_never_calls_recognizer(self):
        service = self._service(goals={"billing": "refund"})
        await service.send("user", "请申请重复扣款退款，订单 " + self.order["id"])
        service.recognizer.recognize.assert_not_called()

    async def test_policy_route_never_calls_recognizer(self):
        service = self._service(goals={"policy": "read"}, policy_topics=["refund"])
        await service.send("user", "退款政策是什么")
        service.recognizer.recognize.assert_not_called()

    async def test_unsupported_route_never_calls_recognizer(self):
        service = self._service(unsupported=["寄丢的快递理赔"])
        await service.send("user", "我的快递弄丢了要理赔")
        service.recognizer.recognize.assert_not_called()

    async def test_goal_interpreter_failure_never_calls_recognizer(self):
        self.runtime.client=AsyncMock()
        self.runtime.client.create_tool_turn.side_effect=TimeoutError()
        service=ConversationService(self.runtime)
        service.recognizer.recognize=AsyncMock()
        result=await service.send("user","帮我查一下")
        self.assertEqual(result["interpretation"]["mode"],"model_error")
        service.recognizer.recognize.assert_not_called()

    async def test_chat_route_calls_recognizer_exactly_once(self):
        service = self._service(goals={})
        from core.intent_recognizer import IntentCategory, IntentResult, UrgencyLevel
        service.recognizer.recognize.return_value = IntentResult(
            intent=IntentCategory.GREETING, confidence=0.9, urgency=UrgencyLevel.LOW,
            intent_group="greeting", entities={}, reasoning="", latency_ms=1.0,
            source_scores={"llm": 0.9})
        result = await service.send("user", "你好")
        service.recognizer.recognize.assert_awaited_once()
        self.assertEqual(result["route"], "chat")
        self.assertEqual(result["intent"], "greeting")

    async def test_chat_route_recognizer_failure_returns_model_error_without_side_effects(self):
        service = self._service(goals={})
        service.recognizer.recognize.side_effect = TimeoutError()
        result = await service.send("user", "你好")
        self.assertEqual(result["route"], "model_error")
        self.assertEqual(result["status"], "retryable_error")
        self.assertIn("intent_unavailable", result["degradations"])


class TelemetryIntentMappingTests(unittest.TestCase):
    def test_failed_interpretation(self):
        value, scores = _telemetry_intent("needs_human", None)
        self.assertEqual(value, "other")
        self.assertEqual(scores, {"goal_interpreter": 0.0})

    def test_unsupported(self):
        proposal = GoalProposal(goals={}, unsupported_requests=["x"])
        value, scores = _telemetry_intent("unsupported", proposal)
        self.assertEqual((value, scores), ("other", {"goal_derived": 1.0}))

    def test_policy_is_query(self):
        proposal = GoalProposal(goals={"policy": "read"})
        value, _ = _telemetry_intent("knowledge", proposal)
        self.assertEqual(value, "query")

    def test_billing_refund(self):
        proposal = GoalProposal(goals={"billing": "refund"})
        value, _ = _telemetry_intent("action", proposal)
        self.assertEqual(value, "refund")

    def test_billing_read(self):
        proposal = GoalProposal(goals={"billing": "read"})
        value, _ = _telemetry_intent("action", proposal)
        self.assertEqual(value, "billing")

    def test_entitlement_maps_to_account(self):
        proposal = GoalProposal(goals={"entitlement": "repair"})
        value, _ = _telemetry_intent("action", proposal)
        self.assertEqual(value, "account")

    def test_renewal_maps_to_account(self):
        proposal = GoalProposal(goals={"renewal": "cancel"})
        value, _ = _telemetry_intent("action", proposal)
        self.assertEqual(value, "account")

    def test_service_maps_to_technical(self):
        proposal = GoalProposal(goals={"service": "read"})
        value, _ = _telemetry_intent("action", proposal)
        self.assertEqual(value, "technical")

    def test_priority_billing_over_entitlement(self):
        proposal = GoalProposal(goals={"entitlement": "repair", "billing": "read"})
        value, _ = _telemetry_intent("action", proposal)
        self.assertEqual(value, "billing")

    def test_no_goals_defaults_to_other(self):
        proposal = GoalProposal(goals={})
        value, scores = _telemetry_intent("action", proposal)
        self.assertEqual((value, scores), ("other", {"goal_derived": 1.0}))


if __name__ == "__main__":
    unittest.main()
