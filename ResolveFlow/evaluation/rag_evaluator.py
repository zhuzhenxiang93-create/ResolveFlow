"""RAG 独立验收：验证目标演示政策是否进入 top-k 检索结果。"""
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List


def load_rag_queries(path: Path) -> List[Dict[str, Any]]:
    records = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record = json.loads(line)
        for field in ("id", "query", "top_k"):
            if field not in record:
                raise ValueError(f"{path}:{line_number} 缺少 {field}")
        if not (
            record.get("expected_document_id")
            or record.get("expected_chunk_id")
            or record.get("expect_no_match") is True
        ):
            raise ValueError(
                f"{path}:{line_number} 必须提供 expected_document_id、"
                "expected_chunk_id 或 expect_no_match=true"
            )
        records.append(record)
    return records


def _percentile(values: List[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return round(ordered[0], 2)
    rank = (len(ordered) - 1) * pct / 100
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 2)


async def evaluate_retrieval(
    queries: Iterable[Dict[str, Any]],
    search: Callable[[str, int], Awaitable[List[Dict[str, Any]]]],
    *,
    top_ks: List[int] = None,
) -> Dict[str, Any]:
    top_ks = sorted(set(top_ks or [1, 3, 5]))
    max_k = max(top_ks) if top_ks else 5
    results = []
    for item in queries:
        requested_k = max(max_k, int(item.get("top_k", max_k)))
        started = time.monotonic()
        matches = await search(item["query"], requested_k)
        latency_ms = (time.monotonic() - started) * 1000
        expect_no_match = item.get("expect_no_match") is True
        expected_chunk_id = item.get("expected_chunk_id")
        expected = expected_chunk_id or item.get("expected_document_id")
        result_field = "chunk_id" if expected_chunk_id else "document_id"
        returned_document_ids = [row.get("document_id") for row in matches]
        returned_chunk_ids = [row.get("chunk_id") for row in matches]
        returned_ids = returned_chunk_ids if expected_chunk_id else returned_document_ids
        rank = returned_ids.index(expected) + 1 if expected in returned_ids else None
        hits = {
            f"top_{k}_hit": bool(rank and rank <= k) if not expect_no_match else None
            for k in top_ks
        }
        passed = not matches if expect_no_match else bool(
            rank and rank <= int(item.get("top_k", max_k))
        )
        results.append({
            "id": item["id"],
            "query": item["query"],
            "expected_document_id": item.get("expected_document_id"),
            "expected_chunk_id": expected_chunk_id,
            "expect_no_match": expect_no_match,
            "tags": list(item.get("tags") or []),
            "returned_document_ids": returned_document_ids,
            "returned_chunk_ids": returned_chunk_ids,
            "retrieval_diagnostics": [
                {
                    "chunk_id": row.get("chunk_id"),
                    "document_id": row.get("document_id"),
                    "vector_rank": row.get("vector_rank"),
                    "lexical_rank": row.get("lexical_rank"),
                    "rrf_score": row.get("rrf_score"),
                }
                for row in matches
            ],
            "rank": rank,
            "reciprocal_rank": round(1 / rank, 4) if rank else 0.0,
            "latency_ms": round(latency_ms, 2),
            "passed": passed,
            **hits,
        })
    total = len(results)
    passed = sum(1 for item in results if item["passed"])
    latencies = [float(item["latency_ms"]) for item in results]
    retrieval_results = [item for item in results if not item["expect_no_match"]]
    retrieval_total = len(retrieval_results)
    no_match_results = [item for item in results if item["expect_no_match"]]
    top_k_rates = {
        f"top_{k}_hit_rate": round(
            sum(1 for item in retrieval_results if item.get(f"top_{k}_hit")) / retrieval_total,
            4,
        ) if retrieval_total else 0.0
        for k in top_ks
    }
    exact_results = [item for item in retrieval_results if "exact_identifier" in item["tags"]]
    exact_top_3 = (
        round(sum(1 for item in exact_results if item.get("top_3_hit")) / len(exact_results), 4)
        if exact_results else None
    )
    return {
        "total": total,
        "passed": passed,
        "top_k_hit_rate": round(passed / total, 4) if total else 0.0,
        **top_k_rates,
        "mrr": round(
            sum(float(item["reciprocal_rank"]) for item in retrieval_results) / retrieval_total, 4
        ) if retrieval_total else 0.0,
        "exact_identifier_total": len(exact_results),
        "exact_identifier_top_3_hit_rate": exact_top_3,
        "no_match_total": len(no_match_results),
        "no_match_accuracy": (
            round(sum(1 for item in no_match_results if item["passed"]) / len(no_match_results), 4)
            if no_match_results else None
        ),
        "avg_latency_ms": round(sum(latencies) / total, 2) if latencies else 0.0,
        "p95_latency_ms": _percentile(latencies, 95),
        "failed": [item for item in results if not item["passed"]],
        "results": results,
    }
