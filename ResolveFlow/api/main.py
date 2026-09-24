"""
ResolveFlow 智能客服系统 — FastAPI 入口

启动时打印小熊饼干图案。
所有核心组件在 lifespan 中初始化，通过环境变量配置。
"""
import asyncio
import json
import logging
import os
import pathlib
import sys
import uuid
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional


_ROOT = str(pathlib.Path(__file__).parent.parent.resolve())
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Response, UploadFile, File, Header
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BANNER = r"""
    ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ
   ╔══════════════════════╗
   ║   ResolveFlow  v2.0     ║
   ║   智能客服 AI 系统    ║
   ╚══════════════════════╝
    ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ
"""

# ── 全局组件（lifespan 中初始化）─────────────────────────────────────────────
_orchestrator = None
_memory       = None
_tool_manager = None
_monitor      = None
_evaluator    = None
_skill_manager = None

def _llm_cfg() -> Dict[str, Any]:
    """统一 LLM 配置：主 Agent/Orchestrator/RAG 链路和 Action GoalInterpreter
    现在读同一组 LLM_* 变量，只需要配置一次（见 .env.example）。"""
    key = os.getenv("LLM_API_KEY", "")
    if not key:
        raise RuntimeError("未设置 LLM_API_KEY")
    cfg: Dict[str, Any] = {
        "api_key":  key,
        "model":    os.getenv("LLM_MODEL", "claude-3-5-sonnet-20241022").strip(),
        # anthropic（默认）= Claude 官方 / DeepSeek 等 Anthropic 协议兼容 API
        # openai         = Qwen(DashScope) 等 OpenAI Chat Completions 兼容 API
        "provider": os.getenv("LLM_PROVIDER", "anthropic").strip().lower(),
    }
    base_url = os.getenv("LLM_BASE_URL", "").strip()
    if base_url:
        cfg["base_url"] = base_url
    return cfg


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _orchestrator, _memory, _tool_manager, _monitor, _evaluator, _skill_manager

    print(BANNER, flush=True)

    from agents.agent_orchestrator import AgentOrchestrator, Request
    from core.intent_recognizer import IntentRecognizer
    from evaluation.evaluator import EndToEndEvaluator
    from mcp.knowledge_base import KnowledgeBase
    from mcp.qwen_reranker import Qwen3Reranker
    from mcp.tool_manager import MCPToolManager, Tool
    from memory.conversation_memory import MemoryManager
    from monitor.performance_monitor import PerformanceMonitor
    from core.skill_loader import SkillManager

    cfg = _llm_cfg()
    logger.info(f"模型: {cfg['model']}  provider: {cfg['provider']}  base_url: {cfg.get('base_url', '(官方)')}")

    # 意图识别器（Orchestrator 内部也会创建，这里单独暴露给 Evaluator）
    recognizer = IntentRecognizer(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        provider=cfg["provider"],
    )

    # Skills：启动时从目录加载业务能力说明，并在 Agent 调用 LLM 时动态注入。
    skills_dir = os.getenv("RESOLVEFLOW_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills"))
    _skill_manager = SkillManager(
        root_dir=skills_dir,
        max_prompt_chars=int(os.getenv("RESOLVEFLOW_SKILLS_MAX_PROMPT_CHARS", "5000")),
    )
    _skill_manager.load()

    # 记忆管理器（Redis 工作记忆 + ChromaDB 情景记忆/用户画像）
    _memory = MemoryManager(
        redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
        chroma_host=os.getenv("CHROMA_HOST", "chromadb"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/app/data/chroma"),
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        provider=cfg["provider"],
    )

    # MCP 工具管理器 + RAG 知识库（基于 ChromaDB 的真实检索）
    qwen_reranker = Qwen3Reranker.from_env(fallback_api_key=cfg["api_key"])
    if qwen_reranker is None:
        logger.warning(
            "qwen3-rerank 配置不完整：需设置 QWEN_RERANK_BASE_URL，或设置 "
            "DASHSCOPE_WORKSPACE_ID（API Key 可复用当前 Qwen Key）；检索将降级到 RRF"
        )
    else:
        logger.info("Reranker: %s endpoint=%s", qwen_reranker.model, qwen_reranker.endpoint)

    _tool_manager = MCPToolManager(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        provider=cfg["provider"],
        reranker=qwen_reranker,
    )
    kb = KnowledgeBase(
        chroma_host=os.getenv("CHROMA_HOST", "chromadb"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/app/data/chroma"),
    )
    demo_documents = []
    for policy_path in sorted((pathlib.Path(_ROOT) / "data" / "knowledge").glob("*_policy_v1.json")):
        try:
            demo_documents.extend(json.loads(policy_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as ex:
            logger.warning("无法加载演示知识政策 %s: %s", policy_path.name, ex)
    if demo_documents:
        await kb.add_documents_async(demo_documents)
    subscription_docs = json.loads((pathlib.Path(_ROOT) / "data/knowledge/subscription_service_v1.json").read_text())
    # Retire subscription-only rules from the active knowledge index; source files
    # remain available for historical/offline regression, never current answers.
    await asyncio.to_thread(kb.delete_documents, [d["id"] for d in subscription_docs])
    logger.info(f"知识库已加载: {await kb.doc_count_async()} 个文档片段")

    def knowledge_fallback(params: Dict[str, Any], context: Optional[Dict[str, Any]], error: str):
        query = params.get("query", "")
        return [{
            "title": "知识库降级结果",
            "content": f"知识库暂时不可用，未能完成对“{query}”的混合检索。请稍后重试，或转人工客服确认。",
            "score": 0.0,
            "fallback": True,
            "error": error,
        }]

    _tool_manager.register(Tool(
        name="knowledge_search",
        description="搜索知识库（ChromaDB 向量 + BM25 双路召回与 RRF 融合）",
        handler=kb.search_handler,
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
                "allowed_document_ids": {"type": "array"},
            },
            "required": ["query"],
        },
        cache_ttl=300.0,
        supports_rerank=True,
        fallback=knowledge_fallback,
    ))

    # Agent 编排器（在 tool_manager 就绪之后创建，让老系统闲聊/通用链路里的
    # Agent 也能拿到共享知识库检索工具——见 agents/chat_tools.py 的说明）
    _orchestrator = AgentOrchestrator(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        skill_manager=_skill_manager,
        provider=cfg["provider"],
        supervisor_max_subtasks=int(os.getenv("SUPERVISOR_MAX_SUBTASKS", "3")),
        subtask_timeout_s=float(os.getenv("SUPERVISOR_SUBTASK_TIMEOUT_SECONDS", "30")),
        tool_manager=_tool_manager,
    )

    # 性能监控（可选启动 Prometheus）
    prom_port = int(os.getenv("PROMETHEUS_PORT", "0")) or None
    _monitor = PerformanceMonitor(
        orchestrator=_orchestrator,
        tool_manager=_tool_manager,
        interval_s=float(os.getenv("MONITOR_INTERVAL", "10")),
        webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
        prometheus_port=prom_port,
    )
    await _monitor.start()

    # 评测器
    _evaluator = EndToEndEvaluator(
        orchestrator=_orchestrator,
        recognizer=recognizer,
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        baseline_path=os.getenv("EVAL_BASELINE_PATH", str(pathlib.Path(_ROOT) / "data/eval/baseline.json")),
        reports_dir=os.getenv("EVAL_REPORTS_DIR", str(pathlib.Path(_ROOT) / "data/eval/reports")),
        provider=cfg["provider"],
        knowledge_resolver=_build_knowledge_context,
    )

    _configure_conversation_service()
    logger.info("ResolveFlow 已就绪")
    yield

    await _monitor.stop()
    if _tool_manager is not None:
        await _tool_manager.aclose()
    if _memory is not None:
        await _memory.close()
    logger.info("ResolveFlow 已关闭")


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="ResolveFlow 智能客服",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
)

from api.action_routes import router as action_router
app.include_router(action_router)
from api.commerce_routes import router as commerce_router
app.include_router(commerce_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 请求/响应模型 ─────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message:     str
    user_id:     str = "anonymous"
    conv_id:     Optional[str] = None
    task_id: Optional[str] = None
    order_id: Optional[str] = None


class ChatResponse(BaseModel):
    conv_id:     str
    commerce_case: Optional[Dict[str, Any]] = None
    candidates: List[Dict[str, Any]] = Field(default_factory=list)
    action_task: Optional[Dict[str, Any]] = None
    response:    str
    intent:      str
    intent_group: str = "other"
    agent_type:  str
    agent_types: List[str] = Field(default_factory=list)
    primary_agent: str = ""
    supporting_agents: List[str] = Field(default_factory=list)
    routing_reason: str = ""
    routing_confidence: float = 0.0
    escalated:   bool
    latency_ms:  float
    knowledge_used: bool = False
    entities: Dict[str, List[str]] = Field(default_factory=dict)
    intent_confidence: float = 0.0
    intent_source_scores: Dict[str, float] = Field(default_factory=dict)
    task_plan: Optional[Dict[str, Any]] = None
    execution_trace: List[Dict[str, Any]] = Field(default_factory=list)
    synthesis_method: str = "single_agent"
    degradations: List[str] = Field(default_factory=list)
    memory_mode: Optional[str] = None
    sources: List[Dict[str, Any]] = Field(default_factory=list)


def _configure_conversation_service():
    from api.action_routes import conversation_service
    service = conversation_service()
    if _memory is not None:
        service.memory = _memory
    if _orchestrator is not None:
        service.recognizer = _orchestrator._intent_recognizer
        service.answer_orchestrator = _orchestrator
    if _tool_manager is not None:
        async def search(query, allowed_document_ids=None):
            extra = {"allowed_document_ids": sorted(allowed_document_ids)} if allowed_document_ids else None
            result = await _tool_manager.search_with_rewrite("knowledge_search", query, top_k=10, extra_params=extra)
            return result.data if result.success and isinstance(result.data, list) else []
        service.knowledge_search = search
        tool = _tool_manager._tools.get("knowledge_search")
        if tool and hasattr(tool.handler, "__self__"):
            service.knowledge_document_ids = tool.handler.__self__.document_ids
    return service


def _require_any_role(authorization: str = Header(default="")) -> str:
    """Any authenticated subject (user/reviewer/admin) — for read-only endpoints
    that don't need per-owner scoping but should no longer be fully open."""
    from core.auth import AuthError, subject_with_role
    try:
        return subject_with_role(authorization, "user", "reviewer", "admin")
    except AuthError as ex:
        raise HTTPException(401, str(ex)) from ex


def _require_admin(authorization: str = Header(default="")) -> str:
    """Writes/ops that affect every user (knowledge base mutation, skill
    reload, monitor internals, evaluation runs) require the admin role."""
    from core.auth import AuthError, subject_with_role
    try:
        return subject_with_role(authorization, "admin")
    except AuthError as ex:
        raise HTTPException(403, str(ex)) from ex


# ── 路由 ──────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    # Deliberately open: the Docker/Compose healthcheck and Prometheus both
    # hit this without a token, and it leaks no per-user or business data.
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    return {"status": "ok", "agents": _orchestrator.get_stats()}


@app.get("/skills", tags=["Skills"])
async def skills_summary(user=Depends(_require_any_role)):
    """查看当前已加载的 Skills，便于确认热加载结果和排查解析错误。"""
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    return _skill_manager.summary()


@app.post("/skills/reload", tags=["Skills"])
async def reload_skills(user=Depends(_require_admin)):
    """运行时重新扫描 Skill 目录，不需要重启服务。"""
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    _skill_manager.reload()
    if _orchestrator is not None:
        _orchestrator.set_skill_manager(_skill_manager)
    return _skill_manager.summary()


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, authorization: str = Header(default="")):
    """
    统一对话接口：身份解析 → 目标理解（咨询/办理分流）→ 执行 → 记忆写入。

    这是唯一的对话入口——不再有单独的"仅问答"或"仅办理"模式；纯知识问答和
    受权限约束的业务办理共用同一条鉴权、同一个会话状态。旧的 mode=chat/action
    直接调用编排器、绕过身份校验的入口已下线，见 wiki/unified-conversation.md。
    """
    from api.action_routes import owner
    user = owner(authorization)
    service = _configure_conversation_service()
    started = time.monotonic()
    try:
        result = await service.send(user, req.message, conversation_id=req.conv_id, task_id=req.task_id, order_id=req.order_id)
    except ValueError as ex:
        raise HTTPException(400, str(ex)) from ex
    task = result["task"]
    return ChatResponse(conv_id=result["conversation_id"], response=result["response"], intent=result["intent"],
                        agent_type=task["primary_agent"] if task else "general",
                        escalated=result["status"] in {"needs_human", "awaiting_approval"},
                        latency_ms=(time.monotonic() - started) * 1000, action_task=task, commerce_case=result.get("commerce_case"), candidates=result.get("candidates", []),
                        knowledge_used=result["knowledge_used"], intent_source_scores=result["intent_source_scores"],
                        memory_mode=result["memory_mode"], sources=result["sources"],
                        degradations=result["degradations"], routing_reason=result["route"], synthesis_method="unified_conversation")


def _truncate_content(content: str, limit: int) -> str:
    """
    截断知识库内容，优先在最近的句号处截断，避免吐出半句话。

    找不到句号（比如整段没有标点的极端文本）时才退化为硬切，
    并且始终在截断处补一个可见标记，让 LLM 和排障日志都能看出这段信息不完整。
    """
    if len(content) <= limit:
        return content
    cut = content.rfind("。", 0, limit)
    cut = cut + 1 if cut != -1 else limit
    return content[:cut].rstrip() + "…（内容过长，已截断）"


async def _build_knowledge_context(message: str, intent=None, top_k: int = 3) -> tuple[str, bool]:
    """
    为 /chat 主链路构建 RAG 知识上下文。

    这里复用 MCPToolManager 的查询改写、并行召回、重排、fallback 能力。
    """
    if _tool_manager is None:
        return "", False
    if not _should_use_knowledge(message, intent=intent):
        return "", False

    from mcp.knowledge_base import KnowledgeBase

    # 略高于单片目标长度（KnowledgeBase.CHUNK_SIZE），只用来兜底极端场景
    # （比如整段没有句号导致切片本身就超长），正常切片不会触发这个上限。
    content_limit = KnowledgeBase.CHUNK_SIZE + 100

    try:
        result = await _tool_manager.search_with_rewrite("knowledge_search", message, top_k=top_k)
        if not result.success or not isinstance(result.data, list) or not result.data:
            return "", False

        parts = ["[知识库检索结果]"]
        used = False
        for i, item in enumerate(result.data[:top_k], start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "未命名文档"))
            content = str(item.get("content", "")).strip()
            score = item.get("score", "")
            if not content:
                continue
            used = True
            # 长文档会被切成多片，多片同时命中时标注段落位置，
            # 让 LLM 知道这是长文档里的一个片段，而不是文档全貌。
            total_chunks = item.get("total_chunks", 1)
            position = ""
            if isinstance(total_chunks, int) and total_chunks > 1:
                chunk_index = item.get("chunk", 0)
                position = f"（第 {chunk_index + 1}/{total_chunks} 段）"
            truncated = _truncate_content(content, content_limit)
            if truncated != content:
                logger.info(f"知识库片段过长已截断: title={title!r} 原长度={len(content)}")
            parts.append(f"{i}. 标题: {title}{position}\n   相关度: {score}\n   内容: {truncated}")

        if not used:
            return "", False
        parts.append("请优先依据以上知识库内容回答；如果知识库内容不足，再结合通用客服能力说明。")
        return "\n".join(parts), True
    except Exception as ex:
        logger.warning(f"构建知识库上下文失败: {ex}")
        return "", False


