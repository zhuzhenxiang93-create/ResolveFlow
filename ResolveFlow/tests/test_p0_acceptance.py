import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from scripts.run_p0_acceptance import BudgetClient, DATA, FROZEN_SHA, goals_match


class AcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_acceptance_requires_fresh_query(self):
        from scripts.run_p0_acceptance import run_case
        case = next(c for c in json.loads(DATA.read_text())["cases"] if c["id"] == "refund-recovery")
        with tempfile.TemporaryDirectory() as directory:
            result = await run_case(case, None, directory)
        self.assertTrue(result["passed"])
        self.assertGreaterEqual(result["billing_query_count"], 2)
        self.assertTrue(result["stale_evidence_injected"])

    def test_constraint_and_consultation_rules_do_not_hide_explicit_queries(self):
        from agents.subscription_knowledge import benefits_are_constraint, is_consultation_only
        self.assertTrue(benefits_are_constraint("停止续费但保持现有会员权益"))
        self.assertFalse(benefits_are_constraint("保留权益，同时帮我检查套餐权益"))
        self.assertTrue(is_consultation_only("假设停止续费会影响权益吗"))
        self.assertFalse(is_consultation_only("如果可以，请帮我关闭自动续费"))

    async def test_read_diagnosis_suggests_but_does_not_write(self):
        from agents.action_runtime import ActionRuntime
        with tempfile.TemporaryDirectory() as directory:
            r = ActionRuntime(Path(directory) / "db", allow_fallback=False)
            order = r.seed("user")
            task = r.create("user", "查询订阅和账单")
            task = await r.advance(task["id"], "user", order["id"])
            self.assertEqual(task["status"], "completed")
            self.assertEqual(len(task["recommendations"]), 2)
            self.assertEqual(task["actions"], [])
            self.assertIn("异常本身尚未解决", task["response"])
            self.assertFalse(task["confirmations"])

    def test_frozen_data_and_unique_ids(self):
        self.assertEqual(hashlib.sha256(DATA.read_bytes()).hexdigest(), FROZEN_SHA)
        cases = json.loads(DATA.read_text())["cases"]
        self.assertEqual(len(cases), 11)
        self.assertEqual(len({c["id"] for c in cases}), len(cases))
        self.assertFalse(goals_match({"goals": {"renewal": "cancel"}}, {"goals": {"policy": "read"}}))

    async def test_campaign_budget_survives_restart_and_counts_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "budget.json"
            sdk = AsyncMock()
            sdk.create_tool_turn.side_effect = TimeoutError()
            first = BudgetClient(sdk, ledger, 1)
            with self.assertRaises(TimeoutError):
                await first.create_tool_turn(max_tokens=512)
            again = BudgetClient(sdk, ledger, 1)
            with self.assertRaises(RuntimeError):
                await again.create_tool_turn(max_tokens=512)
            self.assertEqual(sdk.create_tool_turn.await_count, 1)
            self.assertEqual(json.loads(ledger.read_text())["reserved_calls"], 1)

    async def test_expected_answers_not_sent_to_model(self):
        from scripts.run_p0_acceptance import run_case
        sdk = AsyncMock()
        sdk.create_tool_turn.return_value = {"tool_calls": [{"function": {
            "name": "interpret_goal", "arguments": json.dumps({"goals": {"billing": "read"}})}}]}
        with tempfile.TemporaryDirectory() as directory:
            result = await run_case({"id": "test", "kind": "semantic", "message": "查询账单", "goals": {"billing": "read"}}, sdk, directory)
        self.assertTrue(result["passed"])
        sent = sdk.create_tool_turn.call_args.kwargs["messages"]
        self.assertNotIn("expected", json.dumps(sent))
        self.assertNotIn('"billing": "read"', sent[0]["content"])
