"""Production runtime contract tests: consent is never automatically granted."""
import asyncio
import json
import tempfile
import time
import unittest
from unittest.mock import patch, AsyncMock

from agents.action_runtime import ActionRuntime
from agents.goal_interpreter import GOAL_MODES, GoalInterpreter, GoalProposal


class SemanticClient:
    def __init__(self, goals):
        self.goals = goals
        self.requests = []
        self.bad_first = False

    async def create_tool_turn(self, **kwargs):
        self.requests.append(kwargs)
        tool = kwargs["tools"][0]["name"]
        if tool == "interpret_goal":
            args = {"goals": self.goals}
            if self.bad_first and len(self.requests) == 1:
                args["authorized"] = True
        else:
            args = {"order_id": json.loads(kwargs["messages"][0]["content"])["order_id"]}
        return {"tool_calls": [{"id": "c" + str(len(self.requests)), "type": "function",
                                "function": {"name": tool, "arguments": json.dumps(args)}}]}


class SequencedClient(SemanticClient):
    """Like SemanticClient, but returns a different `goals` proposal on each
    successive interpret_goal call (then keeps returning the last one) -
    used to simulate a second, unrelated request arriving mid-task."""
    def __init__(self, sequence):
        super().__init__(sequence[0])
        self._sequence = list(sequence)
        self._interpret_calls = 0

    async def create_tool_turn(self, **kwargs):
        if kwargs["tools"][0]["name"] == "interpret_goal":
            idx = min(self._interpret_calls, len(self._sequence) - 1)
            self.goals = self._sequence[idx]
            self._interpret_calls += 1
        return await super().create_tool_turn(**kwargs)


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_pasted_current_control_ids_do_not_invalidate_or_authorize(self):
        task = await self.begin("请申请重复扣款退款")
        cid = task["confirmations"]["request_refund"]["id"]
        with self.assertRaisesRegex(ValueError, "确认编号"):
            await self.r.advance(task["id"], "alice", message=cid)
        self.assertEqual(self.r.get(task["id"], "alice"), task)
        self.r.confirm(task["id"], "alice", cid, True)
        task = await self.r.advance(task["id"], "alice")
        self.assertEqual(task["status"], "awaiting_approval")
        with self.assertRaisesRegex(ValueError, "审批编号"):
            await self.r.advance(task["id"], "alice", message=" " + task["approvals"]["request_refund"]["id"] + " ")
        self.assertEqual(self.r.get(task["id"], "alice"), task)
        self.assertEqual(self.order_state()["refunds"], 0)
        changed = await self.r.advance(task["id"], "alice", message="改成另外一笔订单")
        self.assertEqual(changed["status"], "needs_human")
        self.assertEqual(changed["approvals"]["request_refund"]["status"], "invalidated")
        still = await self.r.advance(task["id"], "alice", message=task["approvals"]["request_refund"]["id"])
        self.assertEqual(still["status"], "needs_human")

    async def test_unsupported_request_stops_before_order_or_tools(self):
        client = SemanticClient({})
        runtime = ActionRuntime(self.path, client=client, allow_fallback=False)
        task = runtime.create("alice", "咨询实物配送及退货")
        proposal = GoalProposal(goals={}, unsupported_requests=["实物配送", "退货政策"])
        with patch.object(runtime.goal_interpreter, "interpret", new=AsyncMock(return_value=(
                proposal, {"mode": "native_llm", "calls": 1, "usage": []}))):
            result = await runtime.advance(task["id"], "alice")
        self.assertEqual(result["status"], "needs_human")
        self.assertEqual(result["evidence"], [])
        self.assertEqual(result["actions"], [])
        self.assertNotIn("请提供订单号", result["response"])
        self.assertIn("无法核实", result["response"])

    async def test_query_response_contains_business_facts(self):
        result = await self.begin("查询订阅和账单")
        self.assertIn("pro", result["response"])
        self.assertIn("basic", result["response"])
        self.assertIn("扣款 2 笔", result["response"])

    def test_policy_without_answer_is_not_verified(self):
        from agents.action_policy import verify
        self.assertFalse(verify({"goals": {"policy": "read"}}, {}, {})["policy"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = self.temp.name + "/db"
        self.r = ActionRuntime(self.path)
        self.order = self.r.seed("alice")

    def order_state(self):
        with self.r.connect() as db:
            return json.loads(db.execute("SELECT body FROM orders WHERE id=?", (self.order["id"],)).fetchone()[0])

    def test_goal_schema_exposes_allowed_values_to_model(self):
        schema = GoalProposal.model_json_schema()["properties"]["goals"]
        self.assertIs(schema["additionalProperties"], False)
        for domain, modes in GOAL_MODES.items():
            self.assertEqual(schema["properties"][domain]["enum"], modes)

    async def test_single_attempt_mode_does_not_retry_provider(self):
        client = SemanticClient({"unknown": "write"})
        task = self.r.create("alice", "查询订阅")
        proposal, record = await GoalInterpreter(client, max_attempts=1).interpret("查询订阅", task)
        self.assertIsNone(proposal)
        self.assertEqual(record["calls"], 1)
        self.assertEqual(len(client.requests), 1)

    async def begin(self, message="请修复权益"):
        t = self.r.create("alice", message)
        return await self.r.advance(t["id"], "alice", self.order["id"])

    async def test_readonly_does_not_request_consent_or_modify(self):
        t = await self.begin("查询订阅和账单")
        self.assertEqual(t["status"], "completed")
        self.assertFalse(t["confirmations"])
        self.assertEqual(self.order_state(), self.order)

    async def test_write_waits_for_specific_user_confirmation(self):
        t = await self.begin()
        self.assertEqual(t["status"], "awaiting_confirmation")
        self.assertEqual(self.order_state()["entitlement"], "basic")
        self.assertFalse(t["actions"])
        self.assertEqual(t["confirmations"]["sync_entitlements"]["parameters"], {"order_id": self.order["id"], "target_plan": "pro"})
        unchanged = await self.r.advance(t["id"], "alice", message="好的")
        self.assertEqual(unchanged["status"], "awaiting_confirmation")
        self.r.confirm(t["id"], "alice", t["confirmations"]["sync_entitlements"]["id"], True)
        done = await self.r.advance(t["id"], "alice")
        self.assertEqual(done["status"], "completed")

    async def test_confirmation_does_not_replace_refund_approval(self):
        t = await self.begin("请申请退款")
        self.assertFalse(t["approvals"])
        self.r.confirm(t["id"], "alice", t["confirmations"]["request_refund"]["id"], True)
        t = await self.r.advance(t["id"], "alice")
        self.assertEqual(t["status"], "awaiting_approval")
        self.assertEqual(self.order_state()["refunds"], 0)
        done = self.r.approve(t["id"], "alice", t["approvals"]["request_refund"]["id"], True, "reviewer")
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.order_state()["refunds"], 1)

    async def test_composite_actions_need_separate_consents(self):
        # Two independent goals (repair entitlement, request a refund) get
        # proposed AS TWO SEPARATE, SIMULTANEOUS confirmations once their
        # read-only prerequisites are done - not one after the other.
        t = await self.begin("请修复权益并申请退款")
        self.assertEqual(t["status"], "awaiting_confirmation")
        self.assertEqual(set(t["confirmations"]), {"sync_entitlements", "request_refund"})
        entitlement_id = t["confirmations"]["sync_entitlements"]["id"]
        refund_id = t["confirmations"]["request_refund"]["id"]
        self.r.confirm(t["id"], "alice", entitlement_id, True)
        t = await self.r.advance(t["id"], "alice")
        # Confirming one does not disturb the other, still-pending one.
        self.assertEqual(t["status"], "awaiting_confirmation")
        self.assertEqual(t["confirmations"]["request_refund"]["id"], refund_id)
        self.assertEqual(self.order_state()["entitlement"], "pro")
        self.assertEqual(self.order_state()["refunds"], 0)
        with self.assertRaises(ValueError):
            self.r.confirm(t["id"], "alice", entitlement_id, False)  # already accepted; can't now decline it
        self.r.confirm(t["id"], "alice", refund_id, True)
        t = await self.r.advance(t["id"], "alice")
        self.assertEqual(t["status"], "awaiting_approval")

    async def test_rejection_foreign_owner_and_forged_id(self):
        t = await self.begin()
        with self.assertRaises(ValueError):
            self.r.confirm(t["id"], "bob", t["confirmations"]["sync_entitlements"]["id"], True)
        with self.assertRaises(ValueError):
            self.r.confirm(t["id"], "alice", "fake", True)
        with self.assertRaises(ValueError):
            self.r.confirm(t["id"], "alice", t["confirmations"]["sync_entitlements"]["id"], "true")
        denied = self.r.confirm(t["id"], "alice", t["confirmations"]["sync_entitlements"]["id"], False)
        self.assertEqual(denied["status"], "cancelled")
        self.assertEqual(await self.r.advance(t["id"], "alice"), denied)
        self.assertEqual(self.order_state(), self.order)

    async def test_expiry_and_business_change_fail_closed(self):
        t = await self.begin()
        with patch("agents.action_runtime.time.time", return_value=time.time() + 2000):
            t = self.r.confirm(t["id"], "alice", t["confirmations"]["sync_entitlements"]["id"], True)
        self.assertEqual(t["status"], "needs_human")
        t = await self.begin()
        with self.r.connect() as db:
            self.r._write_order(db, t, self.order_state())
        t = self.r.confirm(t["id"], "alice", t["confirmations"]["sync_entitlements"]["id"], True)
        self.assertEqual(t["confirmations"]["sync_entitlements"]["status"], "invalidated")

    async def test_revision_and_new_message_invalidate_pending_consent(self):
        t = await self.begin()
        old_id = t["confirmations"]["sync_entitlements"]["id"]
        t = await self.r.advance(t["id"], "alice", message="改成另一张订单")
        self.assertEqual(t["confirmations"]["sync_entitlements"]["status"], "invalidated")
        self.r.confirm(t["id"], "alice", old_id, True)
        self.assertEqual(self.order_state(), self.order)
        new = self.r.revise(t["id"], "alice", "查询账单")
        self.assertFalse(new["confirmations"])
        self.assertNotEqual(new["id"], t["id"])

    async def test_replay_and_restart_do_not_repeat_write(self):
        t = await self.begin()
        r = ActionRuntime(self.path)
        c = t["confirmations"]["sync_entitlements"]["id"]
        r.confirm(t["id"], "alice", c, True)
        r.confirm(t["id"], "alice", c, True)
        results = await asyncio.gather(r.advance(t["id"], "alice"), r.advance(t["id"], "alice"))
        self.assertEqual(results[-1]["status"], "completed")
        self.assertEqual(len(r.get(t["id"], "alice")["actions"]), 1)

    async def test_native_understanding_handles_non_keyword_request(self):
        client = SemanticClient({"entitlement": "repair"})
        self.r = ActionRuntime(self.path, client=client, allow_fallback=False)
        t = await self.begin("付了高级版的钱却还是旧权限，请帮我处理好")
        self.assertEqual(t["goals"], {"entitlement": "repair"})
        self.assertEqual(t["interpretations"][0]["mode"], "native_llm")
        self.assertEqual(t["status"], "awaiting_confirmation")
        self.assertFalse(t["actions"])

    async def test_native_goal_schema_rejects_model_authorization_and_retries(self):
        client = SemanticClient({"entitlement": "repair"})
        client.bad_first = True
        self.r = ActionRuntime(self.path, client=client, allow_fallback=False)
        t = await self.begin()
        self.assertEqual(t["interpretations"][0]["calls"], 2)
        self.assertEqual(t["status"], "awaiting_confirmation")
        self.assertEqual(self.order_state(), self.order)

    async def test_native_failure_never_falls_back_to_writes(self):
        class Bad:
            async def create_tool_turn(self, **kwargs):
                raise TimeoutError()
        self.r = ActionRuntime(self.path, client=Bad(), allow_fallback=True)
        t = await self.begin()
        self.assertEqual(t["status"], "needs_human")
        self.assertEqual(t["interpretations"][0]["mode"], "failed")
        self.assertEqual(t["fallback_count"], 0)
        self.assertEqual(self.order_state(), self.order)

    async def test_context_preserves_goal_on_order_supplement(self):
        client = SemanticClient({"entitlement": "repair"})
        self.r = ActionRuntime(self.path, client=client, allow_fallback=False)
        t = self.r.create("alice", "请修复权益")
        t = await self.r.advance(t["id"], "alice")
        self.assertEqual(t["status"], "awaiting_clarification")
        t = await self.r.advance(t["id"], "alice", message="订单号 " + self.order["id"])
        context = json.loads(client.requests[1]["messages"][0]["content"])
        self.assertEqual(context["current_goals"], {"entitlement": "repair"})
        self.assertTrue(context["recent_messages"])
        self.assertEqual(t["status"], "awaiting_confirmation")

    async def test_unknown_goal_and_fabricated_order_are_blocked(self):
        client = SemanticClient({"account": "freeze"})
        self.r = ActionRuntime(self.path, client=client)
        t = await self.begin()
        self.assertEqual(t["status"], "needs_human")
        self.assertEqual(self.order_state(), self.order)
        order_id = self.order["id"]
        class Invented(SemanticClient):
            async def create_tool_turn(self, **kwargs):
                turn = await super().create_tool_turn(**kwargs)
                turn["tool_calls"][0]["function"]["arguments"] = json.dumps({"goals": self.goals, "order_reference": order_id})
                return turn
        self.r = ActionRuntime(self.path, client=Invented({"billing": "read"}))
        t = self.r.create("alice", "查询账单")
        t = await self.r.advance(t["id"], "alice")
        self.assertEqual(t["status"], "needs_human")
        self.assertFalse(t["evidence"])

    async def test_parameter_tampering_and_replanning_invalidate_consent(self):
        from agents.action_policy import plan_for
        t = await self.begin()
        with self.r.connect() as db:
            t["confirmations"]["sync_entitlements"]["parameters"]["target_plan"] = "enterprise"
            self.r._save(db, t)
        t = self.r.confirm(t["id"], "alice", t["confirmations"]["sync_entitlements"]["id"], True)
        self.assertEqual(t["confirmations"]["sync_entitlements"]["status"], "invalidated")
        t = await self.begin()
        old_id = t["confirmations"]["sync_entitlements"]["id"]
        self.r.confirm(t["id"], "alice", old_id, True)
        self.r.replace_plan(t["id"], "alice", plan_for(t))
        t = await self.r.advance(t["id"], "alice")
        self.assertEqual(t["status"], "awaiting_confirmation")
        self.assertNotEqual(t["confirmations"]["sync_entitlements"]["id"], old_id)
        self.assertEqual(self.order_state(), self.order)

    async def test_disjoint_request_runs_concurrently_not_serially_while_awaiting_approval(self):
        # Reproduces the reported gap: a genuinely unrelated request (cancel
        # renewal) arriving while an unrelated refund is still awaiting
        # reviewer approval must not be treated as a conflicting business
        # change, AND must not have to wait for the refund to resolve before
        # the user even sees its own confirmation prompt - real products
        # don't serialize requests from unrelated domains.
        client = SequencedClient([{"billing": "refund"}, {"renewal": "cancel"}])
        self.r = ActionRuntime(self.path, client=client, allow_fallback=False)
        t = await self.begin("请申请重复扣款退款")
        self.assertEqual(t["goals"], {"billing": "refund"})
        self.r.confirm(t["id"], "alice", t["confirmations"]["request_refund"]["id"], True)
        t = await self.r.advance(t["id"], "alice")
        self.assertEqual(t["status"], "awaiting_approval")
        approval_id = t["approvals"]["request_refund"]["id"]

        # The disjoint request arrives WHILE the refund still awaits Bob.
        t = await self.r.advance(t["id"], "alice", message="帮我关闭自动续费")
        # It shows up immediately as its OWN confirmation - it does not wait
        # for the refund approval to resolve first.
        self.assertEqual(t["status"], "awaiting_confirmation")
        self.assertEqual(t["confirmations"]["cancel_renewal"]["status"], "pending")
        self.assertEqual(t["approvals"]["request_refund"]["status"], "pending")
        self.assertEqual(t["approvals"]["request_refund"]["id"], approval_id)
        self.assertEqual(t["goals"], {"billing": "refund", "renewal": "cancel"})

        # The user confirms the renewal cancellation WHILE the refund is
        # still sitting with the reviewer - the branches don't block each
        # other, in either direction.
        self.r.confirm(t["id"], "alice", t["confirmations"]["cancel_renewal"]["id"], True)
        t = await self.r.advance(t["id"], "alice")
        self.assertEqual(self.order_state()["auto_renew"], False)
        self.assertEqual(self.order_state()["refunds"], 0)  # refund still not approved yet
        self.assertEqual(t["status"], "awaiting_approval")  # only the refund is left pending
        self.assertEqual(t["approvals"]["request_refund"]["id"], approval_id)

        self.r.approve(t["id"], "alice", approval_id, True, "bob")
        final = await self.r.advance(t["id"], "alice")
        self.assertEqual(final["status"], "completed")
        self.assertEqual(self.order_state()["refunds"], 1)

    async def test_conflicting_change_on_same_goal_still_invalidates_while_awaiting_confirmation(self):
        # A message that maps to the SAME goal key with a DIFFERENT mode is a
        # genuine conflict, not an independent side-request, so it must still
        # fail closed exactly like before.
        client = SequencedClient([{"entitlement": "repair"}, {"entitlement": "read"}])
        self.r = ActionRuntime(self.path, client=client, allow_fallback=False)
        t = await self.begin("请修复权益")
        self.assertEqual(t["status"], "awaiting_confirmation")
        t = await self.r.advance(t["id"], "alice", message="不用修复了，查一下就行")
        self.assertEqual(t["status"], "needs_human")
        self.assertEqual(t["confirmations"]["sync_entitlements"]["status"], "invalidated")

    async def test_two_order_candidates_require_explicit_selection(self):
        other = self.r.seed("alice")
        t = self.r.create("alice", "查询账单 " + self.order["id"] + " " + other["id"])
        t = await self.r.advance(t["id"], "alice")
        self.assertEqual(t["status"], "awaiting_clarification")
        self.assertIsNone(t["order_id"])
        t = await self.r.advance(t["id"], "alice", order_id=self.order["id"])
        self.assertEqual(t["status"], "completed")


class ConfirmationAPITests(unittest.TestCase):
    def test_confirmation_endpoint_auth_and_strict_body(self):
        from fastapi.testclient import TestClient
        from api.action_demo import app
        from api.action_routes import runtime
        from tests.action_test_support import mint_token
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
            "AGENT_STATE_PATH": directory + "/db", "AGENT_JWT_SECRET": "test-signing-secret", "AGENT_USE_LLM": "0"}):
            runtime.cache_clear()
            self.addCleanup(runtime.cache_clear)
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer " + mint_token("user", "alice")}
                order = client.post("/agent/demo/orders", headers=headers).json()
                t = client.post("/agent/tasks", headers=headers, json={"message": "请申请退款"}).json()
                base = "/agent/tasks/" + t["id"]
                t = client.post(base + "/continue", headers=headers, json={"order_id": order["id"]}).json()
                body = {"confirmation_id": t["confirmations"]["request_refund"]["id"], "accepted": True}
                self.assertEqual(client.post(base + "/confirmation", json=body).status_code, 401)
                self.assertEqual(client.post(base + "/confirmation", headers={"Authorization": "Bearer " + mint_token("reviewer", "bob")}, json=body).status_code, 401)
                self.assertEqual(client.post(base + "/confirmation", headers=headers, json={**body, "approved": True}).status_code, 422)
                t = client.post(base + "/confirmation", headers=headers, json=body).json()
                self.assertEqual(t["status"], "awaiting_approval")