def _should_use_knowledge(message: str, intent=None) -> bool:
    """跳过纯寒暄，业务类问题才检索知识库，避免无关 RAG 干扰回复。"""
    msg = (message or "").strip().lower()
    if not msg:
        return False
    intent_value = getattr(intent, "value", intent)
    if intent_value in {"greeting", "feedback", "other"}:
        return False
    if intent_value in {
        "query", "request", "technical", "billing", "account", "complaint",
        "order_status", "logistics", "refund", "invoice", "payment_issue",
        "account_security", "technical_login", "technical_crash",
    }:
        return True
    greetings = {"你好", "您好", "嗨", "hi", "hello", "hey", "早上好", "晚上好"}
    if msg in greetings:
        return False
    business_keywords = [
        "退款", "订单", "物流", "配送", "发票", "扣款", "支付", "账单", "订阅",
        "登录", "报错", "错误", "崩溃", "会员", "积分", "账户", "密码", "地址",
        "refund", "order", "invoice", "payment", "error", "login",
    ]
    return len(msg) >= 4 or any(kw in msg for kw in business_keywords)


@app.get("/monitor")
async def monitor_summary(user=Depends(_require_admin)):
    """实时监控摘要：Agent 成功率、工具统计、告警、优化建议。内部运维数据，要求 admin。"""
    if _monitor is None:
        raise HTTPException(503, "服务未就绪")
    return _monitor.summary()


