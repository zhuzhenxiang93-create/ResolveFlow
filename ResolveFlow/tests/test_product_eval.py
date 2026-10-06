"""Product Evaluation harness, grading and read-only report API; plus regressions for the
conversation fixes it motivated. All offline, no model calls."""
import copy
import json
import logging
import os
import tempfile
import unittest
from collections import Counter
from unittest.mock import patch

from evaluation import bad_case_analyzer, product_evaluator, product_metrics


class DatasetTests(unittest.TestCase):
    def test_main_dataset_shape(self):
        cases = product_evaluator.load_cases()
        self.assertEqual(len(cases), 80)
        self.assertEqual(Counter(c["category"] for c in cases), {
            "single_task": 12, "disambiguation": 10, "multi_intent": 10, "multi_turn": 10, "business_rule": 8,
            "confirmation_approval": 10, "idempotency_state": 8, "authorization": 7, "policy_technical": 5})
        for c in cases:
            for key in ("fixture", "turns", "expected_goals", "requires_clarification", "requires_confirmation",
                        "requires_review", "expected_final_state", "max_user_turns"):
                self.assertIn(key, c, c["id"])

    def test_holdout_is_separate(self):
        main = {c["id"] for c in product_evaluator.load_cases()}
        holdout = {c["id"] for c in product_evaluator.load_cases(product_evaluator.HOLDOUT)}
        self.assertEqual(len(holdout), 16)
        self.assertFalse(main & holdout)


class HarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        ids = {"PE-S01", "PE-C02", "PE-A07", "PE-D03", "PE-I01", "PE-C07"}
        cls.mode, cls.results = product_evaluator.run([c for c in product_evaluator.load_cases() if c["id"] in ids])
        cls.by_id = {r["case"]["id"]: r for r in cls.results}

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)

    def test_replays_real_chain_offline(self):
        self.assertEqual(self.mode, "offline-rules")
        for r in self.results:
            grade = product_metrics.grade_case(r)
            self.assertTrue(grade["passed"], (r["case"]["id"], grade["failed_metrics"]))

    def test_success_comes_from_sqlite_and_audit(self):
        r = self.by_id["PE-S01"]
        refunds = r["final"]["headphones"]["refunds"]
        self.assertEqual([(x["amount_minor"], x["status"]) for x in refunds], [(25900, "succeeded_simulated")])
        writes = product_metrics.compliance_writes(r)
        self.assertTrue(writes and all(w["compliant"] for w in writes))

    def test_double_click_creates_one_refund(self):
        self.assertEqual(len(self.by_id["PE-I01"]["final"]["headphones"]["refunds"]), 1)

    def test_attacks_are_recorded_and_blocked(self):
        attacks = self.by_id["PE-A07"]["attacks"]
        self.assertEqual({a["attack"] for a in attacks}, {"cross_user_decide", "decision_field_injection"})
        self.assertTrue(all(a["blocked"] and not a["state_changed"] for a in attacks))

    def test_unconfirmed_write_is_flagged(self):
        r = copy.deepcopy(self.by_id["PE-C02"])
        r["final"]["headphones"]["refunds"].append({"id": "R-forged", "amount_minor": 25900, "status": "refund_processing",
                                                    "item_id": None, "payment_id": "x", "idempotency_key": "no-such-action", "created": 1})
        grade = product_metrics.grade_case(r)
        self.assertFalse(grade["passed"])
        self.assertFalse(grade["checks"]["confirmation"])
        self.assertEqual(bad_case_analyzer.classify(r, grade)[0], "CONFIRMATION_VIOLATION")

    def test_text_reply_is_not_consent(self):
        r = self.by_id["PE-C07"]
        self.assertEqual([a["status"] for a in r["actions"]], ["awaiting_confirmation"])


class EvalApiTests(unittest.TestCase):
    def test_read_only_endpoints(self):
        from fastapi.testclient import TestClient
        from api import commerce_routes
        from api.portfolio_demo import create_app
        saved = commerce_routes.runtime, commerce_routes.conversation_service
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"RESOLVEFLOW_DEMO_MODE": "true", "RESOLVEFLOW_DEMO_DIR": root, "AGENT_USE_LLM": "0"}):
            try:
                client = TestClient(create_app())
                latest = client.get("/eval/product/latest")
                if latest.status_code == 404:
                    self.skipTest("no generated report in this checkout")
                body = latest.json()
                self.assertEqual(body["current"]["dataset"]["case_count"], 80)
                self.assertIn("task_success_rate", body["current"]["metrics"])
                listed = client.get("/eval/product/cases", params={"run": "current"}).json()
                self.assertEqual(listed["count"], 80)
                one = client.get("/eval/product/cases/" + listed["cases"][0]["id"]).json()
                self.assertIn("trace", one["case"])
                self.assertEqual(client.get("/eval/product/cases/NOPE").status_code, 404)
                self.assertEqual(client.post("/eval/product/latest").status_code, 405)
            finally:
                commerce_routes.runtime, commerce_routes.conversation_service = saved


class ConversationFixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from agents.conversation_service import ConversationService
        from business.execution import ExecutionContext
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = ExecutionContext(self.tmp.name + "/db.sqlite3")
        self.rows = self.runtime.store.seed("alice")
        self.s = ConversationService(self.runtime)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def oid(self, slug):
        return next(o["id"] for o in self.rows if o["id"].endswith(slug))

    async def test_typed_ordinal_resolves_displayed_candidate(self):
        r = await self.s.send("alice", "我要退款")
        r = await self.s.send("alice", "第二个", conversation_id=r["conversation_id"])
        self.assertEqual(r["commerce_case"]["operations"][0]["quote"]["object_id"], self.oid("headphones"))

    async def test_multi_object_clause_and_policy_question(self):
        r = await self.s.send("alice", "无线耳机和蓝牙音箱都退掉")
        self.assertEqual({a["quote"]["object_id"] for a in r["commerce_case"]["operations"]}, {self.oid("headphones"), self.oid("speaker")})
        r = await self.s.send("alice", "Basic 会员别续了，另外会员退款政策是什么")
        self.assertEqual(r["commerce_case"]["operations"][0]["quote"]["operation"], "cancel_renewal")
        self.assertIn("subscription-commerce-refund", {x["document_id"] for x in r["sources"]})

    async def test_withdrawal_cancels_only_pending(self):
        r = await self.s.send("alice", "帮我把无线耳机退掉")
        case = r["commerce_case"]
        await self.s.send("alice", "还是算了", conversation_id=r["conversation_id"])
        self.assertEqual(self.runtime.store.get_case("alice", case["id"])["operations"][0]["status"], "cancelled")

    async def test_item_scope_after_only(self):
        r = await self.s.send("alice", "机械键盘套装里我只退鼠标")
        self.assertEqual(r["commerce_case"]["operations"][0]["quote"]["amount_minor"], 9000)

    async def test_already_closed_renewal_is_ineligible(self):
        basic = self.runtime.store.detail("alice", self.oid("basic"))
        quote = self.runtime.store.quote({**basic, "auto_renew": False}, "cancel_renewal")
        self.assertFalse(quote["eligible"])

    async def test_offline_error_code_uses_read_only_tool(self):
        r = await self.s.send("alice", "接口返回 429 是什么意思")
        self.assertEqual(r["support_result"]["tools_used"], ["lookup_error_code"])
        self.assertIn("限流", r["response"])

    async def test_digits_inside_object_id_are_not_error_codes(self):
        from business.conversation import CommerceConversation
        catalog = [{"id": "S-a432b-pro", "title": "Pro 月度会员", "domain": "subscription", "items": [], "payments": []}]
        proposal = CommerceConversation.rules("申请退款 S-a432b-pro", catalog)
        self.assertEqual([i.operation for i in proposal.intents], ["refund"])
        self.assertEqual(proposal.general_remainder, "")

    async def test_narrowed_refund_replaces_unconfirmed_full_order_card(self):
        # Found on the public demo: "退机械键盘" then "我只想退键盘" left a ¥399 and a ¥299 card open.
        first = await self.s.send("alice", "退机械键盘")
        second = await self.s.send("alice", "我只想退键盘", conversation_id=first["conversation_id"])
        old = self.runtime.store.get_case("alice", first["commerce_case"]["id"])["operations"][0]
        new = second["commerce_case"]["operations"][0]
        self.assertEqual(old["status"], "cancelled")
        self.assertEqual((new["quote"]["amount_minor"], new["status"]), (29900, "awaiting_confirmation"))
        self.assertIn("已替换之前未确认的申请", second["response"])

    async def test_identical_repeat_keeps_both_cards(self):
        a = await self.s.send("alice", "帮我把无线耳机退掉")
        b = await self.s.send("alice", "帮我把无线耳机退掉", conversation_id=a["conversation_id"])
        self.assertEqual(self.runtime.store.get_case("alice", a["commerce_case"]["id"])["operations"][0]["status"], "awaiting_confirmation")
        self.assertEqual(b["commerce_case"]["operations"][0]["status"], "awaiting_confirmation")


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from memory.local_conversation_memory import LocalConversationMemory
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = LocalConversationMemory(self.tmp.name + "/m.sqlite3")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_one_time_codes_and_card_numbers_are_redacted(self):
        from memory.conversation_memory import MsgRole
        await self.mem.add_message("u", "c", MsgRole.USER, "我的验证码是 382915，卡号 6222020200112233445")
        context = await self.mem.get_context("u", "c")
        stored = context.recent_messages[0].content
        self.assertNotIn("382915", stored)
        self.assertNotIn("6222020200112233445", stored)

    async def test_latest_standing_preference_wins_and_temporary_is_ignored(self):
        from memory.conversation_memory import MsgRole
        for text in ["以后回复请简短一点", "我还是喜欢详细一点的回答", "这次简洁点"]:
            await self.mem.add_message("u", "c", MsgRole.USER, text)
        await self.mem.update_profile("u", "c")
        self.assertEqual((await self.mem.get_profile("u"))["response_style"], "detailed")
