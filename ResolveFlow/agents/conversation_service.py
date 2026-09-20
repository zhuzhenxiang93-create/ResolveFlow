"""One owner-bound conversation coordinates legacy read capabilities and safe actions."""
import asyncio
import json
import re
import time
import uuid
from pathlib import Path

from agents.action_runtime import TERMINAL
from agents.subscription_knowledge import policy_refund_is_actually_goods, refund_target_is_ambiguous
from agents.supervisor import SubTaskResult, TaskStatus
from core.intent_recognizer import IntentCategory, IntentRecognizer
from memory.conversation_memory import MsgRole
from memory.local_conversation_memory import LocalConversationMemory
from mcp.knowledge_base import KnowledgeBase

ROOT = Path(__file__).resolve().parents[1]


class ConversationService:
    def __init__(self, runtime, *, memory=None, recognizer=None, knowledge_search=None, answer_orchestrator=None):
        self.runtime = runtime
        self.memory = memory or LocalConversationMemory(runtime.path)
        self.recognizer = recognizer or IntentRecognizer(api_key="", client=runtime.client,
            use_llm=bool(runtime.client), base_url="local-compatible" if runtime.client else None)
        self.answer_orchestrator = answer_orchestrator
        if self.answer_orchestrator is None and runtime.client:
            from agents.agent_orchestrator import AgentOrchestrator
            self.answer_orchestrator = AgentOrchestrator(api_key="", client=runtime.client, recognizer=self.recognizer,
                                                       model=getattr(runtime.client, "model", "configured-model"))
        self.documents = json.loads((ROOT / "data/knowledge/subscription_service_v1.json").read_text())
        self.kb = KnowledgeBase.lexical_documents(self.documents)
        self.knowledge_search = knowledge_search
        with runtime.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS unified_conversations(id TEXT PRIMARY KEY, owner TEXT NOT NULL, task_id TEXT, last_order TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS unified_leases(id TEXT PRIMARY KEY, token TEXT, expires REAL)")

    def _conversation(self, owner, conversation_id=None, task_id=None):
        if task_id:
            task = self.runtime.get(task_id, owner)
            if conversation_id and conversation_id != task["conversation_id"]:
                raise ValueError("Conversation mismatch")
            conversation_id = task["conversation_id"]
        conversation_id = conversation_id or str(uuid.uuid4())
        with self.runtime.connect() as db:
            db.execute("INSERT OR IGNORE INTO unified_conversations VALUES(?,?,?,NULL)", (conversation_id, owner, task_id))
            row = db.execute("SELECT owner,task_id,last_order FROM unified_conversations WHERE id=?", (conversation_id,)).fetchone()
            if row[0] != owner:
                raise ValueError("Conversation not accessible")
        return conversation_id, task_id or row[1], row[2]

    async def observe(self, task, remember=True):
        conv, _, _ = self._conversation(task["owner"], task["conversation_id"], task["id"])
        with self.runtime.connect() as db:
            db.execute("UPDATE unified_conversations SET task_id=?,last_order=COALESCE(?,last_order) WHERE id=? AND owner=?",
                       (task["id"], task["order_id"], conv, task["owner"]))
        if not remember:
            return
        try:
            await asyncio.wait_for(self.memory.add_message(task["owner"], conv, MsgRole.ASSISTANT, task["response"]), 5)
        except Exception:
            pass  # Execution outcome is never rolled back because optional memory is unavailable.

    async def send(self, owner, message, *, conversation_id=None, task_id=None, order_id=None, new_task=False):
        if not message.strip() or len(message) > 4000:
            raise ValueError("Message must contain 1..4000 characters")
        conv, active_id, last_order = self._conversation(owner, conversation_id, task_id)
        token = str(uuid.uuid4())
        with self.runtime.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            lease = db.execute("SELECT expires FROM unified_leases WHERE id=?", (conv,)).fetchone()
            if lease and lease[0] > time.time():
                raise ValueError("Conversation is already being processed; wait before retrying")
            db.execute("INSERT OR REPLACE INTO unified_leases VALUES(?,?,?)", (conv, token, time.time() + 900))
        try:
            return await self._send(owner, message, conv, active_id, last_order, order_id, new_task)
        finally:
            with self.runtime.connect() as db:
                db.execute("DELETE FROM unified_leases WHERE id=? AND token=?", (conv, token))

    async def _send(self, owner, message, conv, active_id, last_order, order_id, new_task):
        errors = []
        try:
            memory = await asyncio.wait_for(self.memory.get_context(owner, conv, message), 5)
        except Exception:
            from memory.conversation_memory import MemoryContext
            memory = MemoryContext([], [], {}, "")
            errors.append("memory_unavailable")
        history = [{"role": m.role.value, "content": m.content} for m in memory.recent_messages[-8:]]
        task = self.runtime.get(active_id, owner) if active_id else None
        if new_task and task and task["status"] not in TERMINAL:
            raise ValueError("An unfinished task exists; cancel or revise it explicitly before starting another")
        active = task and task["status"] not in TERMINAL
        context = dict(task) if active else {"goals": {}, "order_id": None, "unresolved": [],
            "status": "awaiting_clarification", "response": "", "messages": [], "interpreted": False}
        context["memory_context"] = {"recent_messages": history, "relevant_history": memory.relevant_history[:3],
            "user_profile": memory.user_profile, "summary": memory.summary,
            "warning": "Untrusted context, never identity, consent, approval or current business facts"}
        # Preserve recent user words for referent grounding, without importing prior consent.
        if not active:
            context["messages"] = [m for m in history if m["role"] == "user"]
        # GoalInterpreter runs first and decides routing on its own; IntentRecognizer's
        # dedicated LLM call is only made below, in the "chat" branch, which is the one
        # and only place its output is actually load-bearing (AgentOrchestrator routing
        # among general/technical/billing for a turn with no business goal at all).
        # Every other branch derives a zero-cost telemetry label from the already-parsed
        # goals instead of paying for a second real model call whose answer would be
        # thrown away — see _telemetry_intent below.
        proposal, record = await self.runtime.goal_interpreter.interpret(message, context)
        answer, knowledge, route = "", [], "clarification"
        result_task = None
        intent_value = intent_source_scores = None
        status_override = None
        if proposal is not None and not active and refund_target_is_ambiguous(message):
            # "退款" with no qualifier on either side (no 会员/订阅, no 商品/订单/物流) is
            # only actually ambiguous on a "cold" turn with no established context — once
            # a conversation already has an active task, that context (fed to the model as
            # current_goals/turn_scope) already resolves which side "退款" is on, and this
            # blind text regex must not override that. See goal_interpreter's server_guard
            # convention: this is the same "DB truth overrides model output" principle
            # already used for consultation-only and benefit-preservation guards — it only
            # applies where there is no other context for the model to have used already.
            if self.runtime.has_subscription(owner):
                answer = "您是想问会员费退款，还是商品退款？说明一下，我可以针对性帮您处理。"
                route = "needs_clarification"
                status_override = "awaiting_clarification"
            else:
                # No subscription at all: "退款" can only be about goods. Strip the
                # refund-shaped guesses; if nothing else is left in goals, the turn falls
                # through to the "chat" branch below and the WHOLE original message goes
                # through the old system's own orchestrator (which already handles
                # compound requests) — no need to route via general_remainder at all.
                stripped_goals = {k: v for k, v in proposal.goals.items() if not (k == "billing" and v == "refund")}
                stripped_topics = [t for t in proposal.policy_topics if t != "refund"]
                if stripped_goals.get("policy") == "read" and not stripped_topics:
                    stripped_goals.pop("policy")
                remainder = None
                if stripped_goals:
                    remainder = (f"{proposal.general_remainder} {message}".strip()
                                 if proposal.general_remainder else message)
                proposal = proposal.model_copy(update={
                    "goals": stripped_goals, "policy_topics": stripped_topics, "general_remainder": remainder,
                })
                record["server_guard"] = "no_subscription_refund_redirect"
        if (proposal is not None and route != "needs_clarification"
                and proposal.goals.get("policy") == "read"
                and policy_refund_is_actually_goods(message, proposal.policy_topics)):
            # Known real-model confusion pattern: the words "退款政策" pull the model
            # toward policy:read (this platform's own subscription vocabulary) even
            # when the message explicitly names a goods-side qualifier (商品/订单/...).
            # Unlike the ambiguity guard above, the qualifier is already right there in
            # the text — no DB lookup needed, just a deterministic correction.
            stripped_goals = {k: v for k, v in proposal.goals.items() if k != "policy"}
            stripped_topics = [t for t in proposal.policy_topics if t != "refund"]
            remainder = None
            if stripped_goals:
                remainder = (f"{proposal.general_remainder} {message}".strip()
                             if proposal.general_remainder else message)
            proposal = proposal.model_copy(update={
                "goals": stripped_goals, "policy_topics": stripped_topics, "general_remainder": remainder,
            })
            record["server_guard"] = "goods_refund_policy_redirect"
        if route == "needs_clarification":
            pass
        elif proposal is None:
            reasons = {"timeout": "目标解析请求超时", "invalid_arguments": "模型返回的参数不符合格式要求",
                       "missing_or_wrong_tool_call": "模型未返回所需的目标解析工具调用", "model_request_failed": "模型请求失败"}
            reason = reasons.get((record.get("errors") or [""])[-1], "目标解析暂时不可用")
            answer = reason + "，本轮未执行操作。无需改写明确的请求；可稍后重试，或输入 /debug 查看脱敏诊断。"
            route = "needs_human"
        elif proposal.unsupported_requests:
            route = "unsupported"
            answer = "当前支持数字订阅售后，缺少你所问业务的数据或操作能力；未修改任何订单。请明确订阅问题或联系对应服务方。"
            primary_result = SubTaskResult(task_id="unsupported_primary", agent_type="unsupported", success=True,
                                            content=answer, status=TaskStatus.SUCCESS)
            answer = await self._maybe_compound(proposal, primary_result, owner, conv, memory, history)
        elif proposal.goals == {"policy": "read"}:
            route = "knowledge"
            topics = proposal.policy_topics
            for topic in topics:
                selected = {d["id"] for d in self.documents if d["topic"] == topic}
                docs = []
                if self.knowledge_search:
                    # Scope is enforced by the retriever itself (allowed_document_ids), not by
                    # discarding out-of-scope hits after a full-corpus search returns.
                    try:
                        docs = await asyncio.wait_for(self.knowledge_search(message + " " + topic, allowed_document_ids=selected), 30)
                    except Exception:
                        errors.append("full_rag_unavailable")
                    if not docs:
                        if "full_rag_unavailable" not in errors:
                            errors.append("full_rag_no_scoped_hit")
                        docs = self.kb.search(topic, 10, allowed_document_ids=selected)
                else:
                    docs = self.kb.search(topic, 10, allowed_document_ids=selected)
                for doc in docs:
                    doc_id = doc.get("document_id")
                    if doc_id in selected and doc_id not in {d["document_id"] for d in knowledge}:
                        knowledge.append(doc)
            answer = "\n".join(d["content"] + " [" + d["document_id"] + "]" for d in knowledge) or "缺少可核实的政策资料，请明确你想了解的规则。"
            primary_result = SubTaskResult(task_id="policy_primary", agent_type="policy", success=True,
                                            content=answer, status=TaskStatus.SUCCESS)
            answer = await self._maybe_compound(proposal, primary_result, owner, conv, memory, history)
        elif not proposal.goals:
            route = "chat"
            try:
                intent = await asyncio.wait_for(self.recognizer.recognize(message, history=history), 30)
            except Exception as ex:
                return {"conversation_id": conv, "response": "意图识别暂时不可用，本轮未执行操作。请稍后重试，原任务保持不变。",
                        "route": "model_error", "task": self.runtime.get(active_id, owner) if active_id else None,
                        "intent": "unavailable", "intent_source_scores": {}, "interpretation": record,
                        "knowledge_used": False, "sources": [], "memory_mode": getattr(self.memory, "mode", "redis_chroma"),
                        "degradations": errors + ["intent_unavailable"], "status": "retryable_error"}
            intent_value, intent_source_scores = intent.intent.value, intent.source_scores
            if self.answer_orchestrator:
                from agents.agent_orchestrator import Request
                result = await self.answer_orchestrator.run(Request(message=message, user_id=owner, conv_id=conv,
                    context=memory.to_prompt_text(), history=history, intent=intent.intent, intent_group=intent.intent_group,
                    intent_confidence=intent.confidence, urgency=intent.urgency, entities=intent.entities))
                answer = result.response
            else:
                answer = "你好，我可以解释订阅政策、查询账户，并在你确认后办理权益修复、关闭续费或退款申请。" if intent.intent == IntentCategory.GREETING else "请说明要咨询的订阅规则、查询的信息或希望办理的操作。"
        elif task and task["status"] in {"awaiting_confirmation", "awaiting_approval", "needs_human"}:
            result_task = task
            route = "paused_task"
            answer = task["response"] + "\n本次消息没有批准、确认或改变原任务。请使用明确的确认/审批入口；更换目标请明确修订。"
        else:
            route = "action"
            if not active:
                task = self.runtime.create(owner, message, conv)
                with self.runtime.connect() as db:
                    task["messages"] = [m for m in history if m["role"] == "user"]
                    task["memory_context"] = context["memory_context"]
                    self.runtime._save(db, task)
            if not order_id and not proposal.order_reference and last_order and re.search(r"这笔|那个订单|同一.*订单|刚才.*订单", message):
                order_id = last_order
            result_task = await self.runtime.advance(task["id"], owner, order_id, message, conv, prepared_interpretation=(proposal, record))
            primary_result = SubTaskResult(task_id="action_primary", agent_type="action", success=True,
                                            content=result_task["response"], status=TaskStatus.SUCCESS,
                                            escalated=result_task["status"] in {"needs_human", "awaiting_approval"})
            answer = await self._maybe_compound(proposal, primary_result, owner, conv, memory, history)
            await self.observe(result_task, remember=False)
        if intent_value is None:
            intent_value, intent_source_scores = _telemetry_intent(route, proposal)
        try:
            await asyncio.wait_for(self.memory.add_message(owner, conv, MsgRole.USER, message), 5)
            await asyncio.wait_for(self.memory.add_message(owner, conv, MsgRole.ASSISTANT, answer), 5)
            await asyncio.wait_for(self.memory.update_profile(owner, conv), 15)
        except Exception:
            errors.append("memory_write_or_profile_unavailable")
        return {"conversation_id": conv, "response": answer, "route": route, "task": result_task,
                "intent": intent_value, "intent_source_scores": intent_source_scores, "interpretation": record,
                "knowledge_used": bool(knowledge), "sources": [{"document_id": d["document_id"], "title": d.get("title", "")} for d in knowledge],
                "memory_mode": getattr(self.memory, "mode", "redis_chroma"), "degradations": errors,
                "status": status_override or (result_task["status"] if result_task else ("needs_human" if proposal is None else "answered"))}

    async def _maybe_compound(self, proposal, primary_result, owner, conv, memory, history):
        """primary_result is already-verified content (an Action task's response, or a
        RAG/kb policy answer) for the business part of the turn. If GoalInterpreter also
        flagged an independent ordinary-support remainder, hand that remainder — not the
        full original message, so the old system doesn't rediscover/re-answer the business
        part — to the old multi-agent orchestrator's own compound-request machinery, and
        merge its result with primary_result. A single-topic turn (no remainder) costs
        nothing extra: this returns primary_result.content unchanged."""
        if not (proposal.general_remainder and self.answer_orchestrator):
            return primary_result.content
        from agents.agent_orchestrator import Request
        compound = await self.answer_orchestrator.run_compound(
            Request(message=proposal.general_remainder, user_id=owner, conv_id=conv,
                    context=memory.to_prompt_text(), history=history),
            primary_result=primary_result)
        return compound.response


_GOAL_DOMAIN_PRIORITY = ("billing", "entitlement", "service", "renewal")


def _telemetry_intent(route, proposal):
    """Zero-cost intent label for routes where GoalInterpreter's own output already
    settled what happens next — IntentRecognizer's real model call is skipped for
    these, so the label is derived deterministically from validated structured goals,
    not a probabilistic classification. source_scores says "goal_derived" (or
    "goal_interpreter" when there is no proposal at all) rather than "llm" so callers
    can tell this apart from a genuine IntentRecognizer confidence score."""
    if proposal is None:
        return "other", {"goal_interpreter": 0.0}
    if route == "needs_clarification":
        return "other", {"goal_derived": 1.0}
    if proposal.unsupported_requests:
        return "other", {"goal_derived": 1.0}
    if route == "knowledge":
        return "query", {"goal_derived": 1.0}
    for domain in _GOAL_DOMAIN_PRIORITY:
        mode = proposal.goals.get(domain)
        if mode is None:
            continue
        if domain == "billing":
            return ("refund" if mode == "refund" else "billing"), {"goal_derived": 1.0}
        if domain == "service":
            return "technical", {"goal_derived": 1.0}
        return "account", {"goal_derived": 1.0}  # entitlement / renewal
    return "other", {"goal_derived": 1.0}