class ToolTraceResponse(BaseModel):
    found: bool
    trace: Dict[str, Any] = Field(default_factory=dict)


class RecentToolTracesResponse(BaseModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)


@app.get("/trace/tool/{request_id}", response_model=ToolTraceResponse)
async def get_tool_trace(request_id: str, user=Depends(_require_admin)):
    """按 request_id 回放一次 /chat 请求里实际发生过的工具调用：调用了哪些工具、
    是否成功、耗时、是否命中缓存/重排。仅保留最近 RESOLVEFLOW_TOOL_TRACE_MAX 条
    （默认 200），进程重启即丢失——用于排查和演示，不是审计凭证。内部运维数据，要求 admin。"""
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    trace = _orchestrator.get_tool_trace(request_id)
    return ToolTraceResponse(found=trace is not None, trace=trace or {})


@app.get("/trace/tools", response_model=RecentToolTracesResponse)
async def list_recent_tool_traces(limit: int = 20, user=Depends(_require_admin)):
    """列出最近若干条请求的工具调用轨迹，按时间倒序。内部运维数据，要求 admin。"""
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    return RecentToolTracesResponse(items=_orchestrator.get_recent_tool_traces(limit=limit))


@app.get("/metrics")
async def prometheus_metrics():
    # 保持开放：Prometheus 抓取本身不方便每次带一个会过期的 JWT，
    # 且这里只是标准 Prometheus 文本格式的低层指标，不含业务/用户数据。
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/search")
async def search(query: str, top_k: int = 5, user=Depends(_require_any_role)):
    """
    演示检索优化链路：查询改写 → 并行召回 → 重排 → Top-K。
    展示 MCP 工具调用的核心亮点。
    """
    if _tool_manager is None:
        raise HTTPException(503, "服务未就绪")
    result = await _tool_manager.search_with_rewrite("knowledge_search", query, top_k=top_k)
    return {
        "query": query,
        "results": result.data,
        "reranked": result.reranked,
        "degradations": result.degradations,
    }


