"""
RAG 知识库 —— 基于 ChromaDB 的真实检索实现。

功能：
  1. 文档导入：将文本切片后同步写入 ChromaDB 与进程内 BM25 索引
  2. 双路召回：独立执行 cosine 向量检索和中英混合 BM25 检索
  3. 排名融合：按稳定 chunk_id 去重并使用加权 RRF 融合
  4. 与 MCP 工具框架集成：作为 knowledge_search 工具的真实 handler

ChromaDB 在这里的角色：
  - memory/ 中用于存储对话记忆（情景记忆 + 用户画像）
  - 这里用于存储知识库文档（RAG 检索）
  两者是不同的 collection，互不干扰。
"""
import asyncio
import hashlib
import json
import logging
import threading
from typing import Any, Dict, Iterable, List, Optional

import chromadb
from chromadb.errors import InvalidCollectionException
from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

from mcp.hybrid_retriever import (
    BM25Index,
    contains_cjk,
    extract_identifiers,
    normalize_text,
    reciprocal_rank_fusion,
)

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """
    基于 ChromaDB 的 RAG 知识库。

    ChromaDB 内置了 Embedding 模型（all-MiniLM-L6-v2），
    调用 add() 时自动生成向量，query() 时自动做语义匹配。
    不需要额外调用 Anthropic Embeddings API。
    """

    # Chroma's distance space is immutable after collection creation.  A
    # versioned name lets us migrate legacy L2 data without deleting it.
    COLLECTION_NAME = "knowledge_base_cosine_v1"
    LEGACY_COLLECTION_NAME = "knowledge_base"
    DISTANCE_SPACE = "cosine"
    CHUNK_SIZE = 500      # 每个切片的目标字符数
    CHUNK_OVERLAP = 100   # 相邻切片之间重叠的字符数，避免跨切片边界的语义被硬切断

    def __init__(
        self,
        chroma_host: str = "localhost",
        chroma_port: int = 8000,
        chroma_path: str = "./data/chroma",
        load_default_docs: bool = True,
        rrf_c: float = 60.0,
        candidate_multiplier: int = 4,
        min_candidates: int = 20,
        cjk_lexical_weight: float = 8.0,
        identifier_lexical_weight: float = 10.0,
    ):
        self._lock = threading.RLock()
        self._rrf_c = float(rrf_c)
        self._candidate_multiplier = max(1, int(candidate_multiplier))
        self._min_candidates = max(1, int(min_candidates))
        self._cjk_lexical_weight = max(0.0, float(cjk_lexical_weight))
        self._identifier_lexical_weight = max(0.0, float(identifier_lexical_weight))
        self._lexical_index = BM25Index()
        # Pin CPU for deterministic local/CI behavior.  Chroma otherwise picks
        # every available provider; CoreML can fail while compiling this model
        # in sandboxed macOS environments.
        self._embedding_function = ONNXMiniLM_L6_V2(
            preferred_providers=["CPUExecutionProvider"]
        )
        # 优先连接独立 ChromaDB 服务（服务端内置 embedding 模型，客户端无需下载）
        self._use_server = False
        try:
            # HttpClient 默认也会初始化 ChromaDB telemetry；显式关闭避免 posthog 兼容性错误日志。
            self._client = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._client.heartbeat()
            self._use_server = True
            logger.info(f"知识库 ChromaDB 已连接: {chroma_host}:{chroma_port}")
        except Exception as ex:
            logger.info(
                "知识库 ChromaDB 服务不可用，使用本地模式 %s（%s）",
                chroma_path,
                type(ex).__name__,
            )
            self._client = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # hnsw:space is immutable.  This versioned collection is explicitly
        # cosine, unlike the legacy collection which used Chroma's L2 default.
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata={
                "description": "ResolveFlow RAG hybrid knowledge base",
                "hnsw:space": self.DISTANCE_SPACE,
                "schema_version": "2",
            },
            embedding_function=self._embedding_function,
        )
        actual_space = (self._collection.metadata or {}).get("hnsw:space")
        if actual_space != self.DISTANCE_SPACE:
            raise RuntimeError(
                f"Collection {self.COLLECTION_NAME!r} uses {actual_space!r}; "
                f"expected immutable distance space {self.DISTANCE_SPACE!r}"
            )

        if self._collection.count() == 0:
            self._migrate_legacy_collection()
        self._refresh_lexical_index()

        # 如果知识库为空，导入默认文档
        if load_default_docs and self._collection.count() == 0:
            self._load_default_docs()

    # ── 文档管理 ──────────────────────────────────────────────────────────────

    def add_documents(self, documents: List[Dict[str, Any]]) -> int:
        """Atomically update the vector collection and its BM25 mirror."""
        with self._lock:
            return self._add_documents_locked(documents)

    def _add_documents_locked(self, documents: List[Dict[str, Any]]) -> int:
        """
        批量导入文档到知识库。

        documents 格式: [{"title": "...", "content": "..."}, ...]
        长文档会自动切片（每片约 500 字，相邻切片重叠约 100 字）。
        """
        ids, docs, metas, stale_ids = [], [], [], []

        for doc in documents:
            source_doc_id = doc.get("id") or hashlib.md5(
                f"{doc.get('title', '')}_{doc.get('content', '')[:80]}".encode()
            ).hexdigest()
            title   = doc.get("title", "")
            content = doc.get("content", "")
            chunks  = self._chunk_text(content, chunk_size=self.CHUNK_SIZE, overlap=self.CHUNK_OVERLAP)

            new_source_ids = []
            for i, chunk in enumerate(chunks):
                chunk_id = f"{source_doc_id}::chunk::{i}"
                new_source_ids.append(chunk_id)
                ids.append(chunk_id)
                docs.append(chunk)
                metas.append({
                    "document_id": source_doc_id,
                    "title": title,
                    "category": doc.get("category", ""),
                    "risk_level": doc.get("risk_level", ""),
                    "source": doc.get("source", ""),
                    "search_terms": json.dumps(doc.get("search_terms", []), ensure_ascii=False),
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                })
            if new_source_ids:
                existing = self._collection.get(
                    where={"document_id": source_doc_id}, include=["metadatas"]
                )
                stale_ids.extend(
                    chunk_id for chunk_id in existing.get("ids", [])
                    if chunk_id not in set(new_source_ids)
                )

        if ids:
            # ChromaDB generates normalized MiniLM embeddings.  The collection
            # explicitly interprets their distance as cosine distance.
            with self._lock:
                self._collection.upsert(ids=ids, documents=docs, metadatas=metas)
                if stale_ids:
                    self._collection.delete(ids=list(dict.fromkeys(stale_ids)))
                self._refresh_lexical_index()
            logger.info(f"知识库导入 {len(ids)} 个文档片段")

        return len(ids)

    async def add_documents_async(self, documents: List[Dict[str, Any]]) -> int:
        """异步导入文档；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.add_documents, documents)

    @classmethod
    def lexical_documents(cls, documents):
        """Explicit dependency-light backend using the same lexical index contract."""
        instance = cls.__new__(cls)
        instance._lexical_only = True
        instance._lexical_index = BM25Index()
        instance._lexical_index.upsert([instance._result_record(d["id"], d["content"],
            {"document_id": d["id"], "title": d["title"], "search_terms": d.get("search_terms", [])}) for d in documents])
        return instance

    def search(
        self,
        query: str,
        top_k: int = 5,
        allowed_document_ids: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Search without serializing concurrent readers behind mutation locks.

        ``allowed_document_ids``, when given, restricts recall itself (a
        ChromaDB ``where`` clause plus a lexical post-filter) instead of
        discarding out-of-scope hits after the fact — the caller gets back
        only candidates from the intended document set, or nothing.
        """
        scope = set(allowed_document_ids) if allowed_document_ids is not None else None
        if scope is not None and not scope:
            return []
        if getattr(self, "_lexical_only", False):
            hits = self._lexical_index.search(query, top_k * 4 if scope else top_k)
            if scope is not None:
                hits = [h for h in hits if h.get("document_id") in scope]
            return hits[:top_k]
        return self._search_impl(query, top_k, scope)

    def _search_impl(
        self,
        query: str,
        top_k: int = 5,
        allowed_document_ids: Optional[set] = None,
    ) -> List[Dict[str, Any]]:
        """Run independent vector/BM25 recall and fuse candidates with RRF."""
        query = str(query or "").strip()
        top_k = max(0, int(top_k))
        if not query or top_k == 0:
            return []
        count = self._collection.count()
        candidate_count = min(
            count,
            max(top_k * self._candidate_multiplier, top_k, self._min_candidates),
        )
        if candidate_count <= 0:
            return []

        rankings: Dict[str, List[Dict[str, Any]]] = {}
        degradations: List[str] = []
        try:
            rankings["vector"] = (
                self._vector_search(query, candidate_count, allowed_document_ids)
                if allowed_document_ids is not None
                else self._vector_search(query, candidate_count)
            )
        except Exception as ex:
            degradations.append("vector_unavailable")
            logger.warning("向量召回失败，降级为关键词召回: %s", type(ex).__name__)
        try:
            lexical_candidate_count = candidate_count * 4 if allowed_document_ids is not None else candidate_count
            rankings["lexical"] = self._lexical_search(query, lexical_candidate_count)
            if allowed_document_ids is not None:
                rankings["lexical"] = [
                    item for item in rankings["lexical"] if item.get("document_id") in allowed_document_ids
                ][:candidate_count]
        except Exception as ex:
            degradations.append("lexical_unavailable")
            logger.warning("关键词召回失败，降级为向量召回: %s", type(ex).__name__)

        available = {route: items for route, items in rankings.items() if items}
        if not available:
            raise RuntimeError("向量与关键词召回均不可用或没有候选")
        identifiers = extract_identifiers(query)
        # An opaque identifier-only query should not be answered from a merely
        # semantic neighbor.  An exact miss is safer and allows the caller to
        # ask for clarification or use its normal fallback.
        if identifiers and normalize_text(query) in identifiers:
            lexical_items = rankings.get("lexical", [])
            if not any(item.get("exact_identifier_hits", 0) for item in lexical_items):
                return []
        lexical_weight = 1.0
        # all-MiniLM-L6-v2 is English-centric.  On the project's Chinese
        # support corpus, calibrated BM25 is the more reliable first-stage
        # ranker; exact opaque identifiers are lexical by definition.
        if contains_cjk(query):
            lexical_weight = self._cjk_lexical_weight
        if identifiers:
            lexical_weight = max(lexical_weight, self._identifier_lexical_weight)
        route_weights = {"vector": 1.0, "lexical": lexical_weight}
        fused = reciprocal_rank_fusion(
            available,
            top_k=top_k,
            c=self._rrf_c,
            route_weights=route_weights,
        )
        for item in fused:
            item["retrieval_routes"] = [
                route for route in ("vector", "lexical") if item.get(f"{route}_rank") is not None
            ]
            item["degradations"] = list(degradations)
            item["fusion_weights"] = dict(route_weights)
        return fused

    def _vector_search(
        self,
        query: str,
        top_k: int,
        allowed_document_ids: Optional[set] = None,
    ) -> List[Dict[str, Any]]:
        where = {"document_id": {"$in": list(allowed_document_ids)}} if allowed_document_ids is not None else None
        results = self._collection.query(
            query_texts=[query],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        ids = results.get("ids", [[]])[0]
        documents = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]
        distances = results.get("distances", [[]])[0]
        items = []
        for chunk_id, content, metadata, distance in zip(ids, documents, metadatas, distances):
            # For cosine space Chroma returns cosine distance, so 1-distance is
            # cosine similarity and is bounded approximately to [-1, 1].
            vector_score = 1.0 - float(distance)
            items.append(self._result_record(
                str(chunk_id), str(content), metadata or {}, vector_score=round(vector_score, 6)
            ))
        return items

    def _lexical_search(self, query: str, top_k: int) -> List[Dict[str, Any]]:
        return self._lexical_index.search(query, top_k)

    async def search_async(
        self,
        query: str,
        top_k: int = 5,
        allowed_document_ids: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        """异步检索；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.search, query, top_k, allowed_document_ids)

    async def doc_count_async(self) -> int:
        """异步获取文档片段数量。"""
        return await asyncio.to_thread(self._collection.count)

    @staticmethod
    def _result_record(
        chunk_id: str,
        content: str,
        metadata: Dict[str, Any],
        *,
        vector_score: Optional[float] = None,
    ) -> Dict[str, Any]:
        clean_metadata = dict(metadata)
        return {
            "chunk_id": chunk_id,
            "document_id": clean_metadata.get("document_id", ""),
            "title": clean_metadata.get("title", ""),
            "source": clean_metadata.get("source", ""),
            "content": content,
            "metadata": clean_metadata,
            "vector_rank": None,
            "lexical_rank": None,
            "vector_score": vector_score,
            "lexical_score": None,
            # Backward-compatible API aliases.
            "chunk": clean_metadata.get("chunk_index", 0),
            "total_chunks": clean_metadata.get("total_chunks", 1),
        }

    def _refresh_lexical_index(self) -> None:
        """Refresh BM25 after startup/migration or a batch mutation, never per query."""
        payload = self._collection.get(include=["documents", "metadatas"])
        records = [
            self._result_record(str(chunk_id), str(content), metadata or {})
            for chunk_id, content, metadata in zip(
                payload.get("ids", []),
                payload.get("documents", []) or [],
                payload.get("metadatas", []) or [],
            )
        ]
        self._lexical_index = BM25Index()
        self._lexical_index.upsert(records)

    def _migrate_legacy_collection(self) -> int:
        """Re-embed the legacy L2 collection into the versioned cosine collection."""
        try:
            legacy = self._client.get_collection(
                self.LEGACY_COLLECTION_NAME,
                embedding_function=self._embedding_function,
            )
        except InvalidCollectionException:
            return 0
        if legacy.count() <= 0:
            return 0
        payload = legacy.get(include=["documents", "metadatas"])
        ids = [str(value) for value in payload.get("ids", [])]
        documents = [str(value) for value in payload.get("documents", []) or []]
        metadatas = [dict(value or {}) for value in payload.get("metadatas", []) or []]
        if not ids:
            return 0
        self._collection.upsert(ids=ids, documents=documents, metadatas=metadatas)
        logger.info("已从旧 L2 collection 迁移并重新向量化 %d 个片段", len(ids))
        return len(ids)

    def delete_documents(self, document_ids: List[str]) -> int:
        """Delete source documents from both vector and lexical indexes."""
        deleted = 0
        with self._lock:
            for document_id in {str(value) for value in document_ids if str(value).strip()}:
                existing = self._collection.get(
                    where={"document_id": document_id}, include=["metadatas"]
                )
                ids = existing.get("ids", [])
                if ids:
                    self._collection.delete(ids=ids)
                    deleted += len(ids)
            self._refresh_lexical_index()
        return deleted

    # ── MCP 工具 handler ─────────────────────────────────────────────────────

    async def search_handler(self, params: Dict[str, Any], context: Any) -> List[Dict]:
        """
        作为 MCP 工具的 handler 注册。

        MCPToolManager.register(Tool(
            name="knowledge_search",
            handler=kb.search_handler,
            ...
        ))
        """
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        return await self.search_async(query, top_k=top_k, allowed_document_ids=params.get("allowed_document_ids"))

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    def _chunk_text(self, text: str, chunk_size: int = 500, overlap: int = 0) -> List[str]:
        """
        将长文本按 chunk_size 切片，保留语义完整性（按句号/换行切分）。

        相邻切片之间保留约 overlap 个字符的重叠——切片时用尾部完整句子回补下一片，
        不切半句，避免一个事实/条件恰好落在切片边界导致任何一侧单独检索都缺上下文。
        overlap 会被限制在 chunk_size 的一半以内，防止配置不当导致切片过度重叠、
        数量暴涨。
        """
        if len(text) <= chunk_size:
            return [text] if text.strip() else []

        overlap = max(0, min(overlap, chunk_size // 2))

        sentences = [s.strip() for s in text.replace("\n", "。").split("。") if s.strip()]
        if not sentences:
            return []

        chunks: List[str] = []
        current: List[str] = []
        current_len = 0

        def flush() -> List[str]:
            """落一个 chunk，并从尾部取够 overlap 长度的完整句子作为下一片的开头。"""
            chunks.append("。".join(current))
            if overlap <= 0:
                return []
            seed: List[str] = []
            seed_len = 0
            for sent in reversed(current):
                if seed_len >= overlap:
                    break
                seed.insert(0, sent)
                seed_len += len(sent) + 1
            return seed

        for sent in sentences:
            if current and current_len + len(sent) + 1 > chunk_size:
                current = flush()
                current_len = sum(len(s) + 1 for s in current)
            current.append(sent)
            current_len += len(sent) + 1

        if current:
            chunks.append("。".join(current))

        return chunks

    def _load_default_docs(self) -> None:
        """导入默认知识库文档（客服场景常见问题）。"""
        default_docs = [
            {
                "title": "退款政策",
                "content": (
                    "退款政策说明（本政策仅适用于站内购买的实物/普通商品订单，不适用于会员费用；"
                    "会员费的重复扣款退款请走会员支持通道，见会员权益与账单相关政策）。"
                    "用户在购买后 7 天内可以申请无理由退款。"
                    "退款申请提交后，系统会在 1-3 个工作日内审核。"
                    "审核通过后，款项将在 5-7 个工作日内退回原支付账户。"
                    "如果商品已发货，需要先完成退货流程才能退款。"
                    "退货运费由用户承担，除非是商品质量问题。"
                    "超过 7 天但未超过 30 天的订单，需要提供商品质量问题的证据才能退款。"
                ),
            },
            {
                "title": "订单查询",
                "content": (
                    "订单查询指南。"
                    "用户可以通过订单号查询订单状态。"
                    "订单状态包括：待支付、已支付、已发货、运输中、已签收、已完成。"
                    "如果订单显示已发货但超过 7 天未收到，可以联系客服申请查件。"
                    "物流信息通常在发货后 24 小时内更新。"
                    "如果订单显示异常，请提供订单号联系客服处理。"
                ),
            },
            {
                "title": "账户安全",
                "content": (
                    "账户安全说明。"
                    "建议用户定期修改密码，密码长度至少 8 位，包含字母和数字。"
                    "如果忘记密码，可以通过绑定的手机号或邮箱重置。"
                    "发现账户异常登录时，系统会自动锁定账户并发送通知。"
                    "用户可以在安全设置中开启两步验证，提高账户安全性。"
                    "不要将密码分享给他人，客服人员不会索要用户密码。"
                ),
            },
            {
                "title": "技术故障排查",
                "content": (
                    "常见技术问题排查。"
                    "应用崩溃：请尝试清除缓存后重启应用，如果问题持续请更新到最新版本。"
                    "登录失败 401 错误：表示认证失败，请检查用户名密码是否正确，或尝试重置密码。"
                    "页面加载慢：检查网络连接，尝试切换 WiFi 或移动数据。"
                    "支付失败：确认银行卡余额充足，检查是否开启了网上支付功能。"
                    "500 服务器错误：这是服务端问题，请稍后重试，如果持续出现请联系技术支持。"
                ),
            },
            {
                "title": "会员与积分",
                "content": (
                    "会员积分规则。"
                    "每消费 1 元累积 1 积分。"
                    "积分可以在下次购物时抵扣，100 积分 = 1 元。"
                    "会员等级分为：普通会员、银卡会员（累计消费 1000 元）、金卡会员（累计消费 5000 元）。"
                    "银卡会员享受 95 折优惠，金卡会员享受 9 折优惠。"
                    "积分有效期为 1 年，过期自动清零。"
                    "生日当月消费可获得双倍积分。"
                ),
            },
            {
                "title": "配送说明",
                "content": (
                    "配送服务说明。"
                    "标准配送：3-5 个工作日送达，免运费（订单满 99 元）。"
                    "加急配送：1-2 个工作日送达，运费 15 元。"
                    "同城配送：当日达或次日达，运费 10 元。"
                    "偏远地区可能需要额外 2-3 天。"
                    "配送时间为每天 9:00-18:00，节假日可能延迟。"
                    "如果需要修改收货地址，请在发货前联系客服。"
                ),
            },
        ]
        self.add_documents(default_docs)
        logger.info(f"已导入默认知识库: {len(default_docs)} 篇文档")
