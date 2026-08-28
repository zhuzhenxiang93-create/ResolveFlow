"""Qwen3 专用文本重排客户端。

该模块只负责把已经召回的有限候选发送给 qwen3-rerank。任何网络、鉴权或
响应解析错误都向调用方抛出，由 MCPToolManager 统一降级到 RRF 顺序。
"""
from __future__ import annotations

import asyncio
import math
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

import httpx


DEFAULT_INSTRUCT = (
    "Given a customer-support question, rank passages by whether they directly and "
    "correctly answer the question. Prioritize exact policy conditions, negation, "
    "numbers, identifiers, exceptions, and safety constraints over general topical "
    "similarity."
)


@dataclass(frozen=True)
class RerankResult:
    index: int
    score: float


class Qwen3Reranker:
    """异步调用阿里云百炼 qwen3-rerank 的轻量客户端。"""

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        model: str = "qwen3-rerank",
        timeout_s: float = 5.0,
        instruct: str = DEFAULT_INSTRUCT,
        max_document_chars: int = 12_000,
        max_retries: int = 1,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Qwen reranker API Key 不能为空")
        if not endpoint.strip():
            raise ValueError("Qwen reranker endpoint 不能为空")
        if max_document_chars <= 0:
            raise ValueError("max_document_chars 必须大于 0")

        self.model = model.strip() or "qwen3-rerank"
        self.endpoint = self._normalize_endpoint(endpoint)
        self.instruct = instruct.strip()
        self.max_document_chars = max_document_chars
        self.max_retries = max(0, max_retries)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            headers={
                "Authorization": f"Bearer {api_key.strip()}",
                "Content-Type": "application/json",
            },
            transport=transport,
        )

    @staticmethod
    def _normalize_endpoint(value: str) -> str:
        endpoint = value.strip().rstrip("/")
        if endpoint.endswith("/reranks"):
            return endpoint
        if endpoint.endswith("/compatible-api/v1"):
            return f"{endpoint}/reranks"
        raise ValueError(
            "QWEN_RERANK_BASE_URL 必须以 /compatible-api/v1 或 /reranks 结尾"
        )

    @classmethod
    def from_env(cls, *, fallback_api_key: str = "") -> Optional["Qwen3Reranker"]:
        """从环境变量构建客户端；配置不足时返回 None，让主链路使用 RRF。"""
        api_key = (
            os.getenv("QWEN_RERANK_API_KEY")
            or os.getenv("DASHSCOPE_API_KEY")
            or fallback_api_key
        ).strip()
        base_url = (
            os.getenv("QWEN_RERANK_BASE_URL")
            or os.getenv("RERANK_BASE_URL")
            or ""
        ).strip()

        if not base_url:
            workspace_id = (
                os.getenv("DASHSCOPE_WORKSPACE_ID")
                or os.getenv("QWEN_WORKSPACE_ID")
                or ""
            ).strip()
            region = os.getenv("DASHSCOPE_REGION", "cn-beijing").strip()
            if workspace_id:
                base_url = (
                    f"https://{workspace_id}.{region}.maas.aliyuncs.com"
                    "/compatible-api/v1"
                )

        if not api_key or not base_url:
            return None

        return cls(
            api_key=api_key,
            endpoint=base_url,
            model=os.getenv("QWEN_RERANK_MODEL", "qwen3-rerank"),
            timeout_s=float(os.getenv("QWEN_RERANK_TIMEOUT_SECONDS", "5")),
            instruct=os.getenv("QWEN_RERANK_INSTRUCT", DEFAULT_INSTRUCT),
            max_document_chars=int(
                os.getenv("QWEN_RERANK_MAX_DOCUMENT_CHARS", "12000")
            ),
            max_retries=int(os.getenv("QWEN_RERANK_MAX_RETRIES", "1")),
        )

    @staticmethod
    def _document_text(item: Any) -> str:
        if not isinstance(item, Mapping):
            return str(item)
        title = str(item.get("title", "")).strip()
        content = str(item.get("content", "")).strip()
        metadata = item.get("metadata")
        useful_metadata: Dict[str, Any] = {}
        if isinstance(metadata, Mapping):
            for key in ("category", "product", "document_type", "language"):
                value = metadata.get(key)
                if value not in (None, ""):
                    useful_metadata[key] = value
        parts = []
        if title:
            parts.append(f"Title: {title}")
        if useful_metadata:
            parts.append(f"Metadata: {useful_metadata}")
        if content:
            parts.append(f"Content: {content}")
        return "\n".join(parts) or str(dict(item))

    async def rerank(
        self,
        query: str,
        items: Sequence[Any],
    ) -> List[RerankResult]:
        if not items:
            return []

        documents = [
            self._document_text(item)[: self.max_document_chars] for item in items
        ]
        payload: Dict[str, Any] = {
            "model": self.model,
            "query": query,
            "documents": documents,
            # 请求全部候选的分数，避免服务端 top_n 截断导致证据静默丢失。
            "top_n": len(documents),
        }
        if self.instruct:
            payload["instruct"] = self.instruct

        response = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.post(self.endpoint, json=payload)
                response.raise_for_status()
                break
            except (httpx.RequestError, httpx.HTTPStatusError) as ex:
                status = ex.response.status_code if isinstance(ex, httpx.HTTPStatusError) else None
                retryable = status is None or status == 429 or status >= 500
                if not retryable or attempt >= self.max_retries:
                    raise
                await asyncio.sleep(0.2 * (2 ** attempt))
        if response is None:  # pragma: no cover - 防御性保护
            raise RuntimeError("qwen3-rerank 请求未产生响应")
        body = response.json()
        raw_results = body.get("results") if isinstance(body, Mapping) else None
        if not isinstance(raw_results, list):
            raise ValueError("qwen3-rerank 响应缺少 results 数组")

        parsed: List[RerankResult] = []
        seen = set()
        for raw in raw_results:
            if not isinstance(raw, Mapping):
                continue
            index = raw.get("index")
            score = raw.get("relevance_score")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < len(items)
                or index in seen
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
            ):
                continue
            seen.add(index)
            parsed.append(RerankResult(index=index, score=float(score)))

        if not parsed:
            raise ValueError("qwen3-rerank 未返回任何有效候选")
        parsed.sort(key=lambda item: (-item.score, item.index))
        return parsed

    async def aclose(self) -> None:
        await self._client.aclose()