class DocInput(BaseModel):
    """单篇文档输入。"""
    title:   str
    content: str
    id: Optional[str] = None
    domain: str = "general"
    version: str = "v1"
    effective_at: str = "2000-01-01"
    source: str = "admin-upload"
    topic: str = ""


class BatchDocInput(BaseModel):
    """批量文档导入请求体。"""
    documents: List[DocInput]


class EvalIntentInput(BaseModel):
    """意图识别评测用例。"""
    message: str
    expected_intent: str
    context: Optional[Dict[str, Any]] = None


class EvalDialogInput(BaseModel):
    """对话质量评测用例。question 单轮，turns 多轮。"""
    question: Optional[str] = None
    turns: Optional[List[str]] = None
    user_id: Optional[str] = None
    conv_id: Optional[str] = None


class EvalRunInput(BaseModel):
    """评测请求。为空时使用内置默认用例。"""
    intent_cases: Optional[List[EvalIntentInput]] = None
    dialog_cases: Optional[List[EvalDialogInput]] = None
    dataset: Optional[str] = Field(
        default=None,
        description="内置金标数据集名称；支持 billing 或 multi_agent",
    )
    split: str = Field(default="all", description="数据切分：all、dev 或 holdout")
    promote_baseline: bool = Field(default=False, description="仅在最终门槛通过时提升正式基线")
    max_intents_per_label: int = Field(default=0, ge=0, description="按意图稳定抽样上限，仅用于在线冒烟")
    max_dialogs_per_agent: int = Field(default=0, ge=0, description="按主 Agent 稳定抽样上限，仅用于在线冒烟")


