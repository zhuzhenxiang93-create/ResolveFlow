"""
亮点：多 Agent 路由与编排

核心路径：多意图结构化识别 -> Agent 映射与轻量 TaskPlan 构建
-> DAG 调度 -> 领域 Agent 独立执行 -> 结果合并。
路由阶段不再重复用关键词扫描语义意图；业务写操作另走受控执行链路。
同类多个 Agent 实例仍根据运行表现选择，失败时可降级到 General。
"""
import asyncio
import inspect
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, List, Optional

from core.intent_recognizer import IntentCategory, IntentRecognizer, RecognizedIntent, UrgencyLevel
from core.llm_client import LLMClient
from agents.chat_tools import build_tool_registry
from agents.supervisor import (
    DAGScheduler,
    ResponseSynthesizer,
    SubTask,
    SubTaskResult,
    TaskPlan,
    TaskStatus,
)

logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────────────────────────────────

class AgentType(Enum):
    GENERAL   = "general"    # 通用客服
    TECHNICAL = "technical"  # 技术支持
    BILLING   = "billing"    # 账单/退款
    ESCALATION = "escalation" # 人工升级（占位）


@dataclass
class AgentStats:
    """Agent 运行时统计，供 Monitor 和路由决策使用。"""
    total:     int   = 0
    success:   int   = 0
    total_ms:  float = 0.0
    monitor_penalty: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 1.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.total if self.total else 0.0

    def routing_score(self) -> float:
        """路由评分：成功率高、延迟低的 Agent 得分高。"""
        latency_score = 1.0 / (1.0 + self.avg_ms / 1000)
        base_score = self.success_rate * 0.7 + latency_score * 0.3
        return base_score * max(0.0, 1.0 - self.monitor_penalty)


@dataclass
class AgentResponse:
    agent_type:  AgentType
    content:     str
    success:     bool
    confidence:  float = 1.0
    latency_ms:  float = 0.0
    escalate:    bool  = False   # 是否需要升级
    tools_used:  List[str] = field(default_factory=list)
    tool_traces: List[Dict[str, Any]] = field(default_factory=list)
    error_diagnostic: Optional[Dict[str, Any]] = None  # 仅 success=False 时填充，见 handle() 的 except 分支


@dataclass
class Request:
    message:     str
    user_id:     str
    conv_id:     str
    context:     str = ""        # 来自 MemoryManager 的格式化上下文
    history:     Optional[List[Dict[str, str]]] = None  # 对话历史，传给意图识别
    entities:    Dict[str, List[str]] = field(default_factory=dict)
    intent:      Optional[IntentCategory] = None
    intent_group: Optional[str] = None
    urgency:     Optional[UrgencyLevel]   = None
    intent_confidence: float = 1.0
    intents: List[RecognizedIntent] = field(default_factory=list)
    subtask_id: Optional[str] = None
    task_instruction: str = ""
    dependency_context: str = ""
    request_id:  str = field(default_factory=lambda: str(uuid.uuid4())[:8])


@dataclass
class OrchestratorResult:
    request_id:  str
    response:    str
    agent_type:  AgentType
    intent:      Optional[IntentCategory]
    escalated:   bool  = False
    latency_ms:  float = 0.0
    agent_types: List[AgentType] = field(default_factory=list)
    primary_agent: Optional[AgentType] = None
    supporting_agents: List[AgentType] = field(default_factory=list)
    routing_reason: str = ""
    routing_confidence: float = 0.0
    task_plan: Optional[Dict[str, Any]] = None
    execution_trace: List[Dict[str, Any]] = field(default_factory=list)
    synthesis_method: str = "single_agent"
    degradations: List[str] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)
    tool_traces: List[Dict[str, Any]] = field(default_factory=list)
    error_diagnostics: List[Dict[str, Any]] = field(default_factory=list)  # 见 AgentResponse.error_diagnostic


@dataclass
class RoutingDecision:
    """一次请求的结构化路由决策。"""
    primary_agent: AgentType
    supporting_agents: List[AgentType] = field(default_factory=list)
    reason: str = ""
    confidence: float = 0.0

    @property
    def agent_types(self) -> List[AgentType]:
        return [self.primary_agent] + self.supporting_agents

    @property
    def multi_agent(self) -> bool:
        return bool(self.supporting_agents)


# ── 基础 Agent ────────────────────────────────────────────────────────────────

