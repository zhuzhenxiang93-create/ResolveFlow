import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

import httpx

from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

from evaluation.rag_evaluator import evaluate_retrieval
from mcp.hybrid_retriever import BM25Index, reciprocal_rank_fusion, tokenize
from mcp.knowledge_base import KnowledgeBase
from mcp.qwen_reranker import Qwen3Reranker
from mcp.tool_manager import MCPToolManager


def record(chunk_id, document_id=None, content="text"):
    return {
        "chunk_id": chunk_id,
        "document_id": document_id or chunk_id,
        "title": chunk_id,
        "content": content,
        "metadata": {},
        "vector_score": None,
        "lexical_score": None,
    }


class HybridRetrieverTests(unittest.TestCase):
    def test_tokenizer_preserves_mixed_identifiers_and_chinese_bigrams(self):
        tokens = tokenize("登录 ERR_X91 后检查 RTX-5090")
        self.assertIn("id:err_x91", tokens)
        self.assertIn("id:rtx-5090", tokens)
        self.assertIn("登录", tokens)

    def test_bm25_searches_full_index_and_supports_incremental_updates(self):
        index = BM25Index()
        index.upsert([
            record("a", content="普通登录帮助"),
            record("b", content="错误码 ERR_X91 表示令牌过期"),
        ])
        self.assertEqual(index.search("ERR_X91", 1)[0]["chunk_id"], "b")
        index.upsert([record("b", content="这个片段已经替换，不再含有旧错误码")])
        self.assertFalse(index.search("ERR_X91", 3))
        index.remove(["b"])
        self.assertEqual(len(index), 1)

    def test_rrf_fuses_both_and_single_route_hits(self):
        fused = reciprocal_rank_fusion(
            {
                "vector": [record("both"), record("vector-only")],
                "lexical": [record("both"), record("lexical-only")],
            },
            top_k=10,
        )
        self.assertEqual(fused[0]["chunk_id"], "both")
        by_id = {item["chunk_id"]: item for item in fused}
        self.assertEqual(by_id["both"]["vector_rank"], 1)
        self.assertEqual(by_id["both"]["lexical_rank"], 1)
        self.assertIsNone(by_id["vector-only"]["lexical_rank"])
        self.assertIsNone(by_id["lexical-only"]["vector_rank"])

    def test_rrf_deduplicates_within_route_and_handles_large_top_k(self):
        fused = reciprocal_rank_fusion(
            {"vector": [record("a"), record("a")], "lexical": [record("b")]},
            top_k=20,
        )
        self.assertEqual({item["chunk_id"] for item in fused}, {"a", "b"})
        self.assertEqual(len(fused), 2)

    def test_rrf_weight_is_deterministic_for_tied_rankings(self):
        fused = reciprocal_rank_fusion(
            {"vector": [record("a")], "lexical": [record("b")]},
            top_k=2,
            route_weights={"vector": 1.0, "lexical": 2.0},
        )
        self.assertEqual([item["chunk_id"] for item in fused], ["b", "a"])

    @staticmethod
    def _bare_knowledge_base():
        class Collection:
            @staticmethod
            def count():
                return 2

        kb = object.__new__(KnowledgeBase)
        kb._lock = threading.RLock()
        kb._collection = Collection()
        kb._candidate_multiplier = 4
        kb._min_candidates = 20
        kb._rrf_c = 60.0
        kb._cjk_lexical_weight = 8.0
        kb._identifier_lexical_weight = 10.0
        return kb

    def test_vector_failure_degrades_to_lexical_with_observable_marker(self):
        kb = self._bare_knowledge_base()
        kb._vector_search = lambda query, top_k: (_ for _ in ()).throw(RuntimeError("down"))
        lexical = record("lex", "doc-lex")
        lexical["lexical_score"] = 3.0
        kb._lexical_search = lambda query, top_k: [lexical]
        result = kb.search("退款规则", 3)
        self.assertEqual(result[0]["document_id"], "doc-lex")
        self.assertIn("vector_unavailable", result[0]["degradations"])

    def test_lexical_failure_degrades_to_vector(self):
        kb = self._bare_knowledge_base()
        vector = record("vec", "doc-vec")
        vector["vector_score"] = 0.8
        kb._vector_search = lambda query, top_k: [vector]
        kb._lexical_search = lambda query, top_k: (_ for _ in ()).throw(RuntimeError("down"))
        result = kb.search("refund policy", 3)
        self.assertEqual(result[0]["document_id"], "doc-vec")
        self.assertIn("lexical_unavailable", result[0]["degradations"])

    def test_both_retrieval_routes_failing_raises_for_tool_fallback(self):
        kb = self._bare_knowledge_base()
        kb._vector_search = lambda query, top_k: (_ for _ in ()).throw(RuntimeError("down"))
        kb._lexical_search = lambda query, top_k: (_ for _ in ()).throw(RuntimeError("down"))
        with self.assertRaises(RuntimeError):
            kb.search("refund", 3)

    def test_document_update_and_delete_keep_vector_and_bm25_in_sync(self):
        ONNXMiniLM_L6_V2.DOWNLOAD_PATH = (
            Path(__file__).resolve().parents[1]
            / "data/eval/.cache/onnx_models"
            / ONNXMiniLM_L6_V2.MODEL_NAME
        )
        with tempfile.TemporaryDirectory(prefix="resolveflow_hybrid_test_") as path:
            kb = KnowledgeBase(
                chroma_host="127.0.0.1",
                chroma_port=65535,
                chroma_path=path,
                load_default_docs=False,
            )
            kb.add_documents([{
                "id": "mutable-doc",
                "title": "Mutable",
                "content": "ERR_OLD is the original marker。" * 80,
            }])
            self.assertGreater(len(kb._lexical_search("ERR_OLD", 10)), 0)
            kb.add_documents([{
                "id": "mutable-doc",
                "title": "Mutable",
                "content": "ERR_NEW is the replacement marker。",
            }])
            self.assertFalse(kb.search("ERR_OLD", 10))
            self.assertEqual(
                kb._lexical_search("ERR_NEW", 1)[0]["document_id"],
                "mutable-doc",
            )
            remaining = kb._collection.get(
                where={"document_id": "mutable-doc"}, include=["metadatas"]
            )
            self.assertEqual(len(remaining["ids"]), 1)
            self.assertEqual(kb.delete_documents(["mutable-doc"]), 1)
            self.assertFalse(kb._lexical_search("ERR_NEW", 10))

    def test_reranker_sanitizes_indices_and_appends_omissions(self):
        async def run():
            async def handler(request):
                payload = __import__("json").loads(request.content)
                self.assertEqual(payload["model"], "qwen3-rerank")
                self.assertEqual(payload["top_n"], 5)
                self.assertIn("instruct", payload)
                return httpx.Response(200, json={
                    "results": [
                        {"index": 2, "relevance_score": 0.91},
                        {"index": 2, "relevance_score": 0.80},
                        {"index": 99, "relevance_score": 0.99},
                    ]
                })

            reranker = Qwen3Reranker(
                api_key="test-key",
                endpoint="https://workspace.example/compatible-api/v1",
                transport=httpx.MockTransport(handler),
            )
            manager = object.__new__(MCPToolManager)
            manager._reranker = reranker
            try:
                return await manager._rerank("query", ["a", "b", "c", "d", "e"], 4)
            finally:
                await reranker.aclose()

        result, reranked = asyncio.run(run())
        self.assertTrue(reranked)
        self.assertEqual(result, ["c", "a", "b", "d"])

    def test_reranker_failure_preserves_rrf_order(self):
        async def run():
            async def handler(request):
                return httpx.Response(500, json={"message": "temporary failure"})

            reranker = Qwen3Reranker(
                api_key="test-key",
                endpoint="https://workspace.example/compatible-api/v1/reranks",
                transport=httpx.MockTransport(handler),
            )
            manager = object.__new__(MCPToolManager)
            manager._reranker = reranker
            try:
                return await manager._rerank("query", ["a", "b", "c"], 2)
            finally:
                await reranker.aclose()

        result, reranked = asyncio.run(run())
        self.assertFalse(reranked)
        self.assertEqual(result, ["a", "b"])

    def test_qwen_reranker_adds_scores_without_mutating_source_items(self):
        async def run():
            async def handler(request):
                return httpx.Response(200, json={
                    "results": [
                        {"index": 1, "relevance_score": 0.95},
                        {"index": 0, "relevance_score": 0.20},
                    ]
                })

            reranker = Qwen3Reranker(
                api_key="test-key",
                endpoint="https://workspace.example/compatible-api/v1",
                transport=httpx.MockTransport(handler),
            )
            manager = object.__new__(MCPToolManager)
            manager._reranker = reranker
            source = [record("a"), record("b")]
            try:
                result, succeeded = await manager._rerank("query", source, 2)
                return source, result, succeeded
            finally:
                await reranker.aclose()

        source, result, succeeded = asyncio.run(run())
        self.assertTrue(succeeded)
        self.assertEqual([item["chunk_id"] for item in result], ["b", "a"])
        self.assertEqual(result[0]["rerank_score"], 0.95)
        self.assertEqual(result[0]["rerank_model"], "qwen3-rerank")
        self.assertNotIn("rerank_score", source[1])

    def test_evaluator_scores_exact_subset_and_no_match(self):
        async def search(query, top_k):
            if query == "missing-id":
                return []
            return [{"chunk_id": "c1", "document_id": "d1"}]

        report = asyncio.run(evaluate_retrieval([
            {
                "id": "exact",
                "query": "ERR_X91",
                "expected_document_id": "d1",
                "top_k": 3,
                "tags": ["exact_identifier"],
            },
            {
                "id": "missing",
                "query": "missing-id",
                "expect_no_match": True,
                "top_k": 3,
            },
        ], search))
        self.assertEqual(report["exact_identifier_top_3_hit_rate"], 1.0)
        self.assertEqual(report["no_match_accuracy"], 1.0)


if __name__ == "__main__":
    unittest.main()