def _eval_budget_configured() -> bool:
    """只判断预算是否为正数，不读取或记录具体额度。"""
    try:
        return float(os.getenv("EVAL_MAX_USD", "0")) > 0
    except ValueError:
        return False


@app.post("/knowledge/add", tags=["知识库"])
async def add_knowledge(body: BatchDocInput, user=Depends(_require_admin)):
    """
    批量导入文档到知识库。

    文档会自动切片（每片 500 字）并存入 ChromaDB，ChromaDB 内置 Embedding 模型自动向量化。

    示例请求体：
    ```json
    {
      "documents": [
        {"title": "退款政策", "content": "用户在购买后 7 天内可以申请无理由退款..."},
        {"title": "配送说明", "content": "标准配送 3-5 个工作日..."}
      ]
    }
    ```
    """
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__
    try:
        count = await kb.add_documents_async([d.model_dump(exclude_none=True) for d in body.documents])
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    _tool_manager._cache.clear()
    total = await kb.doc_count_async()
    return {"message": f"成功导入 {count} 个文档片段", "added_chunks": count, "total_chunks": total}


@app.post("/knowledge/upload", tags=["知识库"])
async def upload_knowledge(file: UploadFile = File(...), user=Depends(_require_admin)):
    """
    上传文件导入知识库。

    支持格式：
    - `.txt` / `.md`：整个文件作为一篇文档，文件名作为标题
    - `.json`：JSON 数组格式 `[{"title": "...", "content": "..."}, ...]`

    文件大小限制：10MB
    """
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "文件大小超过 10MB 限制")

    text = content.decode("utf-8", errors="ignore")
    filename = file.filename or "unknown"

    if filename.endswith(".json"):
        import json as _json
        try:
            docs = _json.loads(text)
            if not isinstance(docs, list):
                raise HTTPException(400, "JSON 文件应为数组格式: [{title, content}, ...]")
        except _json.JSONDecodeError as e:
            raise HTTPException(400, f"JSON 解析失败: {e}")
    else:
        # txt / md：整个文件作为一篇文档
        title = filename.rsplit(".", 1)[0] if "." in filename else filename
        docs = [{"title": title, "content": text}]

    try:
        docs = [DocInput.model_validate(d).model_dump(exclude_none=True) for d in docs]
        count = await kb.add_documents_async(docs)
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    _tool_manager._cache.clear()
    total = await kb.doc_count_async()
    return {
        "message": f"文件 {filename} 导入成功",
        "added_chunks": count,
        "total_chunks": total,
    }


