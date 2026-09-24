import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock

from agents.action_runtime import ActionRuntime
from agents.conversation_service import ConversationService
from agents.goal_interpreter import GoalProposal
from memory.conversation_memory import MsgRole


class Understanding:
    client = True

    def __init__(self):
        self.contexts = []

    async def interpret(self, message, context):
        self.contexts.append(context)
        if "退订" in message or "政策" in message:
            goals, topics = {"policy": "read"}, ["renewal"]
        elif "那就" in message or "关闭" in message:
            goals, topics = {"renewal": "cancel"}, []
        elif "查询" in message:
            goals, topics = {"renewal": "read"}, []
        else:
            goals, topics = context.get("goals", {}), []
        return GoalProposal(goals=goals, policy_topics=topics), {"mode": "mock", "calls": 0, "usage": []}


class UnifiedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.r = ActionRuntime(self.temp.name + "/db", allow_fallback=False)
        self.r.goal_interpreter = Understanding()
        self.s = ConversationService(self.r)
        self.order = self.r.seed("user")

    async def test_consult_then_action_shared_memory_and_no_duplicate_interpretation(self):
        first = await self.s.send("user", "怎么退订")
        self.assertTrue(first["knowledge_used"])
        self.assertIsNone(first["task"])
        conv = first["conversation_id"]
        second = await self.s.send("user", "那就帮我关掉", conversation_id=conv)
        self.assertEqual(second["status"], "awaiting_clarification")
        seen = self.r.goal_interpreter.contexts[-1]["memory_context"]["recent_messages"]
        self.assertTrue(any("怎么退订" in m["content"] for m in seen))
        third = await self.s.send("user", self.order["id"], conversation_id=conv)
        self.assertEqual(third["status"], "awaiting_confirmation")
        self.assertEqual(len(self.r.goal_interpreter.contexts), 3)
        task = third["task"]
        before = self.r.get(task["id"], "user")
        aside = await self.s.send("user", "再解释一下退订政策", conversation_id=conv)
        self.assertEqual(aside["route"], "knowledge")
        self.assertEqual(self.r.get(task["id"], "user"), before)
        yes = await self.s.send("user", "好的", conversation_id=conv)
        self.assertEqual(yes["status"], "awaiting_confirmation")
        self.assertEqual(yes["task"]["actions"], [])
        self.r.confirm(task["id"], "user", task["confirmations"]["cancel_renewal"]["id"], True)
        done = await self.r.advance(task["id"], "user")
        await self.s.observe(done)
        self.assertEqual(done["status"], "completed")
        again = await self.s.send("user", "查询这笔订单的续费状态", conversation_id=conv)
        self.assertEqual(again["status"], "completed")
        self.assertNotEqual(again["task"]["id"], done["id"])
        self.assertEqual(again["task"]["actions"], [])

    async def test_conversation_owner_and_profile_isolation(self):
        result = await self.s.send("user", "请简洁回答，退订政策")
        conv = result["conversation_id"]
        with self.assertRaises(ValueError):
            await self.s.send("intruder", "查询", conversation_id=conv)
        profile = (await self.s.memory.get_context("user", conv)).user_profile
        self.assertEqual(profile["response_style"], "concise")
        other = await self.s.memory.get_context("intruder", "other")
        self.assertEqual(other.user_profile, {})
        self.assertEqual(other.relevant_history, [])

    async def test_restart_and_history_retrieval(self):
        first = await self.s.send("user", "怎么退订")
        restarted = ConversationService(ActionRuntime(self.r.path))
        restored = await restarted.memory.get_context("user", first["conversation_id"])
        self.assertGreaterEqual(len(restored.recent_messages), 2)
        history = await restarted.memory.get_context("user", "new-conversation", "退订")
        self.assertTrue(history.relevant_history)

    async def test_full_knowledge_adapter_and_unavailable_fallback(self):
        self.s.knowledge_search = AsyncMock(return_value=[{"document_id": "subscription-renewal-v1", "title": "订阅", "content": "内部已核实规则"}])
        result = await self.s.send("user", "退订政策")
        self.s.knowledge_search.assert_awaited_once()
        self.assertIn("内部已核实规则", result["response"])
        self.s.knowledge_search.side_effect = RuntimeError()
        failed = await self.s.send("user", "退订政策")
        self.assertIn("full_rag_unavailable", failed["degradations"])
        self.assertTrue(failed["sources"])

    async def test_memory_failure_does_not_allow_write(self):
        self.s.memory.get_context = AsyncMock(side_effect=RuntimeError())
        result = await self.s.send("user", "关闭自动续费")
        self.assertIn("memory_unavailable", result["degradations"])
        self.assertEqual(result["status"], "awaiting_clarification")
        self.assertFalse(result["task"]["actions"])

    async def test_credential_memory_redaction(self):
        await self.s.memory.add_message("user", "c", MsgRole.USER, "密码: fake-secret sk-fakeapikey123")
        context = await self.s.memory.get_context("user", "c")
        self.assertNotIn("fake-secret", context.to_prompt_text())
        self.assertNotIn("sk-fake", context.to_prompt_text())

    async def test_concurrent_turn_is_rejected(self):
        # GoalInterpreter.interpret() is now the one call every route makes
        # unconditionally (recognizer.recognize() only runs for the "chat"
        # fallback), so the concurrency probe holds that call instead.
        conv, _, _ = self.s._conversation("user")
        entered, proceed = asyncio.Event(), asyncio.Event()
        original = self.s.runtime.goal_interpreter.interpret
        async def held(*args, **kwargs):
            entered.set()
            await proceed.wait()
            return await original(*args, **kwargs)
        self.s.runtime.goal_interpreter.interpret = held
        first = asyncio.create_task(self.s.send("user", "退订政策", conversation_id=conv))
        await entered.wait()
        try:
            with self.assertRaises(ValueError):
                await self.s.send("user", "关闭续费", conversation_id=conv)
        finally:
            proceed.set()
            await first

    async def test_agent_orchestrator_failure_diagnostic_is_queryable_without_changing_the_answer(self):
        # BaseAgent.handle() (agents/agent_orchestrator.py) catches ANY exception from
        # a live model call and always replies with the same generic apology — by
        # design, since the raw exception could carry provider error bodies or other
        # details that shouldn't round-trip into a user-facing response. Before this,
        # the real exception only ever reached a server-side logger.error() line, so a
        # user hitting this had no way to tell "transient network blip" from "real code
        # bug" without someone going to find that log. Confirm the sanitized
        # category/error_type now surfaces through the SAME interpretation.diagnostics
        # channel GoalInterpreter failures already use, while the chat-visible answer
        # text is untouched.
        from unittest.mock import AsyncMock, MagicMock
        self.s.answer_orchestrator = MagicMock()
        self.s.answer_orchestrator.run = AsyncMock(return_value=MagicMock(
            response="抱歉，处理您的请求时出现问题，请稍后重试。",
            error_diagnostics=[{"category": "agent_call_failed", "error_type": "APIConnectionError"}]))
        result = await self.s.send("user", "你好，随便问问")
        self.assertEqual(result["response"], "抱歉，处理您的请求时出现问题，请稍后重试。")
        self.assertIn("agent_call_failed", result["degradations"])
        diagnostics = result["interpretation"]["diagnostics"]
        self.assertTrue(any(d.get("source") == "agent_orchestrator" and d.get("error_type") == "APIConnectionError"
                             for d in diagnostics))

    async def test_intent_timeout_preserves_task_and_releases_lease(self):
        # recognizer.recognize() only runs on the "chat" fallback (no goals at
        # all), so the timeout has to be provoked on a fresh conversation with
        # no active task — that's also what proves an unrelated existing task
        # is left untouched by a failure somewhere else entirely.
        first = await self.s.send("user", "关闭自动续费")
        before = self.r.get(first["task"]["id"], "user")
        self.s.recognizer.recognize = AsyncMock(side_effect=asyncio.TimeoutError())
        result = await self.s.send("user", "你好，随便问问", conversation_id="unrelated-chat-conversation")
        self.assertEqual(result["status"], "retryable_error")
        self.assertIn("intent_unavailable", result["degradations"])
        self.assertEqual(self.r.get(before["id"], "user"), before)
        with self.r.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM unified_leases").fetchone()[0], 0)

    async def test_goal_failure_is_diagnosable_without_a_task(self):
        from agents.goal_interpreter import GoalInterpreter
        from scripts.agent_cli import Session
        from unittest.mock import patch
        import json
        model = AsyncMock()
        for side_effect, turn, category in [
            (asyncio.TimeoutError(), None, "timeout"),
            (None, {"content": "text only", "finish_reason": "stop"}, "missing_or_wrong_tool_call"),
            (None, {"tool_calls": [{"function": {"name": "interpret_goal", "arguments": "invalid"}}]}, "invalid_arguments"),
        ]:
            model.create_tool_turn.reset_mock()
            model.create_tool_turn.side_effect = side_effect
            model.create_tool_turn.return_value = turn
            self.r.goal_interpreter = GoalInterpreter(model)
            with patch("api.action_routes.conversation_service_for", return_value=self.s):
                session = Session(self.r, owner="user", unified=True)
            result = await session.handle("关闭自动续费")
            self.assertIsNone(result["task"])
            self.assertEqual(result["interpretation"]["errors"], [category, category])
            self.assertEqual(model.create_tool_turn.await_count, 2)
            self.assertEqual(model.create_tool_turn.call_args.kwargs["required_tool"], "interpret_goal")
            diagnostic = json.loads(await session.handle("/debug"))
            self.assertEqual(diagnostic["interpretation"]["diagnostics"][0]["category"], category)
            self.assertNotIn("text only", json.dumps(diagnostic))

    async def test_pending_side_policy_cannot_inherit_write_goal(self):
        import json
        from agents.goal_interpreter import GoalInterpreter
        from agents.subscription_knowledge import is_consultation_only
        started = await self.s.send("user", "关闭自动续费")
        pending = await self.s.send("user", self.order["id"], conversation_id=started["conversation_id"])
        before = pending["task"]
        model = AsyncMock()
        model.create_tool_turn.return_value = {"tool_calls": [{"function": {"name": "interpret_goal", "arguments": json.dumps({"goals": {"renewal": "cancel", "policy": "read"}, "policy_topics": ["refund"]})}}]}
        self.r.goal_interpreter = GoalInterpreter(model)
        for question in ["顺便解释一下退款政策，我现在不申请退款。", "介绍退款规则", "请说明退订流程"]:
            result = await self.s.send("user", question, conversation_id=started["conversation_id"])
            self.assertEqual(result["route"], "knowledge")
            self.assertTrue(result["knowledge_used"])
            self.assertIsNone(result["task"])
            self.assertIn("server_guard", result["interpretation"])
            self.assertEqual(self.r.get(before["id"], "user"), before)
        self.assertFalse(is_consultation_only("解释退款政策，并帮我退款"))

    async def test_technical_remainder_backstop_when_model_drops_it(self):
        # GoalInterpreter's system prompt asks the model to ALWAYS restate an
        # independent off-topic technical complaint into general_remainder, but a
        # single LLM call won't always comply — a salient primary goal (a clear
        # duplicate-charge refund ask) can pull attention away from a shorter
        # secondary complaint ("登录报错401") entirely. Simulate exactly that model
        # failure (goals populated, general_remainder left empty despite an obvious
        # technical-fault mention) and confirm the deterministic keyword backstop in
        # ConversationService catches it and still routes it to the old orchestrator.
        from agents.goal_interpreter import GoalProposal
        proposal = GoalProposal(goals={"billing": "refund"}, policy_topics=[])
        self.r.goal_interpreter.interpret = AsyncMock(return_value=(proposal, {"mode": "mock", "calls": 0, "usage": []}))
        self.s.answer_orchestrator = AsyncMock()
        self.s.answer_orchestrator.run_compound = AsyncMock(
            return_value=AsyncMock(response="登录 401 报错建议清除缓存后重试，如果仍无法登录请联系账号安全团队。"))
        message = "登录报错401，而且还重复扣款了"
        result = await self.s.send("user", message, order_id=self.order["id"])
        self.assertEqual(result["interpretation"]["server_guard"], "technical_remainder_backstop")
        self.s.answer_orchestrator.run_compound.assert_awaited_once()
        remainder_request = self.s.answer_orchestrator.run_compound.await_args.args[0]
        self.assertEqual(remainder_request.message, message)
        self.assertIn("登录 401 报错建议清除缓存", result["response"])

    async def test_technical_remainder_backstop_skips_when_service_goal_present(self):
        # If the model already folded the technical complaint into the "service"
        # goal (digital service uptime), the backstop must not ALSO force a
        # redundant remainder — that would be double-answering the same complaint.
        from agents.goal_interpreter import GoalProposal
        proposal = GoalProposal(goals={"service": "read"}, policy_topics=[])
        self.r.goal_interpreter.interpret = AsyncMock(return_value=(proposal, {"mode": "mock", "calls": 0, "usage": []}))
        self.s.answer_orchestrator = AsyncMock()
        self.s.answer_orchestrator.run_compound = AsyncMock()
        result = await self.s.send("user", "登录报错401", order_id=self.order["id"])
        self.assertNotEqual(result["interpretation"].get("server_guard"), "technical_remainder_backstop")
        self.s.answer_orchestrator.run_compound.assert_not_awaited()

    async def test_chat_and_conversation_api_share_confirmation_and_memory(self):
        import os
        import httpx
        from unittest.mock import patch
        from api import main, action_routes
        from tests.action_test_support import mint_token
        transport = httpx.ASGITransport(app=main.app)
        self.r.seed("demo-user")
        with patch.dict(os.environ, {"AGENT_JWT_SECRET": "test-signing-secret"}), \
             patch.object(action_routes, "runtime", return_value=self.r), \
             patch.object(action_routes, "conversation_service", return_value=self.s), \
             patch.object(main, "_memory", None), patch.object(main, "_orchestrator", None), \
             patch.object(main, "_tool_manager", None):
            token = mint_token("user", "demo-user")
            async with httpx.AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": "Bearer " + token}) as client:
                first = await client.post("/chat", json={"message": "怎么退订"})
                self.assertEqual(first.status_code, 200)
                conv = first.json()["conv_id"]
                second = await client.post("/agent/conversation", json={"message": "那就关闭", "conversation_id": conv})
                self.assertEqual(second.status_code, 200)
                task = second.json()["task"]
                self.assertEqual(task["conversation_id"], conv)
                cancelled = await client.post("/agent/tasks/" + task["id"] + "/cancel")
                self.assertEqual(cancelled.status_code, 200)
                memory = await self.s.memory.get_context("demo-user", conv)
                self.assertEqual(memory.recent_messages[-1].content, cancelled.json()["response"])
                denied = await client.post("/agent/conversation", json={"message": "查询", "conversation_id": conv}, headers={"Authorization": "Bearer wrong"})
                self.assertEqual(denied.status_code, 401)
