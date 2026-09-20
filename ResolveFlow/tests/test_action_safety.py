"""Fault injection tests; these validate runtime contracts, not model quality."""
import asyncio
import copy
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from agents.action_policy import plan_for, validate_plan
from action_test_support import ActionRuntime


class Client:
    def __init__(self, rounds):
        self.rounds = iter(rounds)
        self.requests = []

    async def create_tool_turn(self, **kwargs):
        self.requests.append(kwargs)
        result = next(self.rounds)
        return result(kwargs) if callable(result) else result


def calls(*items):
    return {"tool_calls": [{"id": cid, "function": {"name": name, "arguments": args},
                            "type": "function"} for cid, name, args in items]}


class SafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = self.tmp.name + "/db"
        self.r = ActionRuntime(self.path, allow_fallback=False)
        self.order = self.r.seed("alice")
        self.args = json.dumps({"order_id": self.order["id"]})

    def state(self):
        with self.r.connect() as db:
            return json.loads(db.execute("SELECT body FROM orders WHERE id=?", (self.order["id"],)).fetchone()[0])

    async def run_task(self, message):
        t = self.r.create("alice", message)
        return await self.r.advance(t["id"], "alice", self.order["id"])

    async def test_readonly_and_negation_never_modify(self):
        for message in ("查询订阅和账单", "重复扣款", "不要修复权益，只查询账单", "不需要退款，检查重复扣款",
                        "如何修复权益", "请问能申请退款吗"):
            with self.subTest(message=message):
                t = await self.run_task(message)
                self.assertEqual(t["status"], "completed")
                self.assertFalse(t["actions"])
                self.assertIsNone(t["approval"])
                self.assertEqual(self.state(), self.order)

    async def test_model_cannot_add_write_permission(self):
        self.r.client = Client([calls(("a", "sync_entitlements", self.args)), {"tool_calls": []}])
        t = await self.run_task("查询订阅")
        self.assertEqual(t["attempts"][0]["error_type"], "permission_denied")
        self.assertEqual(self.state(), self.order)

    async def test_argument_correction_and_partial_batch_results(self):
        self.r.client = Client([calls(("a", "query_subscription", "{"), ("b", "check_service", self.args)),
                                calls(("c", "query_subscription", self.args)),
                                calls(("d", "sync_entitlements", self.args))])
        t = await self.run_task("请修复权益")
        self.assertEqual(t["status"], "completed")
        self.assertEqual(t["attempts"][0]["error_type"], "parameter_error")
        self.assertTrue(t["attempts"][1]["success"])
        self.assertEqual([m["tool_call_id"] for m in t["tool_messages"] if m["role"] == "tool"], ["a", "b", "c", "d"])
        self.assertTrue(t["handoffs"])

    async def test_extra_identity_and_duplicate_ids(self):
        bad = json.dumps({"order_id": self.order["id"], "owner": "admin", "approved": True})
        self.r.client = Client([calls(("a", "query_billing", bad)),
                                calls(("a", "query_billing", self.args)),
                                calls(("b", "query_billing", self.args))])
        t = await self.run_task("查询账单")
        self.assertEqual(t["status"], "completed")
        self.assertEqual([x.get("error_type") for x in t["attempts"][:2]], ["parameter_error", "protocol_error"])
        self.assertEqual(len([m for m in t["tool_messages"] if m["role"] == "tool"]), 2)

    async def test_clarification_natural_language_and_conversation_binding(self):
        t = self.r.create("alice", "查询账单", "conversation-a")
        with self.assertRaises(ValueError):
            await self.r.advance(t["id"], "alice", conversation_id="conversation-b")
        t = await self.r.advance(t["id"], "alice", message="订单号是 " + self.order["id"])
        self.assertEqual(t["status"], "completed")
        self.assertEqual(t["conversation_id"], "conversation-a")

    async def test_correction_invalidates_old_approval(self):
        t = await self.run_task("请申请退款")
        new = self.r.revise(t["id"], "alice", "只查询账单")
        self.assertEqual(self.r.get(t["id"], "alice")["status"], "superseded")
        old = self.r.approve(t["id"], "alice", t["approval"]["id"], True, "reviewer")
        self.assertEqual(old["approval"]["status"], "invalidated")
        self.assertNotEqual(new["id"], t["id"])
        self.assertEqual(new["conversation_id"], t["conversation_id"])
        self.assertEqual(self.state()["refunds"], 0)

    async def test_expired_and_changed_business_approval(self):
        for failure in ("expiry", "business", "binding"):
            with self.subTest(failure=failure):
                t = await self.run_task("请申请退款")
                if failure == "expiry":
                    with patch("agents.action_runtime.time.time", return_value=time.time() + 2000):
                        result = self.r.approve(t["id"], "alice", t["approval"]["id"], True, "reviewer")
                else:
                    with self.r.connect() as db:
                        if failure == "business":
                            self.r._write_order(db, t, self.state())
                        else:
                            t["approval"]["count"] = 99
                            self.r._save(db, t)
                    result = self.r.approve(t["id"], "alice", t["approval"]["id"], True, "reviewer")
                self.assertEqual(result["approval"]["status"], "invalidated")
                self.assertEqual(self.state()["refunds"], 0)

    async def test_plan_change_invalidates_approval_and_cyclic_plan_rejected(self):
        t = await self.run_task("请申请退款")
        steps = plan_for(t)
        bad = copy.deepcopy(steps)
        bad[0]["dependencies"] = [bad[-1]["id"]]
        with self.assertRaises(ValueError):
            validate_plan(bad, t)
        changed = self.r.replace_plan(t["id"], "alice", steps)
        self.assertIsNone(changed["approval"])
        self.assertEqual(changed["approval_history"][0]["status"], "invalidated")
        with self.assertRaises(ValueError):
            self.r.approve(t["id"], "alice", t["approval"]["id"], True, "reviewer")

    async def test_native_replanning_affects_execution_order(self):
        def propose(kwargs):
            context = json.loads(kwargs["messages"][0]["content"])
            steps = list(reversed(plan_for(context)))
            return calls(("plan", "propose_plan", json.dumps({"steps": steps, "reason": "Check billing before technical diagnosis"})))
        self.r.client = Client([propose, calls(("bill", "query_billing", self.args)),
                                calls(("subscription", "query_subscription", self.args))])
        t = await self.run_task("查询订阅和账单")
        self.assertEqual(t["status"], "completed")
        self.assertEqual(t["replans"], 1)
        self.assertEqual(t["evidence"][0]["tool"], "query_billing")

    async def test_write_response_loss_reconciled_without_retry(self):
        original = self.r._write_order
        def lost(db, task, order):
            original(db, task, order)
            raise TimeoutError("response lost")
        with patch.object(self.r, "_write_order", side_effect=lost) as write:
            t = await self.run_task("请修复权益")
        self.assertEqual(t["status"], "completed")
        self.assertEqual(write.call_count, 1)
        self.assertTrue(t["evidence"][-1]["data"]["reconciled_after_timeout"])

    async def test_false_write_success_not_business_completion(self):
        with patch.object(self.r, "_write_order", return_value=None):
            t = await self.run_task("请修复权益")
        self.assertEqual(t["status"], "needs_human")
        self.assertFalse(t["verification"]["entitlement"])
        self.assertEqual(self.state()["entitlement"], "basic")

    async def test_failed_dependency_and_partial_completion(self):
        self.order = self.r.seed("alice", service="down")
        t = await self.run_task("请修复权益并申请退款")
        self.assertEqual(t["status"], "awaiting_approval")
        t = self.r.approve(t["id"], "alice", t["approval"]["id"], True, "reviewer")
        t = await self.r.advance(t["id"], "alice")
        self.assertEqual(t["status"], "needs_human")
        self.assertEqual(t["verification"], {"entitlement": False, "billing": True})
        self.assertNotIn("sync_entitlements", [a["tool"] for a in t["actions"]])

    async def test_active_lease_prevents_concurrent_model_execution(self):
        started, release = asyncio.Event(), asyncio.Event()
        class Blocking:
            async def create_tool_turn(inner, **kwargs):
                started.set()
                await release.wait()
                return calls(("a", "query_billing", self.args))
        self.r.client = Blocking()
        t = self.r.create("alice", "查询账单")
        first = asyncio.create_task(self.r.advance(t["id"], "alice", self.order["id"]))
        await started.wait()
        second = await ActionRuntime(self.path).advance(t["id"], "alice")
        self.assertEqual(second["model_calls"], 1)
        release.set()
        self.assertEqual((await first)["status"], "completed")

    async def test_restart_expired_lease_and_transcript(self):
        t = self.r.create("alice", "请申请退款")
        with self.r.connect() as db:
            t.update(order_id=self.order["id"], status="running")
            self.r._execute(db, t, "query_billing")
            t["tool_messages"] = [{"role": "assistant", **calls(("before", "query_billing", self.args))},
                                  {"role": "tool", "tool_call_id": "before", "content": "{}"}]
            self.r._save(db, t)
            db.execute("INSERT INTO leases VALUES (?, ?, ?)", (t["id"], "dead-process", 0))
        client = Client([calls(("after", "request_refund", self.args))])
        restarted = ActionRuntime(self.path, client=client, allow_fallback=False)
        result = await restarted.advance(t["id"], "alice")
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual(client.requests[0]["messages"][2]["tool_call_id"], "before")

    async def test_tool_timeout_retries_read_with_bounded_budget(self):
        original = self.r._execute
        attempts = []
        def transient(*args):
            attempts.append(1)
            if len(attempts) == 1:
                raise TimeoutError()
            return original(*args)
        with patch.object(self.r, "_execute", side_effect=transient):
            t = await self.run_task("查询账单")
        self.assertEqual(t["status"], "completed")
        self.assertEqual(t["attempts"][0]["error_type"], "tool_timeout")

    async def test_no_hidden_fallback(self):
        self.r.client = Client([])
        t = await self.run_task("查询账单")
        self.assertEqual(t["status"], "needs_human")
        self.assertEqual(t["fallback_count"], 0)
        self.assertFalse(t["evidence"])

    async def test_cancel_and_self_approval(self):
        t = await self.run_task("请申请退款")
        with self.assertRaises(ValueError):
            self.r.approve(t["id"], "alice", t["approval"]["id"], True, "alice")
        cancelled = self.r.cancel(t["id"], "alice")
        self.assertEqual(await self.r.advance(t["id"], "alice"), cancelled)
        self.assertEqual(self.state()["refunds"], 0)

    async def test_service_and_subscription_query_each_verified(self):
        t = await self.run_task("查询订阅和服务状态")
        self.assertEqual(t["status"], "completed")
        self.assertEqual(t["verification"], {"entitlement": True, "service": True})

    async def test_human_release_requires_explicit_review(self):
        self.r.client = Client([{}])
        t = await self.run_task("查询账单")
        with self.assertRaises(ValueError):
            self.r.release_handoff(t["id"], "alice", "alice")
        self.r.release_handoff(t["id"], "alice", "reviewer")
        self.r.client = None
        t = await self.r.advance(t["id"], "alice")
        self.assertEqual(t["status"], "completed")

    async def test_stale_evidence_requires_new_query(self):
        t = self.r.create("alice", "请申请退款")
        with self.r.connect() as db:
            t.update(order_id=self.order["id"], status="running")
            self.r._execute(db, t, "query_billing")
            t["evidence"][0]["timestamp"] = 0
            self.r._save(db, t)
        t = await self.r.advance(t["id"], "alice")
        self.assertEqual([e["tool"] for e in t["evidence"]].count("query_billing"), 2)