@app.get("/knowledge/stats", tags=["知识库"])
async def knowledge_stats(user=Depends(_require_any_role)):
    """查看知识库统计信息（文档片段总数）。"""
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__
    return {"total_chunks": await kb.doc_count_async()}


@app.post("/eval/run")
async def run_eval(body: Optional[EvalRunInput] = None, user=Depends(_require_admin)):
    """运行内置或已登记的金标评测用例，返回评测报告。"""
    if _evaluator is None:
        raise HTTPException(503, "服务未就绪")
    from evaluation.evaluator import DEFAULT_DIALOG_CASES, DEFAULT_INTENT_CASES, IntentTestCase

    if body and body.dataset:
        if body.dataset not in {"billing", "multi_agent"}:
            raise HTTPException(400, "未知 dataset；当前仅支持 billing 或 multi_agent")
        if body.intent_cases is not None or body.dialog_cases is not None:
            raise HTTPException(400, "dataset 不能与自定义 intent_cases 或 dialog_cases 同时使用")
        from evaluation.dataset_loader import load_billing_profile, load_multi_agent_profile
        from evaluation.dataset_loader import stratified_limit

        try:
            loader = load_billing_profile if body.dataset == "billing" else load_multi_agent_profile
            intent_records, dialog_cases = loader(pathlib.Path(_ROOT) / "data", split=body.split)
        except (OSError, ValueError) as ex:
            logger.error("加载金标数据失败: %s", ex)
            raise HTTPException(422, f"金标数据校验失败: {ex}") from ex
        if not _eval_budget_configured():
            limited_smoke = (
                body.split == "dev"
                and 0 < body.max_intents_per_label <= 2
                and 0 < body.max_dialogs_per_agent <= 2
            )
            if not limited_smoke:
                raise HTTPException(
                    409,
                    "未配置 EVAL_MAX_USD；仅允许 dev 的小样本冒烟评测，"
                    "请设置 max_intents_per_label/max_dialogs_per_agent（均不超过 2），或配置预算后再运行完整集。",
                )
        intent_records = stratified_limit(intent_records, "expected_intent", body.max_intents_per_label)
        dialog_cases = stratified_limit(dialog_cases, "expected_primary_agent", body.max_dialogs_per_agent)
        intent_cases = [
            IntentTestCase(
                message=record["text"],
                expected_intent=record["expected_intent"],
                test_id=record["id"],
                source=record["source"],
                synthetic=bool(record["synthetic"]),
                expected_group=record["expected_group"],
                expected_primary_agent=record["expected_primary_agent"],
            )
            for record in intent_records
        ]
    elif body and body.intent_cases is not None:
        intent_cases = [
            IntentTestCase(
                message=c.message,
                expected_intent=c.expected_intent,
                context=c.context,
            )
            for c in body.intent_cases
        ]
    else:
        intent_cases = DEFAULT_INTENT_CASES

    if body and body.dataset:
        pass
    elif body and body.dialog_cases is not None:
        dialog_cases = [
            c.model_dump(exclude_none=True)
            for c in body.dialog_cases
        ]
    else:
        dialog_cases = DEFAULT_DIALOG_CASES

    report = await _evaluator.run(
        intent_cases=intent_cases,
        dialog_cases=dialog_cases,
        dataset_name=body.dataset if body and body.dataset else "inline",
        promote_baseline=bool(body and body.promote_baseline),
    )
    return {
        "pass_rate":       report.pass_rate,
        "total":           report.total,
        "passed":          report.passed,
        "avg_scores":      report.avg_scores,
        "regressions":     report.regressions,
        "recommendations": report.recommendations,
        "failure_groups":  report.failure_groups,
        "report_path": report.report_path,
        "baseline_promoted": report.baseline_promoted,
        "results": [
            {
                "test_id": r.test_id,
                "passed": r.passed,
                "scores": r.scores,
                "detail": r.detail,
                "metadata": r.metadata,
            }
            for r in report.results
        ],
    }