class BaseAgent:
    """所有 Agent 的基类，封装 LLM 调用和统计。"""

    agent_type: AgentType
    system_prompt: str

    def __init__(self, client: LLMClient, model: str, skill_manager: Optional[Any] = None, tool_manager: Optional[Any] = None):
        self._client = client
        self._model  = model
        self._skill_manager = skill_manager
        # 只有显式传入 tool_manager 时才装配工具（含共享知识库检索工具），
        # 这是"该 Agent 是否具备工具调用能力"的唯一开关：未传入时保持旧行为
        # （单轮直接生成回复），避免离线/测试场景下的假 LLM client 因为不认识
        # create_tool_turn() 而报错，同时也不给没有真实工具基础设施的调用方
        # 一堆看起来能调用、实际什么都做不了的工具。
        self._tools = build_tool_registry(self.agent_type.value, tool_manager) if tool_manager is not None else {}
        self.stats   = AgentStats()

    async def handle(self, req: Request) -> AgentResponse:
        t0 = time.monotonic()
        self.stats.total += 1
        try:
            content, tools_used, tool_traces = await self._call_llm(req)
            content, safety_escalate = self._apply_safety_guard(content)
            ms = (time.monotonic() - t0) * 1000
            self.stats.success += 1
            self.stats.total_ms += ms
            escalate = safety_escalate or self._needs_escalation(content)
            return AgentResponse(
                agent_type=self.agent_type,
                content=content,
                success=True,
                latency_ms=ms,
                escalate=escalate,
                tools_used=tools_used,
                tool_traces=tool_traces,
            )
        except Exception as ex:
            ms = (time.monotonic() - t0) * 1000
            self.stats.total_ms += ms
            logger.error(f"{self.agent_type.value} 处理失败: {ex}")
            # Sanitized failure diagnostic, same discipline as GoalInterpreter.interpret's
            # own diagnostics (agents/goal_interpreter.py): a coarse category bucket + the
            # exception's class name + an HTTP status if the client attached one. Never the
            # raw exception message/args — those can carry provider error bodies, request
            # URLs, or other details we don't want round-tripping into a user-facing API
            # response. The real `ex` detail stays server-side in the logger.error above.
            category = "timeout" if isinstance(ex, asyncio.TimeoutError) or "Timeout" in type(ex).__name__ else "agent_call_failed"
            diagnostic = {"category": category, "error_type": type(ex).__name__}
            status = getattr(ex, "status_code", None)
            if isinstance(status, int):
                diagnostic["http_status"] = status
            return AgentResponse(
                agent_type=self.agent_type,
                content="抱歉，处理您的请求时出现问题，请稍后重试。",
                success=False,
                latency_ms=ms,
                error_diagnostic=diagnostic,
            )

    async def _call_llm(self, req: Request):
        """返回 (最终文本回复, 本轮用过的工具名列表, 工具调用明细列表)。"""
        def _clean(s: str) -> str:
            return s.encode("utf-8", errors="ignore").decode("utf-8")

        messages: List[Dict[str, Any]] = []
        if req.context:
            messages.append({"role": "user", "content": f"[背景信息]\n{_clean(req.context)}"})
            messages.append({"role": "assistant", "content": "好的，我已了解背景信息。"})
        if req.entities:
            entities_text = json.dumps(req.entities, ensure_ascii=False)
            messages.append({"role": "user", "content": f"[结构化实体]\n{_clean(entities_text)}"})
            messages.append({"role": "assistant", "content": "好的，我会结合这些结构化实体处理。"})
        if req.task_instruction:
            scoped = (
                "[原始请求，仅作背景]\n"
                f"{_clean(req.message)}\n\n"
                "[当前子任务]\n"
                f"{_clean(req.task_instruction)}\n\n"
                "只完成当前子任务，不要替其他 Agent 回答其负责的部分。"
            )
            if req.dependency_context:
                scoped += f"\n\n[已完成的依赖结果]\n{_clean(req.dependency_context)}"
            messages.append({"role": "user", "content": scoped})
        else:
            messages.append({"role": "user", "content": _clean(req.message)})

        if not self._tools:
            # 没有可用工具（未注入 tool_manager，或该 Agent 类型未配置工具）时，
            # 保持原来的单轮调用路径，不为空工具表多付一次协议转换开销。
            content = await self._client.create(
                max_tokens=1024,
                system=self._build_system_prompt(req),
                messages=messages,
            )
            return content, [], []

        return await self._run_tool_loop(req, messages)

    async def _run_tool_loop(self, req: Request, messages: List[Dict[str, Any]]):
        """最多 3 轮的工具调用循环：模型可以在给出最终回复前，先调用本 Agent
        工具白名单里的只读工具（知识库检索、错误码查询、账单字段核验等）。
        每一轮的调用明细都记录进 tool_traces，供 /trace/tool/{request_id} 回放。"""
        tool_schemas = [
            {"name": spec.name, "description": spec.description, "parameters": spec.parameters}
            for spec in self._tools.values()
        ]
        system = self._build_system_prompt(req)
        tools_used: List[str] = []
        tool_traces: List[Dict[str, Any]] = []
        for _ in range(3):
            turn = await self._client.create_tool_turn(
                system=system, messages=messages, tools=tool_schemas, max_tokens=1024,
            )
            calls = turn.get("tool_calls") or []
            if not calls:
                return turn.get("content") or "", tools_used, tool_traces

            messages.append({"role": "assistant", "content": turn.get("content") or "", "tool_calls": calls})
            for call in calls:
                fn = call.get("function", {}) if isinstance(call, dict) else {}
                name = fn.get("name", "")
                call_id = call.get("id")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except (TypeError, ValueError):
                    args = {}
                spec = self._tools.get(name)
                tool_t0 = time.monotonic()
                error_text = ""
                if spec is None or spec.handler is None:
                    success = False
                    result: Dict[str, Any] = {"success": False, "error": f"工具不在 {self.agent_type.value} Agent 白名单中"}
                    error_text = result["error"]
                else:
                    try:
                        outcome = spec.handler(req, args)
                        if inspect.isawaitable(outcome):
                            outcome = await outcome
                        result = outcome if isinstance(outcome, dict) else {"success": True, "result": outcome}
                        success = bool(result.get("success", True))
                        tools_used.append(name)
                    except Exception as ex:
                        success = False
                        error_text = str(ex)
                        result = {"success": False, "error": error_text}
                        logger.warning("Agent 工具 %s 执行失败: %s", name, ex)
                tool_latency_ms = (time.monotonic() - tool_t0) * 1000
                tool_traces.append({
                    "agent_type": self.agent_type.value,
                    "tool_name": name,
                    "input": dict(args),
                    "success": success,
                    "latency_ms": round(tool_latency_ms, 1),
                    "sources": result.get("sources", []),
                    "degradations": result.get("degradations", []),
                    "cached": bool(result.get("cached")),
                    "reranked": bool(result.get("reranked")),
                    "error": error_text or str(result.get("error") or ""),
                })
                messages.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(result, ensure_ascii=False)})

        logger.warning("%s 工具调用超过最大轮数，返回兜底回复", self.agent_type.value)
        return "抱歉，这个问题需要更多信息核实，建议转接人工客服协助处理。", tools_used, tool_traces

    def _build_system_prompt(self, req: Request) -> str:
        """把动态加载的 Skills 拼入 system prompt，让业务规则随请求生效。"""
        if self._skill_manager is None:
            return self.system_prompt
        skill_prompt = self._skill_manager.prompt_for(req.message, self.agent_type.value)
        if not skill_prompt:
            return self.system_prompt
        return f"{self.system_prompt}\n\n[动态 Skills]\n{skill_prompt}"

    def _needs_escalation(self, content: str) -> bool:
        """检测 Agent 是否建议升级（简单关键词检测）。"""
        keywords = ["转人工", "人工客服", "escalate", "specialist", "无法处理"]
        return any(kw in content for kw in keywords)

    @staticmethod
    def _apply_safety_guard(content: str) -> tuple[str, bool]:
        """阻断敏感凭据索取和确定性资金承诺，优先保证回复安全。"""
        unsafe_phrases = (
            "保证退款", "一定退款", "立刻到账", "立即到账",
            "提供密码", "告诉我密码", "发送验证码给我",
        )
        if not any(phrase in content for phrase in unsafe_phrases):
            return content, False
        return (
            "为保证账户与资金安全，我不能承诺退款结果、到账时间，"
            "也不会索要密码或验证码。该问题需要按演示政策核实，必要时转交人工处理。",
            True,
        )


