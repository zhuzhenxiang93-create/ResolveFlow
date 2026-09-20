"""Scope-restricted retrieval: full hybrid RAG must never leak out-of-domain
documents into the Action layer's policy answers, and must not silently fall
back to local BM25 just because a topic keyword also exists elsewhere in the
main knowledge base."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

from agents.action_runtime import ActionRuntime
from agents.conversation_service import ConversationService
from agents.goal_interpreter import GoalProposal
from mcp.knowledge_base import KnowledgeBase
from mcp.tool_manager import MCPToolManager, Tool

ONNXMiniLM_L6_V2.DOWNLOAD_PATH = (
    Path(__file__).resolve().parents[1] / "data/eval/.cache/onnx_models" / ONNXMiniLM_L6_V2.MODEL_NAME
)


class PolicyOnly:
    """Deterministic goal interpreter stub: always resolves to a policy read."""
    client = True

    def __init__(self, topics):
        self.topics = topics

    async def interpret(self, message, context):
        return GoalProposal(goals={"policy": "read"}, policy_topics=self.topics), {"mode": "mock", "calls": 0, "usage": []}


class ScopedKnowledgeBaseTests(unittest.TestCase):
    def _kb(self):
        self.temp = tempfile.TemporaryDirectory(prefix="resolveflow_scope_test_")
        self.addCleanup(self.temp.cleanup)
        kb = KnowledgeBase(chroma_host="127.0.0.1", chroma_port=65535, chroma_path=self.temp.name, load_default_docs=False)
        kb.add_documents([
            {
                "id": "generic-refund-policy",
                "title": "退款政策",
                "content": "退款政策说明。用户在购买后 7 天内可以申请无理由退款，审核通过后 5-7 个工作日内退回原支付账户。" * 3,
            },
            {
                "id": "subscription-refund-v1",
                "title": "退款条件与进度",
                "source": "subscription_service_v1",
                "content": "内部模拟规则：自动执行链路仅受理已核实的重复订阅扣款，每次申请退一笔。用户确认申请后，仍需独立人工审批。",
            },
        ])
        return kb

    def test_unscoped_search_can_see_the_generic_document(self):
        kb = self._kb()
        hits = kb.search("退款多久到账", 10)
        self.assertIn("generic-refund-policy", {h["document_id"] for h in hits})

    def test_scoped_search_never_returns_documents_outside_the_allowed_set(self):
        kb = self._kb()
        allowed = {"subscription-refund-v1"}
        hits = kb.search("退款多久到账", 10, allowed_document_ids=allowed)
        self.assertTrue(hits)
        self.assertTrue(all(h["document_id"] in allowed for h in hits))
        self.assertNotIn("generic-refund-policy", {h["document_id"] for h in hits})

    def test_empty_allowed_set_returns_nothing_without_querying(self):
        kb = self._kb()
        self.assertEqual(kb.search("退款", 10, allowed_document_ids=set()), [])

    def test_lexical_only_backend_also_honors_scope(self):
        docs = [
            {"id": "generic-refund-policy", "title": "退款政策", "content": "退款政策说明，7 天无理由退款。"},
            {"id": "subscription-refund-v1", "title": "退款条件", "content": "内部模拟重复扣款退款规则。"},
        ]
        kb = KnowledgeBase.lexical_documents(docs)
        hits = kb.search("退款", 10, allowed_document_ids={"subscription-refund-v1"})
        self.assertTrue(all(h["document_id"] == "subscription-refund-v1" for h in hits))


class ConversationServiceScopeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = ActionRuntime(self.temp.name + "/db", allow_fallback=False)
        self.runtime.goal_interpreter = PolicyOnly(["refund"])
        self.service = ConversationService(self.runtime)
        self.subscription_refund_ids = {d["id"] for d in self.service.documents if d["topic"] == "refund"}

    async def test_full_rag_is_called_with_the_topic_scoped_document_ids(self):
        self.service.knowledge_search = AsyncMock(return_value=[
            {"document_id": next(iter(self.subscription_refund_ids)), "title": "订阅退款", "content": "已核实的订阅退款规则"}
        ])
        result = await self.service.send("user", "会员费退款政策是什么")
        self.service.knowledge_search.assert_awaited_once()
        _, kwargs = self.service.knowledge_search.await_args
        self.assertEqual(set(kwargs["allowed_document_ids"]), self.subscription_refund_ids)
        self.assertIn("已核实的订阅退款规则", result["response"])

    async def test_out_of_scope_hits_from_a_misbehaving_retriever_are_dropped(self):
        # Defense in depth: even if the injected retriever ignores the scope and
        # returns a document outside the requested set, conversation_service must
        # not surface it as a subscription policy answer.
        self.service.knowledge_search = AsyncMock(return_value=[
            {"document_id": "generic-refund-policy", "title": "泛用退款", "content": "不应出现的泛用客服政策"}
        ])
        result = await self.service.send("user", "会员费退款政策是什么")
        self.assertNotIn("不应出现的泛用客服政策", result["response"])

    async def test_successful_empty_result_is_labeled_as_no_scoped_hit_not_unavailable(self):
        self.service.knowledge_search = AsyncMock(return_value=[])
        result = await self.service.send("user", "会员费退款政策是什么")
        self.assertIn("full_rag_no_scoped_hit", result["degradations"])
        self.assertNotIn("full_rag_unavailable", result["degradations"])
        # Local BM25 fallback still finds the subscription document by id.
        self.assertTrue(result["sources"])

    async def test_exception_from_full_rag_is_labeled_unavailable(self):
        self.service.knowledge_search = AsyncMock(side_effect=RuntimeError("down"))
        result = await self.service.send("user", "会员费退款政策是什么")
        self.assertIn("full_rag_unavailable", result["degradations"])


class ToolManagerScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_with_rewrite_forwards_extra_params_to_every_sub_query(self):
        manager = MCPToolManager(api_key="test-key")
        # Deterministic rewrite: two sub-queries, no real LLM call.
        manager._rewrite_query_with_status = AsyncMock(return_value=(["退款", "退款政策"], True))
        seen_calls = []

        async def handler(params, context):
            seen_calls.append(dict(params))
            return [{"chunk_id": params["query"], "document_id": "subscription-refund-v1", "content": "ok"}]

        manager.register(Tool(
            name="knowledge_search", description="", handler=handler,
            schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        ))
        result = await manager.search_with_rewrite(
            "knowledge_search", "退款", top_k=5, extra_params={"allowed_document_ids": ["subscription-refund-v1"]},
        )
        self.assertTrue(result.success)
        self.assertEqual(len(seen_calls), 2)
        self.assertTrue(all(c["allowed_document_ids"] == ["subscription-refund-v1"] for c in seen_calls))

    async def test_search_with_rewrite_without_extra_params_is_unaffected(self):
        manager = MCPToolManager(api_key="test-key")
        manager._rewrite_query_with_status = AsyncMock(return_value=(["退款"], True))
        seen_calls = []

        async def handler(params, context):
            seen_calls.append(dict(params))
            return [{"chunk_id": "c1", "document_id": "d1", "content": "ok"}]

        manager.register(Tool(
            name="knowledge_search", description="", handler=handler,
            schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        ))
        result = await manager.search_with_rewrite("knowledge_search", "退款", top_k=5)
        self.assertTrue(result.success)
        self.assertNotIn("allowed_document_ids", seen_calls[0])


if __name__ == "__main__":
    unittest.main()
