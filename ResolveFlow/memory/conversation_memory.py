"""
亮点：多轮对话记忆管理

三级记忆架构，模拟人类记忆机制：
  1. 工作记忆（Redis）—— 当前会话的最近 N 条消息，毫秒级读写
  2. 情景记忆（ChromaDB）—— 跨会话的历史对话，按语义相似度检索
  3. 用户画像（ChromaDB）—— 从对话中提炼的长期偏好和实体

关键设计：
  - 上下文构建时三级记忆融合，按重要性 + 时效性排序
  - 工作记忆超过阈值时自动压缩（LLM 摘要），防止 context 爆炸
  - 所有 Embedding 通过 Anthropic API 生成，无本地模型
"""
import hashlib
import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Set

import chromadb
import redis.asyncio as redis

from core.llm_client import LLMClient

logger = logging.getLogger(__name__)


class MsgRole(Enum):
    USER      = "user"
    ASSISTANT = "assistant"
    SYSTEM    = "system"


@dataclass
class Message:
    role:       MsgRole
    content:    str
    timestamp:  datetime = field(default_factory=datetime.now)
    metadata:   Dict[str, Any] = field(default_factory=dict)


# 轻量正则先筛：只看最新一条用户发言像不像在表达长期偏好/习惯，命中就值得
# 立刻触发一次画像提炼，不用等凑够 PROFILE_UPDATE_EVERY 轮。模式沿用
# LocalConversationMemory.update_profile() 里已经验证过的启发式规则。
_PREFERENCE_HINT_RE = re.compile(
    r"请.*(简短|简洁)|我喜欢.*简洁|请.*(详细|具体).*解释|我喜欢.*详细"
    r"|以后.*(请|帮我)|记住我|我通常|我经常|我更喜欢"
)


def _looks_like_preference_statement(messages: List["Message"]) -> bool:
    """只检查最近一条用户消息（这一轮新增的信息），不是历史所有消息。"""
    for m in reversed(messages):
        if m.role != MsgRole.USER:
            continue
        return bool(_PREFERENCE_HINT_RE.search(m.content))
    return False


@dataclass
class MemoryContext:
    """传给 Agent 的完整上下文。"""
    recent_messages:  List[Message]   # 工作记忆：最近对话
    relevant_history: List[str]       # 情景记忆：语义相关的历史片段
    user_profile:     Dict[str, Any]  # 用户画像：偏好、常用实体
    summary:          str             # 当前会话摘要（压缩后）

    @staticmethod
    def _clean(text: str) -> str:
        """移除 Unicode 代理字符，防止编码错误。"""
        return text.encode("utf-8", errors="ignore").decode("utf-8")

    def to_prompt_text(self) -> str:
        """将记忆上下文格式化为 LLM 可用的文本。"""
        parts = []
        if self.summary:
            parts.append(f"[会话摘要]\n{self._clean(self.summary)}")
        if self.relevant_history:
            parts.append("[相关历史]\n" + "\n".join(f"- {self._clean(h)}" for h in self.relevant_history[:3]))
        if self.user_profile:
            parts.append(f"[用户画像]\n{json.dumps(self.user_profile, ensure_ascii=True)}")
        if self.recent_messages:
            parts.append("[最近对话]")
            for m in self.recent_messages:
                parts.append(f"{m.role.value}: {self._clean(m.content)}")
        return "\n\n".join(parts)