class GeneralAgent(BaseAgent):
    agent_type    = AgentType.GENERAL
    system_prompt = (
        "你是 ResolveFlow 智能客服。友好、简洁地回答用户问题。"
        "如果问题超出你的能力范围，明确说明并建议转接专业客服。"
    )


class TechnicalAgent(BaseAgent):
    agent_type    = AgentType.TECHNICAL
    system_prompt = (
        "你是技术支持专家。专注于：故障排查、错误诊断、系统配置。"
        "提供清晰的步骤化解决方案。遇到需要后台操作的问题，说明需要升级处理。"
    )


class BillingAgent(BaseAgent):
    agent_type    = AgentType.BILLING
    system_prompt = (
        "你是账单服务专家。专注于：账单查询、退款申请、发票问题、订阅管理。"
        "对财务问题保持准确和专业。涉及实际退款操作时，说明需要人工审核。"
        "只能依据已提供的知识库内容；不得编造订单号、工单号、金额、处理时效、联系方式或业务政策。"
        "不得保证退款或到账，不得索要密码、验证码或完整敏感凭据。"
    )


class EscalationAgent(BaseAgent):
    agent_type = AgentType.ESCALATION
    system_prompt = (
        "你是人工升级协调专员。确认已记录用户问题并说明将转交人工或专员处理。"
        "保留问题摘要、当前风险和已尝试步骤；不得索要密码、验证码或其他敏感凭据。"
        "不得编造工单号、联系时效、联系方式、处理结果或任何真实业务承诺。"
    )


# ── 编排器 ────────────────────────────────────────────────────────────────────

