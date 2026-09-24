import asyncio
import json
import tempfile
import unittest
from unittest.mock import patch

from action_test_support import ActionRuntime


class ScriptedClient:
    def __init__(self, names):
        self.names = iter(names)
        self.calls = []

    async def create_tool_turn(self, **kwargs):
        self.calls.append(kwargs)
        name = next(self.names)
        if name == "finish":
            return {"role": "assistant", "content": "Done"}
        state = json.loads(kwargs["messages"][0]["content"])
        return {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_" + str(len(self.calls)), "type": "function",
            "function": {"name": name, "arguments": json.dumps({"order_id": state["order_id"]})}}]}


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = self.tmp.name + "/state.sqlite3"
        self.runtime = ActionRuntime(self.path)
        self.order = self.runtime.seed("alice")

    async def pending(self):
        task = self.runtime.create("alice", "请修复升级后的权益，并申请重复扣款退款")
        self.assertEqual(task["status"], "awaiting_clarification")
        return await self.runtime.advance(task["id"], "alice", self.order["id"])

    async def test_complete_approval_and_restart(self):
        task = await self.pending()
        self.assertEqual(task["status"], "awaiting_approval")
        self.assertTrue(task["verification"]["entitlement"])
        self.assertFalse(task["verification"]["billing"])
        self.assertEqual({e["agent"] for e in task["evidence"]}, {"technical", "billing"})
        runtime = ActionRuntime(self.path)
        args = (task["id"], "alice", task["approvals"]["request_refund"]["id"], True, "reviewer")
        done = runtime.approve(*args)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(runtime.approve(*args), done)
        with runtime.connect() as db:
            state = json.loads(db.execute("SELECT body FROM orders").fetchone()[0])
        self.assertEqual(state["refunds"], 1)

    async def test_deny_and_isolation(self):
        task = await self.pending()
        with self.assertRaises(ValueError):
            self.runtime.get(task["id"], "bob")
        denied = self.runtime.approve(task["id"], "alice", task["approvals"]["request_refund"]["id"], False, "r")
        self.assertEqual(denied["status"], "rejected")
        foreign = self.runtime.create("bob", "重复扣款")
        result = await self.runtime.advance(foreign["id"], "bob", self.order["id"])
        self.assertEqual(result["status"], "awaiting_clarification")

    async def test_bare_order_id_reply_does_not_reinterpret_and_drop_goals(self):
        # Real-world shape of the bug: the user is asked "请提供订单号。" and replies
        # with NOTHING but the order id (typed straight into the chat box, so it
        # arrives as `message`, not a separate structured order_id argument). Before
        # the fix, that message — being neither task["message"] nor the previous
        # message — always triggered a fresh goal re-interpretation; the offline
        # rules fallback `understand()` finds no domain keywords in a bare UUID and
        # returns {} goals, which differs from the already-established {"billing":
        # "refund"} and trips the "检测到目标变化" guard, silently pausing a task the
        # user never actually tried to change. The fix skips re-interpretation
        # entirely when the message is nothing but the order id just extracted from
        # it, since that adds no goal-relevant information beyond what regex
        # extraction already captured deterministically.
        task = self.runtime.create("alice", "帮我申请重复扣款退款")
        first = await self.runtime.advance(task["id"], "alice")
        self.assertEqual(first["status"], "awaiting_clarification")
        self.assertEqual(first["goals"], {"billing": "refund"})
        second = await self.runtime.advance(task["id"], "alice", message=self.order["id"])
        self.assertEqual(second["goals"], {"billing": "refund"})
        self.assertNotEqual(second["status"], "needs_human")
        self.assertNotIn("目标变化", second["response"])
        self.assertEqual(second["order_id"], self.order["id"])

    async def test_llm_early_finish_cannot_claim_success(self):
        self.runtime.client = ScriptedClient(["finish"])
        task = self.runtime.create("alice", "重复扣款")
        result = await self.runtime.advance(task["id"], "alice", self.order["id"])
        self.assertEqual(result["status"], "needs_human")

    async def test_llm_observes_results_and_bounded_loop(self):
        client = ScriptedClient(["query_billing", "request_refund"])
        self.runtime.client = client
        task = self.runtime.create("alice", "请申请退款，重复扣款")
        result = await self.runtime.advance(task["id"], "alice", self.order["id"])
        self.assertEqual(result["status"], "awaiting_approval")
        context = json.loads(client.calls[1]["messages"][0]["content"])
        self.assertEqual(context["evidence"][0]["data"]["charges"], 2)
        self.runtime.client = ScriptedClient(["query_billing"] * 16)
        task = self.runtime.create("alice", "请申请退款，重复扣款")
        result = await self.runtime.advance(task["id"], "alice", self.order["id"])
        self.assertEqual(result["status"], "needs_human")

    async def test_unknown_tool_falls_back_and_write_verified(self):
        self.runtime.client = ScriptedClient(["delete_account", "check_service", "sync_entitlements", "finish"])
        task = self.runtime.create("alice", "请修复升级后的权益")
        with patch.object(self.runtime, "_write_order", return_value=None):
            result = await self.runtime.advance(task["id"], "alice", self.order["id"])
        self.assertNotEqual(result["status"], "completed")
        self.assertFalse(json.loads(result["tool_messages"][1]["content"])["success"])

    async def test_concurrent_resume_and_replayed_approval(self):
        task = self.runtime.create("alice", "请申请退款，重复扣款")
        results = await asyncio.gather(*[
            self.runtime.advance(task["id"], "alice", self.order["id"]) for _ in range(2)])
        self.assertEqual(results[0]["approvals"]["request_refund"]["id"], results[1]["approvals"]["request_refund"]["id"])
        with self.assertRaises(ValueError):
            self.runtime.approve(task["id"], "alice", "forged", True, "r")

    async def test_model_timeout_and_missing_information_recovery(self):
        from unittest.mock import AsyncMock
        client = AsyncMock()
        client.create_tool_turn.side_effect = asyncio.TimeoutError()
        runtime = ActionRuntime(self.path, client=client)
        task = runtime.create("alice", "请申请退款，重复扣款")
        self.assertEqual((await runtime.advance(task["id"], "alice"))["status"], "awaiting_clarification")
        result = await runtime.advance(task["id"], "alice", self.order["id"])
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertTrue(all(e["fallback"] for e in result["evidence"]))

    async def test_refund_requires_evidence_and_verifies_persisted_write(self):
        self.runtime.client = ScriptedClient(["request_refund", "query_billing", "request_refund"])
        task = self.runtime.create("alice", "请申请退款，重复扣款")
        result = await self.runtime.advance(task["id"], "alice", self.order["id"])
        self.assertFalse(result["attempts"][0]["success"])
        with patch.object(self.runtime, "_write_order", return_value=None):
            result = self.runtime.approve(task["id"], "alice", result["approvals"]["request_refund"]["id"], True, "r")
        self.assertNotEqual(result["status"], "completed")
        self.assertFalse(result["verification"]["billing"])


class OrderHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = ActionRuntime(self.tmp.name + "/state.sqlite3")

    def test_history_is_chronological_and_owner_isolated(self):
        self.runtime.record_order_event("alice", "order-2", "purchase", 99.0, "2026-02-01")
        self.runtime.record_order_event("alice", "order-1", "purchase", 199.0, "2026-01-01")
        self.runtime.record_order_event("alice", "order-1", "refund", 199.0, "2026-01-05")
        self.runtime.record_order_event("bob", "order-9", "purchase", 50.0, "2026-01-01")

        history = self.runtime.order_history("alice")
        self.assertEqual([e["order_id"] for e in history], ["order-1", "order-1", "order-2"])
        self.assertEqual([e["event_type"] for e in history], ["purchase", "refund", "purchase"])
        self.assertEqual(len(self.runtime.order_history("bob")), 1)
        self.assertEqual(self.runtime.order_history("nobody"), [])

    def test_rejects_unknown_event_type(self):
        with self.assertRaises(ValueError):
            self.runtime.record_order_event("alice", "order-1", "chargeback", 10.0, "2026-01-01")


class RouteTests(unittest.TestCase):
    def test_demo_approval_permissions(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.action_routes import router, runtime
        from tests.action_test_support import mint_token
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
            "AGENT_STATE_PATH": directory + "/state.sqlite3", "AGENT_JWT_SECRET": "test-signing-secret",
            "AGENT_USE_LLM": "0",
        }):
            runtime.cache_clear()
            app = FastAPI()
            app.include_router(router)
            with TestClient(app) as client:
                self.assertEqual(client.post("/agent/demo/orders").status_code, 401)
                headers = {"Authorization": "Bearer " + mint_token("user", "alice")}
                rows=client.post("/agent/demo/orders",headers=headers).json()["objects"]
                oid=next(o["id"] for o in rows if o["id"].endswith("pro"))
                case=client.post("/agent/tasks",headers=headers,json={"message":"申请退款 "+oid}).json()["commerce_case"]
                body={"approval_id":"old","approved":True}
                url="/agent/tasks/"+case["id"]+"/approval"
                self.assertEqual(client.post(url,headers=headers,json=body).status_code,403)
                result=client.post(url,headers={"Authorization":"Bearer "+mint_token("reviewer","bob")},json=body)
                self.assertEqual(result.status_code,410)
                self.assertEqual(client.get("/agent/tasks/"+case["id"],headers=headers).json()["status"],"awaiting_confirmation")
            runtime.cache_clear()
