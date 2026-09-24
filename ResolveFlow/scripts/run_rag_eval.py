"""在隔离本地 ChromaDB 中导入演示政策并运行 RAG 验收。"""
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from evaluation.rag_evaluator import evaluate_retrieval, load_rag_queries


def _load_documents() -> List[Dict[str, Any]]:
    documents: List[Dict[str, Any]] = []
    for path in sorted((ROOT / "data/knowledge").glob("*_policy_v1.json")):
        documents.extend(json.loads(path.read_text(encoding="utf-8")))
    return documents


def _lexical_search_factory(documents: List[Dict[str, Any]]):
    """无 ChromaDB 依赖的 smoke 检索器，用于验证指标链路，不代表最终向量检索效果。"""
    async def search(query: str, top_k: int) -> List[Dict[str, Any]]:
        query_lower = query.lower()
        query_chars = set(query_lower)
        rows = []
        for doc in documents:
            terms = [str(term).lower() for term in doc.get("search_terms", [])]
            content = f"{doc.get('title', '')} {doc.get('content', '')}".lower()
            term_hits = sum(1 for term in terms if term and term in query_lower)
            char_overlap = len(query_chars.intersection(set(content))) / max(1, len(query_chars))
            score = term_hits * 2 + char_overlap
            rows.append({
                "document_id": doc.get("id", ""),
                "title": doc.get("title", ""),
                "content": doc.get("content", ""),
                "score": round(score, 4),
            })
        rows.sort(key=lambda item: item["score"], reverse=True)
        return rows[:top_k]
    return search


async def main() -> None:
    documents = _load_documents()
    retriever_mode = "chromadb_hybrid"
    index_started = time.monotonic()
    index_build_latency_ms = None
    try:
        from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

        # ChromaDB 0.5.x 默认写入 ~/.cache；评测缓存固定在仓库内，便于沙箱和 CI 复现。
        ONNXMiniLM_L6_V2.DOWNLOAD_PATH = ROOT / "data" / "eval" / ".cache" / "onnx_models" / ONNXMiniLM_L6_V2.MODEL_NAME
        from mcp.knowledge_base import KnowledgeBase

        isolated_path = os.getenv("RESOLVEFLOW_RAG_EVAL_CHROMA_PATH") or tempfile.mkdtemp(prefix="resolveflow_rag_eval_")
        kb = KnowledgeBase(
            chroma_host="127.0.0.1",
            chroma_port=65535,
            chroma_path=isolated_path,
            load_default_docs=False,
        )
        kb.add_documents(documents)
        index_build_latency_ms = round((time.monotonic() - index_started) * 1000, 2)
        search = kb.search_async
    except ModuleNotFoundError as ex:
        if ex.name != "chromadb":
            raise
        retriever_mode = "lexical_fallback"
        search = _lexical_search_factory(documents)

    reranker = None
    if os.getenv("RAG_EVAL_USE_QWEN_RERANK", "0").strip().lower() in {"1", "true", "yes"}:
        from mcp.qwen_reranker import Qwen3Reranker

        reranker = Qwen3Reranker.from_env(
            fallback_api_key=os.getenv("LLM_API_KEY", "")
        )
        if reranker is None:
            raise SystemExit(
                "RAG_EVAL_USE_QWEN_RERANK=1，但缺少 QWEN_RERANK_BASE_URL "
                "或 DASHSCOPE_WORKSPACE_ID"
            )
        base_search = search
        candidate_k = max(2, int(os.getenv("QWEN_RERANK_CANDIDATE_K", "20")))

        async def reranked_search(query: str, top_k: int) -> List[Dict[str, Any]]:
            candidates = await base_search(query, max(top_k, candidate_k))
            if len(candidates) <= 1:
                return candidates[:top_k]
            ranked = await reranker.rerank(query, candidates)
            order = [item.index for item in ranked]
            seen = set(order)
            order.extend(index for index in range(len(candidates)) if index not in seen)
            return [candidates[index] for index in order[:top_k]]

        search = reranked_search
        retriever_mode = f"{retriever_mode}_qwen3_rerank"

    query_paths = [
        ROOT / "data/eval/rag_queries.jsonl",
        ROOT / "data/eval/rag_robustness_queries.jsonl",
    ]
    queries = []
    for query_path in query_paths:
        if query_path.exists():
            queries.extend(load_rag_queries(query_path))
    warmup_started = time.monotonic()
    if documents:
        await search(str(documents[0].get("title", "warmup")), 1)
    warmup_query_latency_ms = round((time.monotonic() - warmup_started) * 1000, 2)
    report = await evaluate_retrieval(queries, search)
    if reranker is not None:
        await reranker.aclose()
    reports_dir = ROOT / "data/eval/reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{datetime.now().strftime('%Y%m%dT%H%M%S')}_rag.json"
    latest_path = reports_dir / "latest_rag.json"
    payload = {
        "timestamp": datetime.now().isoformat(),
        "dataset": "internal golden set",
        "retriever_mode": retriever_mode,
        "index_build_latency_ms": index_build_latency_ms,
        "warmup_query_latency_ms": warmup_query_latency_ms,
        **report,
    }
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "dataset": "internal golden set",
        "retriever_mode": retriever_mode,
        "total": report["total"],
        "passed": report["passed"],
        "top_1_hit_rate": report["top_1_hit_rate"],
        "top_3_hit_rate": report["top_3_hit_rate"],
        "top_5_hit_rate": report["top_5_hit_rate"],
        "mrr": report["mrr"],
        "exact_identifier_top_3_hit_rate": report["exact_identifier_top_3_hit_rate"],
        "no_match_accuracy": report["no_match_accuracy"],
        "index_build_latency_ms": index_build_latency_ms,
        "warmup_query_latency_ms": warmup_query_latency_ms,
        "avg_latency_ms": report["avg_latency_ms"],
        "p95_latency_ms": report["p95_latency_ms"],
        "report_path": str(report_path),
        "failed_count": len(report["failed"]),
        "failed_ids": [item["id"] for item in report["failed"][:20]],
    }, ensure_ascii=False, indent=2))
    print(
        "Resume-safe summary on internal golden set: "
        f"retriever_mode={retriever_mode}, "
        f"Top-3 Hit Rate={report['top_3_hit_rate']:.1%}, "
        f"Top-5 Hit Rate={report['top_5_hit_rate']:.1%}, "
        f"MRR={report['mrr']:.3f}."
    )
    if retriever_mode.startswith("chromadb"):
        failures = []
        if report["top_3_hit_rate"] < 0.90:
            failures.append("Top-3 Hit Rate 未达到 90%")
        if report["mrr"] < 0.80:
            failures.append("MRR 未达到 0.80")
        exact_rate = report.get("exact_identifier_top_3_hit_rate")
        if exact_rate is not None and exact_rate < 0.95:
            failures.append("精确标识符 Top-3 Hit Rate 未达到 95%")
        if failures:
            raise SystemExit("; ".join(failures))


if __name__ == "__main__":
    asyncio.run(main())
