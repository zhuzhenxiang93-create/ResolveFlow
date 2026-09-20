import unittest
from unittest.mock import AsyncMock, patch

from evaluation.agent_evaluator import evaluate
from scripts.run_agent_eval import BudgetClient


class EvaluatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_counts_scopes_and_unavailable_values(self):
        report = await evaluate()
        self.assertEqual(report["sample_count"], len(report["rows"]))
        self.assertTrue(all(r["passed"] for r in report["rows"]))
        metrics = report["metrics"]
        self.assertEqual(metrics["expected_outcome_rate"]["numerator"], 12)
        self.assertEqual(metrics["business_task_completion_rate"]["numerator"], 7)
        self.assertLess(metrics["business_task_completion_rate"]["value"], 1)
        self.assertIsNone(metrics["cost_usd"])
        self.assertIsNone(metrics["input_tokens"])
        self.assertIsNone(metrics["tool_error_correction_rate"])
        self.assertEqual(metrics["unauthorized_operations"], 0)
        self.assertEqual(metrics["duplicate_operations"], 0)
        self.assertIn("data_sha256", report)

    async def test_budget_prevents_extra_provider_request(self):
        inner = AsyncMock()
        client = BudgetClient(inner, 1, 1, 1, 1)
        await client.create_tool_turn(messages=[], tools=[], max_tokens=10)
        with self.assertRaises(ValueError):
            await client.create_tool_turn(messages=[], tools=[], max_tokens=10)
        self.assertEqual(inner.create_tool_turn.await_count, 1)

    async def test_failed_requests_retain_reservation(self):
        inner = AsyncMock()
        inner.create_tool_turn.side_effect = TimeoutError()
        client = BudgetClient(inner, 1, 1, 1, 1)
        with self.assertRaises(TimeoutError):
            await client.create_tool_turn(messages=[], tools=[], max_tokens=10)
        self.assertEqual(client.calls, 1)
        self.assertGreater(client.reserved, 0)

    async def test_oracle_rejects_false_completion(self):
        def lie(runtime, task, order):
            task.update(status="completed", verification={g: True for g in task["goals"]}, unresolved=[])
            for step in task["plan"]["steps"]:
                step["status"] = "completed"
        with patch("agents.action_runtime.ActionRuntime._refresh", lie):
            report = await evaluate()
        by_id = {r["id"]: r for r in report["rows"]}
        self.assertFalse(by_id["repair"]["business_completed"])
        self.assertFalse(by_id["query"]["passed"])
        self.assertFalse(by_id["outage"]["business_completed"])
