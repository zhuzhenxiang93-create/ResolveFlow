import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from agents.action_runtime import ActionRuntime
from agents.goal_interpreter import GoalProposal, GoalInterpreter
from agents.subscription_knowledge import SubscriptionKnowledge, is_consultation_only


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_cannot_turn_howto_into_operation(self):
        client = AsyncMock()
        client.create_tool_turn.return_value = {"tool_calls": [{"id": "c1", "function": {
            "name": "interpret_goal", "arguments": json.dumps({"goals": {"renewal": "cancel"}})}}]}
        task = self.runtime.create("alice", "如何停止下个月续费")
        proposal, record = await GoalInterpreter(client).interpret("如何停止下个月续费", task)
        self.assertEqual(proposal.goals, {"policy": "read"})
        self.assertIn("server_guard", record)
        self.assertFalse(is_consultation_only("如何退订？请帮我关闭自动续费"))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = ActionRuntime(self.temp.name + "/db", allow_fallback=False)
        self.order = self.runtime.seed("alice")

    def stored(self):
        with self.runtime.connect() as db:
            return json.loads(db.execute("SELECT body FROM orders WHERE id=?", (self.order["id"],)).fetchone()[0])

    async def run_task(self, message, order=True):
        task = self.runtime.create("alice", message)
        return await self.runtime.advance(task["id"], "alice", self.order["id"] if order else None)

    async def test_policy_no_order_and_citations(self):
        for question, topic in [("退款政策是什么", "refund"), ("怎么退订", "renewal"), ("有什么权益", "plans")]:
            result = await self.run_task(question, False)
            self.assertEqual(result["status"], "completed")
            self.assertIsNone(result["order_id"])
            self.assertEqual(result["actions"], [])
            self.assertIn("subscription-" + topic, result["response"])
            self.assertTrue(result["knowledge_result"]["sources"])

    async def test_cancel_confirm_verify_resume(self):
        task = await self.run_task("请关闭自动续费")
        self.assertEqual(task["status"], "awaiting_confirmation")
        self.assertTrue(self.stored()["auto_renew"])
        self.assertIn("保留当前权益", task["response"])
        cid = task["confirmations"]["cancel_renewal"]["id"]
        restarted = ActionRuntime(self.runtime.path, allow_fallback=False)
        restarted.confirm(task["id"], "alice", cid, True)
        result = await restarted.advance(task["id"], "alice")
        self.assertEqual(result["status"], "completed")
        self.assertFalse(self.stored()["auto_renew"])
        self.assertIn("已关闭", result["response"])
        for key in ("plan", "entitlement", "charges", "refunds"):
            self.assertEqual(self.stored()[key], self.order[key])
        again = await restarted.advance(task["id"], "alice")
        self.assertEqual(len(again["actions"]), 1)

    async def test_query_is_readonly(self):
        result = await self.run_task("查询自动续费状态")
        self.assertEqual(result["status"], "completed")
        self.assertIn("已开启", result["response"])
        self.assertEqual(result["actions"], [])

    async def test_decline_does_not_cancel_renewal(self):
        task = await self.run_task("关闭自动续费")
        result = self.runtime.confirm(task["id"], "alice", task["confirmations"]["cancel_renewal"]["id"], False)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(self.stored()["auto_renew"])

    async def test_noop_write_is_not_success(self):
        task = await self.run_task("关闭自动续费")
        self.runtime.confirm(task["id"], "alice", task["confirmations"]["cancel_renewal"]["id"], True)
        with patch.object(self.runtime, "_write_order", return_value=None):
            result = await self.runtime.advance(task["id"], "alice")
        self.assertEqual(result["status"], "needs_human")
        self.assertTrue(self.stored()["auto_renew"])

    async def test_lost_response_reconciles(self):
        task = await self.run_task("关闭自动续费")
        self.runtime.confirm(task["id"], "alice", task["confirmations"]["cancel_renewal"]["id"], True)
        write = self.runtime._write_order
        def lost(db, task, order):
            write(db, task, order)
            raise TimeoutError()
        with patch.object(self.runtime, "_write_order", side_effect=lost):
            result = await self.runtime.advance(task["id"], "alice")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["actions"][0]["reconciled"])

    async def test_unknown_legacy_renewal_is_not_assumed(self):
        order = self.stored()
        del order["auto_renew"]
        with self.runtime.connect() as db:
            db.execute("UPDATE orders SET body=? WHERE id=?", (json.dumps(order), order["id"]))
        result = await self.run_task("关闭自动续费")
        self.assertEqual(result["status"], "needs_human")
        self.assertEqual(result["actions"], [])

    async def test_mixed_policy_and_lookup_preserves_answer(self):
        task = self.runtime.create("alice", "退款条件是什么，同时查账单")
        proposal = GoalProposal(goals={"policy": "read", "billing": "read"}, policy_topics=["refund"])
        with patch.object(self.runtime.goal_interpreter, "interpret", new=AsyncMock(return_value=(
                proposal, {"mode": "mock", "calls": 0, "usage": []}))):
            result = await self.runtime.advance(task["id"], "alice")
        self.assertEqual(result["status"], "awaiting_clarification")
        self.assertIn("subscription-refund", result["response"])
        self.assertIn("订单号", result["response"])

    async def test_missing_knowledge_never_completes(self):
        with patch.object(SubscriptionKnowledge, "answer", return_value=None):
            result = await self.run_task("退款政策", False)
        self.assertEqual(result["status"], "needs_human")
        self.assertNotIn(True, result["verification"].values())

    def test_all_topics_have_scoped_sources(self):
        result = SubscriptionKnowledge().answer(["plans", "renewal", "refund", "troubleshooting", "safety"])
        self.assertEqual(len(result["sources"]), 5)
        self.assertTrue(result["simulated"])
        with self.assertRaises(ValueError):
            SubscriptionKnowledge().answer(["shipping"])