# ── 交互式 CLI ────────────────────────────────────────────────────────────────
async def _cli():
    print(BANNER)
    print("ResolveFlow CLI — 输入 quit 退出\n")

    from agents.agent_orchestrator import AgentOrchestrator, Request
    from memory.conversation_memory import MemoryManager, MsgRole
    from core.skill_loader import SkillManager

    cfg = _llm_cfg()
    skill_manager = SkillManager(
        root_dir=os.getenv("RESOLVEFLOW_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills")),
        max_prompt_chars=int(os.getenv("RESOLVEFLOW_SKILLS_MAX_PROMPT_CHARS", "5000")),
    )
    skill_manager.load()
    orch = AgentOrchestrator(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        skill_manager=skill_manager,
        provider=cfg["provider"],
    )
    mem  = MemoryManager(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        chroma_host=os.getenv("CHROMA_HOST", "localhost"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/tmp/chroma"),
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        provider=cfg["provider"],
    )

    user_id, conv_id = "cli_user", str(uuid.uuid4())

    while True:
        try:
            msg = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见 ʕ•ᴥ•ʔ")
            break
        if not msg or msg.lower() in ("quit", "exit", "退出"):
            print("再见 ʕ•ᴥ•ʔ")
            break

        ctx = await mem.get_context(user_id, conv_id, query=msg)
        history = [
            {"role": m.role.value, "content": m.content}
            for m in ctx.recent_messages[-5:]
        ] if ctx.recent_messages else None
        req = Request(message=msg, user_id=user_id, conv_id=conv_id, context=ctx.to_prompt_text(), history=history)
        result = await orch.run(req)

        await mem.add_message(user_id, conv_id, MsgRole.USER, msg)
        await mem.add_message(user_id, conv_id, MsgRole.ASSISTANT, result.response)

        print(f"\nResolveFlow [{result.agent_type.value}]: {result.response}\n")

    await mem.close()


if __name__ == "__main__":
    if "--cli" in sys.argv:
        asyncio.run(_cli())
    else:
        uvicorn.run(
            "api.main:app",
            host=os.getenv("API_HOST", "0.0.0.0"),
            port=int(os.getenv("API_PORT", "8000")),
            reload=os.getenv("APP_ENV") == "development",
        )