class AgentOrchestrator:
    """
    多 Agent 编排器。

    路由逻辑（三层）：
      1. 意图 → Agent 类型映射
      2. 同类多实例时按 routing_score() 选最优
      3. 专属 Agent 失败时降级到 GeneralAgent
    """

    # 意图 → Agent 类型的静态映射（路由表）
    _INTENT_ROUTING: Dict[IntentCategory, AgentType] = {
        IntentCategory.TECHNICAL:  AgentType.TECHNICAL,
        IntentCategory.TECHNICAL_LOGIN: AgentType.TECHNICAL,
        IntentCategory.TECHNICAL_CRASH: AgentType.TECHNICAL,
        IntentCategory.BILLING:    AgentType.BILLING,
        IntentCategory.REFUND:     AgentType.BILLING,
        IntentCategory.INVOICE:    AgentType.BILLING,
        IntentCategory.PAYMENT_ISSUE: AgentType.BILLING,
        IntentCategory.ACCOUNT:    AgentType.GENERAL,
        IntentCategory.ACCOUNT_SECURITY: AgentType.BILLING,
        IntentCategory.ESCALATION: AgentType.ESCALATION,
        IntentCategory.HUMAN_HANDOFF: AgentType.ESCALATION,
        # 其余意图 → GENERAL（默认）
    }

    _TASK_GUARDS = {
        AgentType.GENERAL: "仅处理订单、物流、账户资料和一般咨询，不代替其他领域判断",
        AgentType.TECHNICAL: "仅分析技术故障、错误原因及安全排查步骤，不处理账单结论",
        AgentType.BILLING: "仅核实账单、支付、退款或发票政策，不执行资金写操作",
        AgentType.ESCALATION: "仅整理需要升级的原因和已尝试步骤，不承诺真实处理结果",
    }

    def __init__(
        self,
        api_key:  str,
        base_url: Optional[str] = None,
        model:    str = "claude-3-5-sonnet-20241022",
        skill_manager: Optional[Any] = None,
        provider: Optional[str] = None,
        supervisor_max_subtasks: int = 4,
        subtask_timeout_s: float = 30.0,
        client=None,
        recognizer=None,
        tool_manager: Optional[Any] = None,
    ):
        client = client or LLMClient(api_key=api_key, base_url=base_url, model=model, provider=provider)

        self._intent_recognizer = recognizer or IntentRecognizer(api_key=api_key, base_url=base_url, model=model, provider=provider)
        self._skill_manager = skill_manager
        self._tool_manager = tool_manager

        # Agent 池：每种类型可有多个实例（水平扩展）
        self._pool: Dict[AgentType, List[BaseAgent]] = {
            AgentType.GENERAL:   [GeneralAgent(client, model, skill_manager, tool_manager)],
            AgentType.TECHNICAL: [TechnicalAgent(client, model, skill_manager, tool_manager)],
            AgentType.BILLING:   [BillingAgent(client, model, skill_manager, tool_manager)],
            AgentType.ESCALATION: [EscalationAgent(client, model, skill_manager, tool_manager)],
        }
        self._max_subtasks = max(1, int(supervisor_max_subtasks))
        self._scheduler = DAGScheduler(
            allowed_agent_types=(agent_type.value for agent_type in AgentType),
            max_subtasks=self._max_subtasks,
            timeout_s=subtask_timeout_s,
        )
        self._synthesizer = ResponseSynthesizer()
        # 请求级工具调用轨迹：有界队列，供 /trace/tool/{request_id} 和
        # /trace/tools 回放一次请求里实际发生过的工具调用（不落库，进程重启即丢失，
        # 只用于排查和演示，不作为审计凭证——审计凭证是 action_runtime 的证据链）。
        self._recent_tool_traces: "deque[Dict[str, Any]]" = deque(
            maxlen=int(os.getenv("RESOLVEFLOW_TOOL_TRACE_MAX", "200"))
        )

    def _tool_trace_store(self) -> "deque[Dict[str, Any]]":
        """惰性拿到（必要时创建）trace 队列。测试里常用 object.__new__(AgentOrchestrator)
        绕过 __init__ 直接手工拼装最小实例，这种实例没有 __init__ 设过的属性；
        观测性功能不应该让这类已有测试用例因为缺一个属性而报错。"""
        store = getattr(self, "_recent_tool_traces", None)
        if store is None:
            store = deque(maxlen=int(os.getenv("RESOLVEFLOW_TOOL_TRACE_MAX", "200")))
            self._recent_tool_traces = store
        return store

    def _record_tool_trace(self, result: "OrchestratorResult") -> None:
        self._tool_trace_store().append({
            "request_id": result.request_id,
            "agent_type": result.agent_type.value if result.agent_type else None,
            "agent_types": [agent.value for agent in result.agent_types],
            "tools_used": list(result.tools_used),
            "tool_calls": list(result.tool_traces),
            "latency_ms": round(result.latency_ms, 1),
        })

    def get_tool_trace(self, request_id: str) -> Optional[Dict[str, Any]]:
        for trace in reversed(self._tool_trace_store()):
            if trace.get("request_id") == request_id:
                return trace
        return None

    def get_recent_tool_traces(self, limit: int = 20) -> List[Dict[str, Any]]:
        store = self._tool_trace_store()
        if not store:
            return []
        limit = max(1, min(int(limit or 20), len(store)))
        return list(reversed(list(store)[-limit:]))

    def set_skill_manager(self, skill_manager: Optional[Any]) -> None:
        """更新 SkillManager 引用，供运行时重载或测试替换使用。"""
        self._skill_manager = skill_manager
        for agents in self._pool.values():
            for agent in agents:
                agent._skill_manager = skill_manager

    async def recognize_intent(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]] = None,
    ):
        """对外暴露意图识别，供 API 层先判断是否需要 RAG 等前置能力。"""
        return await self._intent_recognizer.recognize(message, history=history)

    @staticmethod
    async def execute_action(runtime, *, owner, message="", task_id=None, order_id=None, conversation_id=None):
        """Shared entry for chat actions and direct task API, including resume."""
        task = runtime.get(task_id, owner) if task_id else runtime.create(owner, message, conversation_id)
        return await runtime.advance(task["id"], owner, order_id, message, conversation_id)

    # ── 主入口 ────────────────────────────────────────────────────────────────

    async def run(self, req: Request) -> OrchestratorResult:
        """
        处理一次请求的完整流程：
          意图识别 → 路由选 Agent → 执行 → 检查升级 → 返回结果
        """
        t0 = time.monotonic()

        # 1. 意图识别（如果调用方已识别则跳过）
        if req.intent is None:
            intent_result = await self._intent_recognizer.recognize(req.message, history=req.history)
            req.intent  = intent_result.intent
            req.intent_group = intent_result.intent_group
            req.urgency = intent_result.urgency
            req.intent_confidence = intent_result.confidence
            req.intents = list(getattr(intent_result, "intents", []))

        if self._needs_clarification(req):
            return OrchestratorResult(
                request_id=req.request_id,
                response="我还不能确定您要处理的是哪类问题。请补充一下是订单物流、退款账单、账户资料，还是技术故障？",
                agent_type=AgentType.GENERAL,
                intent=req.intent,
                escalated=False,
                latency_ms=(time.monotonic() - t0) * 1000,
                agent_types=[AgentType.GENERAL],
                primary_agent=AgentType.GENERAL,
                routing_reason="低置信度 OTHER 意图，先澄清用户需求",
                routing_confidence=req.intent_confidence,
            )

        # 复杂问题自动并行协作，例如同一句同时涉及登录故障和扣款/退款。
        decision = self._route_decision(req)
        if decision.multi_agent:
            return await self.run_parallel(req, decision)

        # 2. 执行主 Agent（含降级）
        response = await self._execute(req, decision.primary_agent)

        # 4. 升级检查
        escalated = False
        if decision.primary_agent == AgentType.ESCALATION or response.escalate or self._requires_high_risk_escalation(req) or req.urgency == UrgencyLevel.CRITICAL or req.intent in (
            IntentCategory.ESCALATION,
            IntentCategory.HUMAN_HANDOFF,
        ):
            escalated = True
            logger.warning(f"请求 {req.request_id} 触发升级: urgency={req.urgency}")
            # 生产环境：此处创建工单、通知人工客服

        result = OrchestratorResult(
            request_id=req.request_id,
            response=response.content,
            agent_type=response.agent_type,
            intent=req.intent,
            escalated=escalated,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=[response.agent_type],
            primary_agent=decision.primary_agent,
            supporting_agents=[],
            routing_reason=decision.reason,
            routing_confidence=decision.confidence,
            tools_used=list(response.tools_used),
            tool_traces=list(response.tool_traces),
            error_diagnostics=[response.error_diagnostic] if response.error_diagnostic else [],
        )
        self._record_tool_trace(result)
        return result

    async def run_parallel(self, req: Request, decision: RoutingDecision) -> OrchestratorResult:
        """
        将复合请求拆成有限子任务，按 DAG 调度，再受约束地合成结果。
        """
        t0 = time.monotonic()
        self._ensure_supervisor_components()
        risk_reasons = self._high_risk_reasons(req)
        plan, degradations = self._build_plan_or_fallback(req, decision, risk_reasons)
        task_results, degradations = await self._schedule_or_fallback(plan, req, degradations)

        force_escalation = decision.primary_agent == AgentType.ESCALATION or bool(risk_reasons) or req.urgency == UrgencyLevel.CRITICAL or req.intent in (
            IntentCategory.ESCALATION,
            IntentCategory.HUMAN_HANDOFF,
        )
        try:
            synthesis = self._synthesizer.synthesize(
                original_request=req.message,
                plan=plan,
                results=task_results,
                force_escalation=force_escalation,
            )
            combined = synthesis.content
            synthesis_method = synthesis.method
            degradations.extend(synthesis.degradations)
            escalated = synthesis.escalated
        except Exception as ex:
            logger.error("ResponseSynthesizer 失败，使用确定性安全合并: %s", type(ex).__name__)
            degradations.append("response_synthesizer_unavailable")
            combined = self._fallback_synthesis(task_results)
            synthesis_method = "fallback"
            escalated = force_escalation or any(result.escalated for result in task_results)

        combined, safety_escalate = BaseAgent._apply_safety_guard(combined)
        escalated = escalated or safety_escalate
        actual_agent_types = list(dict.fromkeys(
            AgentType(result.agent_type)
            for result in task_results
            if result.success and result.agent_type in {agent.value for agent in AgentType}
        ))

        result = OrchestratorResult(
            request_id=req.request_id,
            response=combined,
            agent_type=decision.primary_agent,
            intent=req.intent,
            escalated=escalated,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=actual_agent_types or decision.agent_types,
            primary_agent=decision.primary_agent,
            supporting_agents=decision.supporting_agents,
            routing_reason=decision.reason,
            routing_confidence=decision.confidence,
            task_plan=plan.to_dict(),
            execution_trace=[task_result.to_trace() for task_result in task_results],
            synthesis_method=synthesis_method,
            degradations=list(dict.fromkeys(degradations)),
            tools_used=list(dict.fromkeys(name for task_result in task_results for name in task_result.tools_used)),
            tool_traces=[trace for task_result in task_results for trace in task_result.tool_traces],
        )
        self._record_tool_trace(result)
        return result

    async def run_compound(self, req: Request, primary_result: SubTaskResult) -> OrchestratorResult:
        """处理复合请求里"已经由调用方算好"的那一部分（Action 层执行结果，或知识库
        RAG 政策回答）之外的剩余通用/技术内容——req.message 应当只是剩余部分的文本
        （由 ConversationService 传入 GoalProposal.general_remainder），不是完整原话，
        这样这里的规划/调度不会重新发现、重复回答已经由 primary_result 处理过的话题。

        primary_result.agent_type 通常是 "action" 或 "policy"；orchestrator 本身不关心
        它具体是哪个路由算出来的，只负责把它和剩余部分的调度结果拼接合成成一条回复——
        并且不会让它参与 ResponseSynthesizer 的关键词冲突检测（见 supervisor.py 里
        synthesize() 的 extra_results 参数说明）。
        """
        t0 = time.monotonic()
        self._ensure_supervisor_components()
        if req.intent is None:
            intent_result = await self._intent_recognizer.recognize(req.message, history=req.history)
            req.intent = intent_result.intent
            req.intent_group = intent_result.intent_group
            req.urgency = intent_result.urgency
            req.intent_confidence = intent_result.confidence
            req.intents = list(getattr(intent_result, "intents", []))

        decision = self._route_decision(req)
        risk_reasons = self._high_risk_reasons(req)
        plan, degradations = self._build_plan_or_fallback(req, decision, risk_reasons)
        task_results, degradations = await self._schedule_or_fallback(plan, req, degradations)

        force_escalation = decision.primary_agent == AgentType.ESCALATION or bool(risk_reasons) or req.urgency == UrgencyLevel.CRITICAL or req.intent in (
            IntentCategory.ESCALATION,
            IntentCategory.HUMAN_HANDOFF,
        )
        try:
            synthesis = self._synthesizer.synthesize(
                original_request=req.message,
                plan=plan,
                results=task_results,
                extra_results=[primary_result],
                force_escalation=force_escalation,
            )
            combined = synthesis.content
            synthesis_method = synthesis.method
            degradations.extend(synthesis.degradations)
            escalated = synthesis.escalated
        except Exception as ex:
            logger.error("ResponseSynthesizer 失败，使用确定性安全合并: %s", type(ex).__name__)
            degradations.append("response_synthesizer_unavailable")
            combined = self._fallback_synthesis([primary_result, *task_results])
            synthesis_method = "fallback"
            escalated = force_escalation or any(result.escalated for result in [primary_result, *task_results])

        combined, safety_escalate = BaseAgent._apply_safety_guard(combined)
        escalated = escalated or safety_escalate or primary_result.escalated
        actual_agent_types = list(dict.fromkeys(
            AgentType(result.agent_type)
            for result in task_results
            if result.success and result.agent_type in {agent.value for agent in AgentType}
        ))

        result = OrchestratorResult(
            request_id=req.request_id,
            response=combined,
            agent_type=decision.primary_agent,
            intent=req.intent,
            escalated=escalated,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=actual_agent_types or decision.agent_types,
            primary_agent=decision.primary_agent,
            supporting_agents=decision.supporting_agents,
            routing_reason=decision.reason,
            routing_confidence=decision.confidence,
            task_plan=plan.to_dict(),
            execution_trace=[primary_result.to_trace()] + [task_result.to_trace() for task_result in task_results],
            synthesis_method=synthesis_method,
            degradations=list(dict.fromkeys(degradations)),
            tools_used=list(dict.fromkeys(
                name for task_result in [primary_result, *task_results] for name in task_result.tools_used
            )),
            tool_traces=[trace for task_result in [primary_result, *task_results] for trace in task_result.tool_traces],
        )
        self._record_tool_trace(result)
        return result

    @staticmethod
    def _redact_task_text(text: str) -> str:
        """Keep sensitive identifiers out of plan/trace descriptions."""
        text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]", text)
        text = re.sub(r"(?<!\d)1\d{10}(?!\d)", "[REDACTED_PHONE]", text)
        text = re.sub(r"(?<!\d)\d{12,19}(?!\d)", "[REDACTED_NUMBER]", text)
        text = re.sub(
            r"(?i)(密码|验证码)\s*[:：=]?\s*[A-Za-z0-9_-]{4,}",
            r"\1=[REDACTED]", text,
        )
        return text

    def _agent_for_intent(self, intent: IntentCategory) -> AgentType:
        """Only server-owned intent-to-agent mappings can grant a domain."""
        agent = self._INTENT_ROUTING.get(intent, AgentType.GENERAL)
        return agent if self._pool.get(agent) else AgentType.GENERAL

    def _build_task_plan(self, req: Request, decision: RoutingDecision,
                         risk_reasons: List[str]) -> TaskPlan:
        """Build executable tasks directly from recognized intent spans.

        No separate planner and no second keyword-based business parsing.
        Dependency edges come from indices validated by IntentRecognizer.
        """
        agent_types = list(dict.fromkeys(decision.agent_types))
        if len(agent_types) > getattr(self, "_max_subtasks", 4):
            raise ValueError("too_many_subtasks")
        items = list(req.intents)
        if not items and req.intent not in (None, IntentCategory.OTHER):
            # Direct/legacy single-intent requests retain a one-task plan.
            items = [RecognizedIntent(req.intent, req.message, req.intent_confidence)]
        agents_by_index = [self._agent_for_intent(item.intent) for item in items]
        scopes: Dict[AgentType, List[str]] = {agent: [] for agent in agent_types}
        edges: Dict[AgentType, List[AgentType]] = {agent: [] for agent in agent_types}

        for index, item in enumerate(items):
            agent = agents_by_index[index]
            if agent not in scopes:
                continue
            # Never let ungrounded LLM text become executable task scope.
            if item.text and item.text in req.message and item.text not in scopes[agent]:
                scopes[agent].append(item.text)
            for dependency in item.depends_on:
                if (not isinstance(dependency, int) or isinstance(dependency, bool)
                        or dependency < 0 or dependency >= index):
                    raise ValueError("invalid_intent_dependency")
                previous_agent = agents_by_index[dependency]
                if (previous_agent != agent and previous_agent in scopes
                        and previous_agent not in edges[agent]):
                    edges[agent].append(previous_agent)

        subtasks: List[SubTask] = []
        for agent in agent_types:
            phrases = scopes[agent]
            # A single legacy request is already scoped to one Agent.
            if not phrases and len(agent_types) == 1 and not req.intents:
                phrases = [req.message]
            guard = self._TASK_GUARDS[agent]
            description = guard
            if phrases:
                description += "。聚焦：" + self._redact_task_text("；".join(phrases))
            risk_level = ("critical" if agent == AgentType.ESCALATION else
                          "high" if risk_reasons and agent in {AgentType.BILLING, AgentType.GENERAL} else
                          "medium" if risk_reasons else "low")
            subtasks.append(SubTask(
                id=f"{agent.value}_1",
                description=description,
                agent_type=agent.value,
                dependencies=[f"{prior.value}_1" for prior in edges[agent]],
                risk_level=risk_level,
                expected_output="给出可核实的处理结果，不越权承诺或执行敏感操作",
            ))

        plan = TaskPlan(
            original_request=req.message,
            subtasks=subtasks,
            reason=decision.reason,
            confidence=decision.confidence,
            method="intent_mapping",
        )
        self._scheduler.validate(plan)
        return plan

    def _build_plan_or_fallback(self, req: Request, decision: RoutingDecision,
                                risk_reasons: List[str]):
        """On invalid task graphs, safely degrade to the primary Agent only."""
        degradations: List[str] = []
        try:
            return self._build_task_plan(req, decision, risk_reasons), degradations
        except Exception as ex:
            logger.warning("Intent task graph failed, degrade to primary Agent: %s", type(ex).__name__)
            degradations.append("task_plan_unavailable")
            primary = decision.primary_agent
            plan = TaskPlan(
                original_request=req.message,
                subtasks=[SubTask(
                    id=f"{primary.value}_fallback_1",
                    description=self._TASK_GUARDS[primary] + "。仅处理主领域的安全咨询",
                    agent_type=primary.value,
                    risk_level="high" if risk_reasons else "low",
                    expected_output="不猜测未验证的其他领域结果",
                )],
                reason="invalid multi-intent task graph; fallback to primary Agent",
                confidence=decision.confidence,
                method="fallback",
            )
            self._scheduler.validate(plan)
            return plan, degradations

    async def _schedule_or_fallback(self, plan: TaskPlan, req: Request, degradations: List[str]):
        """按计划调度子任务；调度器失败时把每个子任务标记为失败，保留可观测性。
        run_parallel() 和 run_compound() 共用。"""
        async def execute_subtask(
            task: SubTask,
            dependency_results: Dict[str, SubTaskResult],
        ) -> SubTaskResult:
            dependency_context = "\n\n".join(
                result.content for result in dependency_results.values()
                if result.success and result.content
            )
            scoped_request = replace(
                req,
                subtask_id=task.id,
                task_instruction=task.description,
                dependency_context=dependency_context,
            )
            response = await self._execute(scoped_request, AgentType(task.agent_type))
            return SubTaskResult(
                task_id=task.id,
                agent_type=response.agent_type.value,
                success=response.success,
                content=response.content,
                latency_ms=response.latency_ms,
                error=None if response.success else "agent_execution_failed",
                escalated=response.escalate,
                status=TaskStatus.SUCCESS if response.success else TaskStatus.FAILED,
                tools_used=list(response.tools_used),
                tool_traces=list(response.tool_traces),
            )

        try:
            task_results = await self._scheduler.execute(plan, execute_subtask)
        except Exception as ex:
            logger.error("DAG Scheduler 失败: %s", type(ex).__name__)
            degradations = [*degradations, "task_scheduler_unavailable"]
            task_results = [SubTaskResult(
                task_id=task.id,
                agent_type=task.agent_type,
                success=False,
                error=type(ex).__name__,
                status=TaskStatus.FAILED,
                dependencies=list(task.dependencies),
            ) for task in plan.subtasks]
        return task_results, degradations

    def _ensure_supervisor_components(self) -> None:
        """Initialize optional components for tests using object.__new__."""
        if not hasattr(self, "_max_subtasks"):
            self._max_subtasks = 4
        if not hasattr(self, "_scheduler"):
            self._scheduler = DAGScheduler(
                allowed_agent_types=(agent.value for agent in AgentType),
                max_subtasks=self._max_subtasks,
                timeout_s=30.0,
            )
        if not hasattr(self, "_synthesizer"):
            self._synthesizer = ResponseSynthesizer()

    @staticmethod
    def _fallback_synthesis(results: List[SubTaskResult]) -> str:
        """Synthesizer 自身不可用时，保留成功证据并明确失败部分。"""
        parts = [
            f"【{result.agent_type}】\n{result.content.strip()}"
            for result in results if result.success and result.content.strip()
        ]
        if any(not result.success for result in results):
            parts.append("【暂未完成】\n部分专业处理环节暂时不可用，请稍后重试或转人工核实。")
        return "\n\n".join(parts) if parts else "抱歉，当前各专业处理环节均未能完成，请稍后重试或转人工核实。"

    # ── 路由逻辑 ──────────────────────────────────────────────────────────────

    def _route_decision(self, req: Request) -> RoutingDecision:
        """One deterministic routing pass over the recognized intent list.

        With legacy single-intent data, route just that one intent. Do not
        infer additional business domains from conjunctions or keywords.
        """
        items = list(req.intents)
        if not items and req.intent not in (None, IntentCategory.OTHER):
            items = [RecognizedIntent(req.intent, req.message, req.intent_confidence)]
        participating: List[AgentType] = []
        for item in items:
            agent = self._agent_for_intent(item.intent)
            if agent not in participating:
                participating.append(agent)
        explicit_handoff = any(
            item.intent in {IntentCategory.ESCALATION, IntentCategory.HUMAN_HANDOFF}
            for item in items
        )
        if req.urgency == UrgencyLevel.CRITICAL or explicit_handoff:
            primary = AgentType.ESCALATION
            supporting = [agent for agent in participating if agent != primary]
            reason = "explicit_handoff_or_critical"
        elif participating:
            primary = participating[0]
            supporting = participating[1:]
            reason = "direct_multi_intent_mapping"
        else:
            primary, supporting, reason = AgentType.GENERAL, [], "no_recognized_intent"
        labels = ",".join(item.intent.value for item in items)
        return RoutingDecision(
            primary_agent=primary,
            supporting_agents=supporting,
            reason=f"{reason}; intents=[{labels}]; primary={primary.value}",
            confidence=min(1.0, max(0.0, float(req.intent_confidence))),
        )

    @staticmethod
    def _needs_clarification(req: Request) -> bool:
        """低置信度且无明确意图时，先追问，避免误路由。"""
        if req.intent != IntentCategory.OTHER:
            return False
        text = (req.message or "").strip()
        if len(text) <= 2:
            return False
        return req.intent_confidence < 0.5

    @staticmethod
    def _requires_high_risk_escalation(req: Request) -> bool:
        """高风险场景使用显式规则触发升级，不依赖模型是否说出转人工。"""
        return bool(AgentOrchestrator._high_risk_reasons(req))

    @staticmethod
    def _high_risk_reasons(req: Request) -> List[str]:
        """返回触发升级的通用风险原因，不绑定具体评测样本。"""
        reasons: List[str] = []
        # A secondary sensitive intent must not bypass escalation.
        categories = {req.intent} | {item.intent for item in req.intents}
        if IntentCategory.ACCOUNT_SECURITY in categories:
            reasons.append("account_security_intent")
        if IntentCategory.PAYMENT_ISSUE in categories:
            reasons.append("payment_issue_intent")
        if categories.intersection({IntentCategory.ESCALATION, IntentCategory.HUMAN_HANDOFF}):
            reasons.append("handoff_intent")
        message = (req.message or "").lower()
        handoff_keywords = (
            "转人工", "人工客服", "找人工", "我要人工", "真人客服", "投诉升级",
            "正式升级", "升级处理", "转交专员", "找负责人",
        )
        handoff_inquiry = any(phrase in message for phrase in ("是否需要升级", "要不要升级", "是否升级"))
        if not handoff_inquiry and any(keyword in message for keyword in handoff_keywords):
            reasons.append("explicit_handoff_keyword")
        payment_terms = ("扣款", "扣费", "支付", "账单", "收费", "交易")
        payment_risk_terms = ("不认识", "异常", "重复", "盗", "不是我", "多扣", "争议", "未经授权", "不一致")
        if any(term in message for term in payment_terms) and any(term in message for term in payment_risk_terms):
            reasons.append("payment_risk")
        security_terms = ("被盗", "盗用", "陌生设备", "异常登录", "安全风险", "泄露", "冻结账户")
        if any(term in message for term in security_terms):
            reasons.append("account_security_risk")
        credential_terms = ("密码", "验证码", "敏感信息", "敏感凭据")
        if any(term in message for term in credential_terms) and any(term in message for term in ("索要", "提供", "发送", "不要")):
            reasons.append("credential_safety")
        refund_action_terms = ("申请退款", "提交退款", "退款申请", "取消退款", "退款争议")
        refund_information_only = any(term in message for term in ("是否符合退款条件", "退款条件是什么", "能否退款", "可以退款吗"))
        if IntentCategory.REFUND in categories and not refund_information_only and any(
            term in message for term in refund_action_terms
        ):
            reasons.append("refund_action")
        refund_review_terms = ("退款结果", "申请复核", "人工审核", "审核完成", "审核结果")
        if IntentCategory.REFUND in categories and any(term in message for term in refund_review_terms):
            reasons.append("refund_review")
        if (
            IntentCategory.REFUND in categories
            and "退款资格" in message
            and any(term in message for term in ("争议", "复核", "审核", "确认我的"))
        ):
            reasons.append("refund_eligibility_dispute")
        if req.intent == IntentCategory.COMPLAINT and any(
            term in message for term in ("严肃处理", "不要让我继续等待", "互相矛盾", "正式投诉")
        ):
            reasons.append("complaint_risk")
        if any(term in message for term in ("已经重试", "多次重试", "反复重试")) and any(
            term in message for term in ("失败", "报错", "转圈", "无法", "仍然", "还是")
        ):
            reasons.append("retry_exhausted")
        repeated_failure_terms = ("反复", "多次", "一直", "长期", "仍未解决")
        if any(term in message for term in repeated_failure_terms) and any(term in message for term in ("失败", "报错", "没有结果", "无效")):
            reasons.append("repeated_failure")
        return list(dict.fromkeys(reasons))

    def _best_agent(self, agent_type: AgentType) -> Optional[BaseAgent]:
        """
        性能路由：从同类 Agent 中选 routing_score() 最高的。
        这是"基于在线表现动态调整路由"的核心。
        """
        agents = self._pool.get(agent_type, [])
        if not agents:
            return None
        return max(agents, key=lambda a: a.stats.routing_score())

    async def _execute(self, req: Request, agent_type: AgentType) -> AgentResponse:
        """执行 Agent，失败时降级到 GeneralAgent。"""
        agent = self._best_agent(agent_type)
        if agent is None:
            agent = self._best_agent(AgentType.GENERAL)
        if agent is None:
            return AgentResponse(
                agent_type=AgentType.GENERAL,
                content="服务暂时不可用，请稍后重试。",
                success=False,
            )

        response = await agent.handle(req)

        # 专属 Agent 失败时降级到 GeneralAgent
        if not response.success and agent_type != AgentType.GENERAL:
            logger.warning(f"{agent_type.value} 失败，降级到 GeneralAgent")
            fallback = self._best_agent(AgentType.GENERAL)
            if fallback:
                response = await fallback.handle(req)

        return response

    # ── 统计（供 Monitor 读取）────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        result = {}
        for agent_type, agents in self._pool.items():
            for i, agent in enumerate(agents):
                key = f"{agent_type.value}_{i}"
                result[key] = {
                    "total":        agent.stats.total,
                    "success_rate": round(agent.stats.success_rate, 3),
                    "avg_ms":       round(agent.stats.avg_ms, 1),
                    "monitor_penalty": round(agent.stats.monitor_penalty, 3),
                    "routing_score": round(agent.stats.routing_score(), 3),
                }
        return result

    def update_routing_penalties(self, penalties: Dict[str, float]) -> None:
        """
        接收 Monitor 的在线表现反馈，动态调整路由惩罚项。

        penalties 的 key 使用 get_stats() 中的 agent key，例如 technical_0。
        """
        for agent_type, agents in self._pool.items():
            for i, agent in enumerate(agents):
                key = f"{agent_type.value}_{i}"
                penalty = penalties.get(key, 0.0)
                agent.stats.monitor_penalty = min(max(penalty, 0.0), 0.9)
