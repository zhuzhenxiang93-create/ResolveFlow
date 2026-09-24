"""One owner-bound conversation coordinates legacy read capabilities and safe actions."""
import asyncio
import json
import re
import time
import uuid
from contextvars import ContextVar

_TURN_EVIDENCE = ContextVar("turn_evidence", default=None)
from pathlib import Path

from agents.action_runtime import TERMINAL
from agents.subscription_knowledge import policy_refund_is_actually_goods, refund_target_is_ambiguous
from agents.supervisor import SubTaskResult, TaskPlanner, TaskStatus
from core.intent_recognizer import IntentCategory, IntentRecognizer
from memory.conversation_memory import MsgRole
from memory.local_conversation_memory import LocalConversationMemory
from mcp.knowledge_base import KnowledgeBase

ROOT = Path(__file__).resolve().parents[1]


class ConversationService:
    def __init__(self, runtime, *, memory=None, recognizer=None, knowledge_search=None, answer_orchestrator=None):
        from business.execution import ExecutionContext
        self.execution = ExecutionContext(runtime.path, runtime.client)
        self.runtime = runtime
        self.memory = memory or LocalConversationMemory(runtime.path)
        self.recognizer = recognizer or IntentRecognizer(api_key="", client=runtime.client,
            use_llm=bool(runtime.client), base_url="local-compatible" if runtime.client else None)
        self.answer_orchestrator = answer_orchestrator
        if self.answer_orchestrator is None and runtime.client:
            from agents.agent_orchestrator import AgentOrchestrator
            self.answer_orchestrator = AgentOrchestrator(api_key="", client=runtime.client, recognizer=self.recognizer,
                                                       model=getattr(runtime.client, "model", "configured-model"))
        self.documents = json.loads((ROOT / "data/knowledge/commerce_policy_v1.json").read_text())
        self.kb = KnowledgeBase.lexical_documents(self.documents)
        self.knowledge_search = knowledge_search
        self.knowledge_document_ids = None
        with runtime.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS unified_conversations(id TEXT PRIMARY KEY, owner TEXT NOT NULL, task_id TEXT, last_order TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS unified_leases(id TEXT PRIMARY KEY, token TEXT, expires REAL)")

    def _conversation(self, owner, conversation_id=None, task_id=None):
        if task_id:
            task = self.execution.get(task_id, owner)
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
        from business.conversation import CommerceConversation
        commerce = CommerceConversation(self.runtime, self.memory, self.knowledge_search, self.knowledge_document_ids)
        try:
            result = await commerce.send(owner, conv, message, selection=order_id)
        except Exception as ex:
            import logging
            logging.getLogger(__name__).exception("Unified commerce failed (%s)", type(ex).__name__)
            return await commerce._answer(owner, conv, message, "业务查询暂时失败，请重试并核对任务状态。", mode="commerce_error")
        if result is not None:
            remainder = result.pop("general_remainder", "")
            if remainder and self.answer_orchestrator:
                from agents.agent_orchestrator import Request
                try:
                    context = await self.memory.get_context(owner, conv, remainder)
                    support = await self.answer_orchestrator.run(Request(message=remainder, user_id=owner, conv_id=conv,
                        context=context.to_prompt_text()))
                    result["response"] += "\n" + support.response
                    for trace in getattr(support, "tool_traces", []):
                        result["sources"].extend(trace.get("sources", []))
                        result["degradations"].extend(trace.get("degradations", []))
                except Exception:
                    result["response"] += "\n补充咨询暂时不可用，以上业务状态已保留。"
                    result["degradations"].append("support_unavailable")
            return result
        errors, knowledge, record = [], [], {"mode": "general_support"}
        try:
            memory = await asyncio.wait_for(self.memory.get_context(owner, conv, message), 5)
        except Exception:
            from memory.conversation_memory import MemoryContext
            memory = MemoryContext([], [], {}, "")
            errors.append("memory_unavailable")
        history = [{"role": m.role.value, "content": m.content} for m in memory.recent_messages[-8:]]
        try:
            intent = await asyncio.wait_for(self.recognizer.recognize(message, history=history), 30)
        except Exception as ex:
            return {"conversation_id": conv, "response": "意图识别暂时不可用，本轮未执行操作。请稍后重试，原任务保持不变。",
                    "route": "model_error", "task": None,
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
            for trace in result.tool_traces:
                knowledge.extend(trace.get("sources", []))
                errors.extend(trace.get("degradations", []))
            if result.error_diagnostics:
                # The user-visible answer stays the orchestrator's own generic
                # "抱歉..." apology unchanged — this only makes the sanitized
                # failure reason (category/error_type/http_status, never the raw
                # exception) queryable via `interpretation.diagnostics`, the same
                # place GoalInterpreter failures already surface theirs, instead of
                # only ever reaching a server-side log line.
                record.setdefault("diagnostics", []).extend(
                    {**d, "source": "agent_orchestrator"} for d in result.error_diagnostics)
                errors.append("agent_call_failed")
        else:
            answer = "你好，我可以解释商品和订阅政策、查询购买记录，并在你确认后办理权益修复、关闭续费或退款申请。" if intent.intent == IntentCategory.GREETING else "请说明要咨询的商品或订阅规则、查询的信息或希望办理的操作。"
        result = await commerce._answer(owner, conv, message, answer, mode="general_support")
        result.update(route="chat", intent=intent_value, intent_source_scores=intent_source_scores,
                      interpretation=record, sources=knowledge, knowledge_used=bool(knowledge))
        result["degradations"].extend(errors)
        return result


# Legacy telemetry labels remain pure helpers for historical report readers.
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
