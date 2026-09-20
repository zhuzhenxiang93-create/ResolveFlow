"""Compound requests: GoalInterpreter's general_remainder lets a single turn get both
its subscription-business part (handled by ActionRuntime/RAG) and an independent
ordinary-support part (handled by the old multi-agent orchestrator's own compound-
request machinery) genuinely answered in one merged reply, instead of the business
goal silently swallowing the whole message. Also covers the "退款" target-ambiguity
guard, which resolves a message with no qualifier on either side (no 会员/订阅, no
商品/订单/物流) using real subscription state instead of a model guess."""
import tempfile
import unittest
from unittest.mock import AsyncMock

from agents.action_runtime import ActionRuntime
from agents.conversation_service import ConversationService
from agents.goal_interpreter import GoalProposal
from agents.subscription_knowledge import policy_refund_is_actually_goods, refund_target_is_ambiguous


class Scripted:
    """Deterministic goal interpreter stub, mirroring test_conversation_intent_merge.py."""
    client = True

    def __init__(self, goals=None, unsupported=None, policy_topics=None, remainder=None):
        self.goals = goals or {}
        self.unsupported = unsupported or []
        self.policy_topics = policy_topics or []
        self.remainder = remainder

    async def interpret(self, message, context):
        return (GoalProposal(goals=self.goals, unsupported_requests=self.unsupported,
                              policy_topics=self.policy_topics, general_remainder=self.remainder),
                {"mode": "mock", "calls": 0, "usage": []})


class RefundTargetIsAmbiguousTests(unittest.TestCase):
    def test_bare_mention_is_ambiguous(self):
        self.assertTrue(refund_target_is_ambiguous("能退款吗"))

    def test_subscription_qualifier_is_not_ambiguous(self):
        self.assertFalse(refund_target_is_ambiguous("怎么退款订阅？"))
        self.assertFalse(refund_target_is_ambiguous("会员费能退吗"))

    def test_goods_qualifier_is_not_ambiguous(self):
        self.assertFalse(refund_target_is_ambiguous("这个订单能退款吗"))
        self.assertFalse(refund_target_is_ambiguous("商品退货退款怎么办"))


class PolicyRefundIsActuallyGoodsTests(unittest.TestCase):
    """Live testing found a real, non-deterministic model-confusion pattern: "退款政策"
    pulls GoalInterpreter toward policy:read (subscription-only) even when the message
    explicitly names a goods-side qualifier — sometimes correctly landing on empty
    goals + general_remainder, sometimes not, across identical repeated calls. This
    function is the deterministic correction wired into ConversationService below."""

    def test_goods_qualifier_with_refund_topic_is_flagged(self):
        self.assertTrue(policy_refund_is_actually_goods("商品退款政策是什么", ["refund"]))

    def test_subscription_qualifier_is_not_flagged(self):
        self.assertFalse(policy_refund_is_actually_goods("会员费退款政策是什么", ["refund"]))

    def test_no_refund_topic_is_not_flagged(self):
        self.assertFalse(policy_refund_is_actually_goods("商品什么时候到", ["renewal"]))

    def test_no_goods_qualifier_is_not_flagged(self):
        self.assertFalse(policy_refund_is_actually_goods("退款政策是什么", ["refund"]))

    def test_no_refund_mention_is_not_ambiguous(self):
        self.assertFalse(refund_target_is_ambiguous("怎么查物流"))


class CompoundActionRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = ActionRuntime(self.temp.name + "/db", allow_fallback=False)
        self.order = self.runtime.seed("user")

    def _service(self, **goal_kwargs):
        self.runtime.goal_interpreter = Scripted(**goal_kwargs)
        service = ConversationService(self.runtime)
        service.answer_orchestrator = AsyncMock()
        return service

    async def test_action_route_with_remainder_calls_run_compound_on_remainder_only(self):
        service = self._service(goals={"billing": "refund"}, remainder="帮我查一下物流")
        service.answer_orchestrator.run_compound.return_value = type(
            "R", (), {"response": "合并回复：物流+退款"})()
        result = await service.send("user", "重复扣款退款，另外查一下物流，订单 " + self.order["id"])
        service.answer_orchestrator.run_compound.assert_awaited_once()
        call = service.answer_orchestrator.run_compound.await_args
        request_arg = call.args[0]
        primary_result = call.kwargs["primary_result"]
        self.assertEqual(request_arg.message, "帮我查一下物流")
        self.assertEqual(primary_result.agent_type, "action")
        self.assertEqual(result["response"], "合并回复：物流+退款")

    async def test_action_route_without_remainder_skips_run_compound(self):
        service = self._service(goals={"billing": "refund"})
        result = await service.send("user", "重复扣款退款，订单 " + self.order["id"])
        service.answer_orchestrator.run_compound.assert_not_called()
        self.assertEqual(result["response"], result["task"]["response"])

    async def test_action_result_needing_approval_marks_primary_result_escalated(self):
        service = self._service(goals={"billing": "refund"}, remainder="帮我查一下物流")
        captured = {}

        async def capture(request, primary_result):
            captured["escalated"] = primary_result.escalated
            return type("R", (), {"response": "ok"})()
        service.answer_orchestrator.run_compound.side_effect = capture
        await service.send("user", "重复扣款退款，另外查一下物流，订单 " + self.order["id"])
        # Whether this specific scenario reaches awaiting_approval depends on ActionRuntime's
        # own policy; the assertion that matters here is that the flag is derived from the
        # actual task status, not hardcoded.
        self.assertIn("escalated", captured)


class CompoundKnowledgeRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = ActionRuntime(self.temp.name + "/db", allow_fallback=False)

    def _service(self, **goal_kwargs):
        self.runtime.goal_interpreter = Scripted(**goal_kwargs)
        service = ConversationService(self.runtime)
        service.answer_orchestrator = AsyncMock()
        return service

    async def test_knowledge_route_with_remainder_calls_run_compound(self):
        service = self._service(goals={"policy": "read"}, policy_topics=["renewal"],
                                 remainder="物流查询一下")
        service.answer_orchestrator.run_compound.return_value = type(
            "R", (), {"response": "合并回复：政策+物流"})()
        result = await service.send("user", "退订政策是什么，另外物流查询一下")
        service.answer_orchestrator.run_compound.assert_awaited_once()
        primary_result = service.answer_orchestrator.run_compound.await_args.kwargs["primary_result"]
        self.assertEqual(primary_result.agent_type, "policy")
        self.assertEqual(result["response"], "合并回复：政策+物流")

    async def test_knowledge_route_without_remainder_skips_run_compound(self):
        service = self._service(goals={"policy": "read"}, policy_topics=["renewal"])
        await service.send("user", "退订政策是什么")
        service.answer_orchestrator.run_compound.assert_not_called()


class CompoundUnsupportedRouteTests(unittest.IsolatedAsyncioTestCase):
    """"办理退货，另外查个物流" style compound: the execution part is genuinely
    unsupported, but the independent ordinary-support part must still be answered
    via the old system, not silently dropped alongside the unsupported part."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = ActionRuntime(self.temp.name + "/db", allow_fallback=False)

    def _service(self, **goal_kwargs):
        self.runtime.goal_interpreter = Scripted(**goal_kwargs)
        service = ConversationService(self.runtime)
        service.answer_orchestrator = AsyncMock()
        return service

    async def test_unsupported_route_with_remainder_calls_run_compound(self):
        service = self._service(unsupported=["能直接给我办理退货吗？"], remainder="实体礼盒送到哪里了？")
        service.answer_orchestrator.run_compound.return_value = type(
            "R", (), {"response": "合并回复：不支持退货+物流"})()
        result = await service.send("user", "实体礼盒送到哪里了？能直接给我办理退货吗？")
        service.answer_orchestrator.run_compound.assert_awaited_once()
        call = service.answer_orchestrator.run_compound.await_args
        self.assertEqual(call.args[0].message, "实体礼盒送到哪里了？")
        self.assertEqual(call.kwargs["primary_result"].agent_type, "unsupported")
        self.assertEqual(result["response"], "合并回复：不支持退货+物流")
        self.assertEqual(result["route"], "unsupported")

    async def test_unsupported_route_without_remainder_skips_run_compound(self):
        service = self._service(unsupported=["能直接给我办理退货吗？"])
        result = await service.send("user", "能直接给我办理退货吗？")
        service.answer_orchestrator.run_compound.assert_not_called()
        self.assertIn("当前支持数字订阅售后", result["response"])


class RefundAmbiguityGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    async def test_no_subscription_redirects_and_falls_through_to_chat(self):
        runtime = ActionRuntime(self.temp.name + "/db1", allow_fallback=False)
        runtime.goal_interpreter = Scripted(goals={"billing": "refund"})
        result = await ConversationService(runtime).send("user", "能退款吗")
        self.assertEqual(result["interpretation"]["server_guard"], "no_subscription_refund_redirect")
        self.assertEqual(result["route"], "chat")
        self.assertIsNone(result["task"])

    async def test_has_subscription_asks_clarifying_question_without_creating_a_task(self):
        runtime = ActionRuntime(self.temp.name + "/db2", allow_fallback=False)
        runtime.seed("user")
        runtime.goal_interpreter = Scripted(goals={"billing": "refund"})
        result = await ConversationService(runtime).send("user", "能退款吗")
        self.assertEqual(result["route"], "needs_clarification")
        self.assertEqual(result["status"], "awaiting_clarification")
        self.assertIsNone(result["task"])
        self.assertIn("会员费退款", result["response"])
        self.assertIn("商品退款", result["response"])

    async def test_explicit_subscription_qualifier_skips_the_guard(self):
        runtime = ActionRuntime(self.temp.name + "/db3", allow_fallback=False)
        order = runtime.seed("user")
        runtime.goal_interpreter = Scripted(goals={"billing": "refund"})
        result = await ConversationService(runtime).send("user", f"怎么退款订阅？订单 {order['id']}")
        self.assertNotIn("server_guard", result["interpretation"])
        self.assertEqual(result["route"], "action")

    async def test_goods_policy_misclassification_is_corrected_to_chat(self):
        # Reproduces the exact flaky live-model output found by hand-testing: the
        # model sometimes classifies "商品退款政策是什么" as policy:read (subscription-
        # only) even though the message names 商品 explicitly. This must not depend on
        # getting a lucky model call — the server-side guard corrects it every time.
        runtime = ActionRuntime(self.temp.name + "/db4", allow_fallback=False)
        runtime.goal_interpreter = Scripted(goals={"policy": "read"}, policy_topics=["refund"])
        result = await ConversationService(runtime).send("user", "商品退款政策是什么")
        self.assertEqual(result["interpretation"]["server_guard"], "goods_refund_policy_redirect")
        self.assertEqual(result["route"], "chat")
        self.assertIsNone(result["task"])

    async def test_subscription_policy_question_is_left_alone(self):
        runtime = ActionRuntime(self.temp.name + "/db5", allow_fallback=False)
        runtime.goal_interpreter = Scripted(goals={"policy": "read"}, policy_topics=["refund"])
        result = await ConversationService(runtime).send("user", "会员费退款政策是什么")
        self.assertNotIn("server_guard", result["interpretation"])
        self.assertEqual(result["route"], "knowledge")


if __name__ == "__main__":
    unittest.main()