class MemoryManager:
    """
    三级记忆管理器。

    工作记忆存 Redis（TTL 24h），情景记忆和用户画像存 ChromaDB（持久化）。
    """

    WORKING_MAX   = 20    # 工作记忆最大条数，超过则触发压缩
    COMPRESS_AT   = 15    # 达到此条数时压缩，保留摘要 + 最近 5 条
    SUMMARY_MAX_CHARS = 800  # 会话摘要上限；每次压缩用 LLM 把新旧摘要合并压回这个长度，
                             # 避免长会话反复压缩后摘要文本无限增长（旧实现是纯字符串拼接）
    HISTORY_TOP_K = 5     # 情景记忆检索返回条数
    PROFILE_UPDATE_EVERY = 5  # 画像提炼节流：正常每 N 轮才真正调一次 LLM，
                              # 命中偏好正则或刚完成一次压缩时提前触发（见 update_profile）

    def __init__(
        self,
        redis_url:    str = "redis://localhost:6379/0",
        chroma_host:  str = "localhost",
        chroma_port:  int = 8000,
        chroma_path:  str = "./data/chroma",
        api_key:      str = "",
        base_url:     Optional[str] = None,
        model:        str = "claude-3-5-sonnet-20241022",
        provider:     Optional[str] = None,
    ):
        self._client = LLMClient(api_key=api_key, base_url=base_url, model=model, provider=provider)
        self._model  = model

        self._redis = redis.from_url(redis_url, decode_responses=True)

        # ChromaDB：优先连接独立服务（docker compose 模式），连不上则降级为本地嵌入式
        try:
            # HttpClient 默认也会初始化 ChromaDB telemetry；显式关闭避免 posthog 兼容性错误日志。
            chroma = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            chroma.heartbeat()  # 测试连接
            logger.info(f"ChromaDB 已连接: {chroma_host}:{chroma_port}")
        except Exception:
            logger.info(f"ChromaDB 服务不可用，使用本地嵌入式模式: {chroma_path}")
            chroma = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # 情景记忆：存储历史对话片段
        self._episodic = chroma.get_or_create_collection("episodic")
        # 用户画像：存储提炼出的偏好和实体
        self._profile  = chroma.get_or_create_collection("user_profile")

        # ── 延迟优化相关状态 ──────────────────────────────────────────────
        # 每个 (user_id, conv_id) 一把锁，防止同一会话被并发触发两次压缩
        # （两个几乎同时到达阈值的请求都去跑 _compress，互相覆盖 Redis 状态）。
        self._compress_locks: Dict[str, asyncio.Lock] = {}
        # 压缩后台任务完成时，把对应 key 记进来，提示下一次 update_profile()
        # "刚发生过一次信息量较大的压缩，顺带做一次画像提炼"。
        self._profile_due: Set[str] = set()
        # 画像提炼节流计数器：每个会话独立计数，达到 PROFILE_UPDATE_EVERY 才
        # 真正调用一次 LLM。
        self._profile_turn_counts: Dict[str, int] = {}
        # 持有后台任务的引用，防止 asyncio 在任务完成前把它垃圾回收。
        self._background_tasks: Set["asyncio.Task"] = set()

    # ── 后台任务调度 ──────────────────────────────────────────────────────────

    def _spawn(self, coro, *, label: str) -> None:
        """把一个协程调度为后台任务，不阻塞调用方等待完成。

        协程内部的业务异常（LLM 调用失败等）应该在协程自己的 try/except 里
        降级处理——这里的 done-callback 只兜底"协程本身抛出了没被处理的异常"
        这种真正意外的情况，避免它安静地消失在事件循环里查不到。
        """
        task = asyncio.ensure_future(coro)
        self._background_tasks.add(task)

        def _on_done(t: "asyncio.Task") -> None:
            self._background_tasks.discard(t)
            if t.cancelled():
                return
            ex = t.exception()
            if ex is not None:
                logger.error(f"后台任务 {label} 异常退出: {ex}")

        task.add_done_callback(_on_done)

    def _get_compress_lock(self, key: str) -> asyncio.Lock:
        lock = self._compress_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._compress_locks[key] = lock
        return lock

    def _schedule_compress(self, user_id: str, conv_id: str) -> bool:
        """把压缩调度为后台任务；如果这个会话已经有一次压缩在跑，直接跳过
        （不排队、不重复触发），返回是否本次调用新调度了一次压缩。"""
        key = f"{user_id}:{conv_id}"
        lock = self._get_compress_lock(key)
        if lock.locked():
            logger.info(f"压缩已在进行中，跳过重复调度: {key}")
            return False

        async def _run() -> None:
            async with lock:
                try:
                    await self._compress(user_id, conv_id)
                except Exception as ex:
                    logger.warning(f"后台压缩失败 {key}: {ex}")
                else:
                    self._profile_due.add(key)

        self._spawn(_run(), label=f"compress:{key}")
        return True

    # ── 写入 ──────────────────────────────────────────────────────────────────

    async def add_message(
        self,
        user_id: str,
        conv_id: str,
        role:    MsgRole,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """将一条消息写入工作记忆；达到压缩阈值时把压缩调度为后台任务，
        不在这次调用里 await 到底（压缩本身要跑 1 次 LLM，同步等待会把这次
        请求的响应时间拖长 1~2 秒，参见 wiki 里记的延迟分析）。

        返回值：这次调用是否新触发了一次压缩调度（供 update_profile 判断
        "刚发生过压缩，值得顺带更新画像"——不过压缩是后台跑的，真正完成时间
        可能晚于这次 add_message 返回，所以这个返回值只是一个即时信号，
        精确的联动靠 _profile_due 在压缩真正完成后设置）。
        """
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        clean_metadata = {
            self._safe_text(k): self._safe_metadata_value(v)
            for k, v in (metadata or {}).items()
        }
        msg = Message(role=role, content=self._safe_text(content), metadata=clean_metadata)
        key = self._wm_key(user_id, conv_id)

        # 追加到 Redis 列表（左推，最新在前）
        await self._redis.lpush(key, json.dumps({
            "role":      msg.role.value,
            "content":   msg.content,
            "ts":        msg.timestamp.isoformat(),
            "metadata":  msg.metadata,
        }))
        await self._redis.expire(key, 86400)  # 24h TTL

        # 超过压缩阈值时，把压缩调度为后台任务，不阻塞这次写入返回
        if await self._redis.llen(key) >= self.COMPRESS_AT:
            return self._schedule_compress(user_id, conv_id)
        return False

    async def update_profile(self, user_id: str, conv_id: str) -> None:
        """
        从当前工作记忆中提炼用户偏好，更新用户画像。

        这一步本身是一次 LLM 调用；如果每轮对话都做一次，等于给每条消息
        都额外加一次模型延迟，但用户画像通常变化很慢，没必要这么频繁。
        这里改成节流触发，命中以下三个条件之一才真正调用 LLM，其余轮次
        只做一次计数器自增（近似零开销），真正的 LLM 提炼调度为后台任务，
        不阻塞这次请求的响应：
          1. 距上次真正提炼已经过了 PROFILE_UPDATE_EVERY 轮（兜底，保证画像
             不会因为一直没命中另外两个条件而永远不更新）
          2. 这个会话刚完成一次工作记忆压缩（说明这段对话信息量已经足够大）
          3. 最新一条用户发言用轻量正则命中了"像是在表达长期偏好/习惯"
        """
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        key = f"{user_id}:{conv_id}"
        self._profile_turn_counts[key] = self._profile_turn_counts.get(key, 0) + 1

        messages = await self._get_working_memory(user_id, conv_id)
        if not messages:
            return

        due_from_compression = key in self._profile_due
        due_from_cadence = self._profile_turn_counts[key] >= self.PROFILE_UPDATE_EVERY
        due_from_heuristic = _looks_like_preference_statement(messages)
        if not (due_from_compression or due_from_cadence or due_from_heuristic):
            return

        self._profile_turn_counts[key] = 0
        self._profile_due.discard(key)
        self._spawn(self._run_profile_update(user_id, conv_id, messages), label=f"profile:{key}")

    async def _run_profile_update(self, user_id: str, conv_id: str, messages: List[Message]) -> None:
        """实际调用 LLM 提炼画像并写入 ChromaDB —— 拆成独立方法是为了能被
        update_profile() 作为后台任务调度，不阻塞调用方等待这次 LLM 调用完成。"""
        text = self._safe_text("\n".join(f"{m.role.value}: {m.content}" for m in messages[-10:]))
        prompt = f"""从以下对话中提炼用户偏好和关键实体，返回 JSON。
对话:
{text}

返回格式: {{"preferences": ["..."], "entities": {{"产品": [], "问题类型": []}}}}"""
        prompt = self._safe_text(prompt)

        try:
            raw = await self._client.create(
                max_tokens=512, temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            s, e = raw.find("{"), raw.rfind("}") + 1
            profile_data = json.loads(raw[s:e])

            doc_id = f"{user_id}_profile_{conv_id}"
            doc_text = self._safe_text(json.dumps(profile_data, ensure_ascii=False))

            try:
                await asyncio.to_thread(self._profile.delete, ids=[doc_id])
            except Exception:
                pass

            # 直接传 documents，让 ChromaDB 内置模型生成 embedding（不依赖 Voyage API）
            await asyncio.to_thread(
                self._profile.add,
                ids=[doc_id],
                documents=[doc_text],
                metadatas=[{"user_id": user_id, "conv_id": conv_id,
                            "ts": datetime.now().isoformat()}],
            )
            logger.info(f"用户画像已更新: {user_id}")
        except Exception as ex:
            logger.warning(f"更新用户画像失败: {ex}")

    # ── 读取 ──────────────────────────────────────────────────────────────────

    async def get_context(self, user_id: str, conv_id: str, query: str = "") -> MemoryContext:
        """
        构建完整的记忆上下文。

        query 用于从情景记忆中检索语义相关的历史片段。
        """
        # 1. 工作记忆（当前会话最近消息）
        user_id = self._safe_text(user_id)
        conv_id = self._safe_text(conv_id)
        query = self._safe_text(query)

        recent = await self._get_working_memory(user_id, conv_id)

        # 2. 情景记忆（跨会话语义检索）
        history = await self._search_episodic(user_id, query or (recent[-1].content if recent else ""))

        # 3. 用户画像
        profile = await self._get_profile(user_id)

        # 4. 会话摘要（如果已压缩过）
        summary = await self._redis.get(self._summary_key(user_id, conv_id)) or ""

        return MemoryContext(
            recent_messages=recent,
            relevant_history=history,
            user_profile=profile,
            summary=summary,
        )

    # ── 压缩（防止 context 爆炸）─────────────────────────────────────────────

    async def _compress(self, user_id: str, conv_id: str) -> None:
        """
        工作记忆压缩：
          1. 一次 LLM 调用，把"已有摘要 + 待压缩的旧消息"直接合并成新的完整
             摘要——取代旧版"先总结新消息、再和旧摘要合并"两次串行 LLM 调用
          2. 摘要存 Redis（覆盖旧摘要）
          3. 旧消息存入情景记忆（ChromaDB）供跨会话检索
          4. 工作记忆只保留最近 5 条

        这个方法现在总是被 _schedule_compress() 作为后台任务调度执行，
        不会阻塞调用 add_message() 的那次请求。
        """
        messages = await self._get_working_memory(user_id, conv_id)
        if len(messages) < self.COMPRESS_AT:
            return

        to_compress = messages[:-5]   # 保留最近 5 条
        keep        = messages[-5:]
        text = self._safe_text("\n".join(f"{m.role.value}: {m.content}" for m in to_compress))

        skey = self._summary_key(user_id, conv_id)
        old_summary = self._safe_text(await self._redis.get(skey) or "")
        new_summary = await self._summarize_single_pass(old_summary, text)
        await self._redis.setex(skey, 86400, new_summary)

        # 旧消息存入情景记忆（存的是新摘要，检索时能看到最新提炼结果）
        await self._store_episodic(user_id, conv_id, text, new_summary)

        # 重置工作记忆为最近 5 条
        key = self._wm_key(user_id, conv_id)
        await self._redis.delete(key)
        for m in reversed(keep):
            await self._redis.lpush(key, json.dumps({
                "role": m.role.value, "content": m.content,
                "ts": m.timestamp.isoformat(), "metadata": m.metadata,
            }))
        await self._redis.expire(key, 86400)
        logger.info(f"工作记忆压缩完成: {user_id}/{conv_id}，摘要 {len(new_summary)} 字")

    async def _summarize_single_pass(self, old_summary: str, new_text: str) -> str:
        """一次 LLM 调用，直接把"已有摘要"和"这次新增的原始对话"合并成一段
        新的完整摘要——取代旧版分两步（先总结新消息成 new_summary，再把
        new_summary 和 old_summary 合并）各调一次模型的做法：语义上是同一件
        事，没必要串行调两次模型，还多引入一次信息压缩损失。

        LLM 调用失败时退化为截断拼接，保留已有摘要内容而不是丢弃。
        """
        old_summary = self._safe_text(old_summary).strip()
        new_text = self._safe_text(new_text).strip()

        if old_summary:
            prompt = f"""你是对话摘要器。这是已有的会话摘要：
{old_summary}

这是新增的对话内容：
{new_text}

请把两者合并为一段新的、完整的会话摘要，不超过 {self.SUMMARY_MAX_CHARS} 个中文字符。
保留：用户偏好、关键实体、待办事项、约束条件、未解决问题。
只输出摘要正文，不要编号，不要解释。"""
        else:
            prompt = f"用 2-3 句话总结以下对话的关键信息，保留用户偏好、关键实体、未解决问题：\n{new_text}"
        prompt = self._safe_text(prompt)

        try:
            raw = await self._client.create(
                max_tokens=256, temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            summary = self._safe_text(raw).strip()
            if summary:
                return summary[: self.SUMMARY_MAX_CHARS]
        except Exception as ex:
            logger.warning(f"生成会话摘要失败，回退为截断拼接: {ex}")

        fallback = f"{old_summary}\n对话包含新增内容（摘要生成失败）" if old_summary else "对话包含新增内容（摘要生成失败）"
        return self._safe_text(fallback).strip()[-self.SUMMARY_MAX_CHARS:]

    # ── 内部辅助 ──────────────────────────────────────────────────────────────

    async def _get_working_memory(self, user_id: str, conv_id: str) -> List[Message]:
        key  = self._wm_key(user_id, conv_id)
        raws = await self._redis.lrange(key, 0, self.WORKING_MAX - 1)
        msgs = []
        for raw in reversed(raws):  # Redis lpush 最新在前，reversed 还原时序
            d = json.loads(raw)
            msgs.append(Message(
                role=MsgRole(d["role"]),
                content=d["content"],
                timestamp=datetime.fromisoformat(d["ts"]),
                metadata=d.get("metadata", {}),
            ))
        return msgs

    async def _search_episodic(self, user_id: str, query: str) -> List[str]:
        """语义检索情景记忆。ChromaDB 内置 embedding，不依赖外部 API。"""
        query_text = self._safe_text(query).strip()
        if not query_text:
            return []
        try:
            # 直接传 query_texts，ChromaDB 内置模型自动生成向量做匹配
            results = await asyncio.to_thread(
                self._episodic.query,
                query_texts=[query_text],
                n_results=self.HISTORY_TOP_K,
                where={"user_id": self._safe_text(user_id)},
            )
            docs = results["documents"][0] if results["documents"] else []
            return [self._safe_text(doc) for doc in docs if isinstance(doc, str) and doc.strip()]
        except Exception as ex:
            logger.warning(f"情景记忆检索失败: {ex}")
            return []

    async def _store_episodic(self, user_id: str, conv_id: str, text: str, summary: str) -> None:
        """将压缩后的对话片段存入情景记忆。ChromaDB 内置 embedding，不依赖外部 API。"""
        try:
            user_id = self._safe_text(user_id)
            conv_id = self._safe_text(conv_id)
            text = self._safe_text(text)
            summary = self._safe_text(summary)
            doc_id = hashlib.md5(f"{user_id}{conv_id}{time.time()}".encode()).hexdigest()
            # 直接传 documents，ChromaDB 内置模型自动生成 embedding
            await asyncio.to_thread(
                self._episodic.add,
                ids=[doc_id],
                documents=[summary],
                metadatas=[{"user_id": user_id, "conv_id": conv_id,
                            "ts": datetime.now().isoformat(), "full_text": self._safe_text(text[:500])}],
            )
        except Exception as ex:
            logger.warning(f"存储情景记忆失败: {ex}")

    async def _get_profile(self, user_id: str) -> Dict[str, Any]:
        """获取用户画像（取最新一条）。"""
        try:
            results = await asyncio.to_thread(self._profile.get, where={"user_id": user_id}, limit=1)
            if results["documents"]:
                return json.loads(results["documents"][0])
        except Exception:
            pass
        return {}

    async def close(self) -> None:
        """关闭异步 Redis 连接前，先等还在跑的后台压缩/画像任务收尾，
        避免它们在连接关掉之后才执行、访问一个已经关闭的 Redis 客户端。"""
        pending = [t for t in self._background_tasks if not t.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await self._redis.aclose()

    @staticmethod
    def _wm_key(user_id: str, conv_id: str) -> str:
        return f"wm:{user_id}:{conv_id}"

    @staticmethod
    def _summary_key(user_id: str, conv_id: str) -> str:
        return f"summary:{user_id}:{conv_id}"

    @staticmethod
    def _safe_text(value: Any) -> str:
        """转成 ChromaDB 可接受的普通 UTF-8 字符串。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    @classmethod
    def _safe_metadata_value(cls, value: Any) -> Any:
        """递归清洗 metadata，避免 Redis/ChromaDB 后续读写遇到非法 UTF-8。"""
        if isinstance(value, str):
            return cls._safe_text(value)
        if isinstance(value, dict):
            return {cls._safe_text(k): cls._safe_metadata_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._safe_metadata_value(v) for v in value]
        return value
