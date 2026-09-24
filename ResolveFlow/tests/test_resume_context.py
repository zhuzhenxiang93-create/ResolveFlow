import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from contextlib import redirect_stdout
from io import StringIO
from scripts.task_view import summary
from agents.action_runtime import ActionRuntime


class Client:
    def __init__(self, success=False):
        self.requests = []
        self.success = success

    async def create_tool_turn(self, **kwargs):
        self.requests.append(kwargs)
        if self.success and len(self.requests) == 2:
            order = json.loads(kwargs["messages"][0]["content"])["order_id"]
            return {"tool_calls": [{"id": "fixed", "function": {"name": "query_billing", "arguments": json.dumps({"order_id": order})}}]}
        return {"tool_calls": [], "content": "Untrusted model text", "finish_reason": "stop"}


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    def test_reviewer_show_without_id_and_debug(self):
        from scripts.approval_cli import main
        auth = Mock()
        auth.is_symlink.return_value = False
        auth.exists.return_value = True
        auth.stat.return_value.st_mode = 0o100600
        response = Mock(is_success=True, is_redirect=False)
        response.json.return_value = {"task": {"id": "selected-task", "status": "needs_human", "response": "已暂停", "tool_messages": ["AUDIT_ONLY"]}}
        http = Mock()
        http.get.return_value = response
        manager = Mock()
        manager.__enter__ = Mock(return_value=http)
        manager.__exit__ = Mock(return_value=False)
        output = StringIO()
        with patch.dict("os.environ", {}), \
             patch("scripts.approval_cli.AUTH", auth), patch("scripts.approval_cli.dotenv_values", return_value={"AGENT_JWT_SECRET": "test-signing-secret"}), \
             patch("scripts.approval_cli.httpx.Client", return_value=manager), patch("sys.argv", ["review"]), \
             patch("builtins.input", side_effect=["/show selected-task", "/show", "/debug", "/quit"]), redirect_stdout(output):
            main()
        self.assertEqual(http.get.call_count, 3)
        self.assertTrue(all(call.args[0] == "/agent/review/tasks/selected-task" for call in http.get.call_args_list))
        self.assertEqual(output.getvalue().count("AUDIT_ONLY"), 1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.r = ActionRuntime(self.temp.name + "/db", allow_fallback=False)
        self.order = self.r.seed("alice")

    async def test_one_correction_then_success(self):
        self.r.client = Client(True)
        task = self.r.create("alice", "查询账单")
        result = await self.r.advance(task["id"], "alice", self.order["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.r.client.requests), 2)
        self.assertEqual(result["no_tool_corrections"], 1)
        self.assertIn("Previous response", self.r.client.requests[1]["messages"][-1]["content"])
        self.assertNotIn("Untrusted model text", json.dumps(result))
        self.assertEqual(result["attempts"][0]["finish_reason"], "stop")

    async def test_budget_survives_release(self):
        self.r.client = Client()
        task = self.r.create("alice", "查询账单")
        task = await self.r.advance(task["id"], "alice", self.order["id"])
        self.assertEqual(task["status"], "needs_human")
        self.assertEqual(len(self.r.client.requests), 2)
        self.r.release_handoff(task["id"], "alice", "reviewer")
        task = await self.r.advance(task["id"], "alice")
        self.assertEqual(len(self.r.client.requests), 3)
        self.assertEqual(task["fallback_count"], 0)

    async def test_release_keeps_audit_but_excludes_old_tool_transcript(self):
        task = self.r.create("alice", "请申请重复扣款退款")
        task = await self.r.advance(task["id"], "alice", self.order["id"])
        self.r.confirm(task["id"], "alice", task["confirmations"]["request_refund"]["id"], True)
        task = await self.r.advance(task["id"], "alice")
        old_messages = task["tool_messages"]
        await self.r.advance(task["id"], "alice", message="改变需求")
        task = self.r.release_handoff(task["id"], "alice", "reviewer")
        self.assertEqual(task["tool_messages"], old_messages)
        messages = self.r._execution_messages(task, ["query_billing"])
        self.assertFalse(any(m["role"] == "tool" for m in messages))
        current = json.loads(messages[0]["content"])
        self.assertFalse(current["approvals"])
        self.assertEqual(current["confirmations"]["request_refund"]["status"], "invalidated")
        self.assertIsNotNone(current["resume_context"])

    def test_summary_hides_audit_noise(self):
        task = {"id": "t", "status": "needs_human", "response": "退款尚未完成", "verification": {"billing": False},
                "tool_messages": ["RAW SECRET AUDIT"], "plans": ["many plans"], "evidence": []}
        view = summary({"task": task}, reviewer=True)
        self.assertIn("退款尚未完成", view)
        self.assertNotIn("RAW SECRET AUDIT", view)
        self.assertNotIn("many plans", view)
        self.assertLess(len(view.splitlines()), 12)
