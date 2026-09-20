"""共享 Agent 工具定义（老系统闲聊/通用链路专用）。

这些工具服务于 agents/agent_orchestrator.py 里的 GeneralAgent / TechnicalAgent /
BillingAgent / EscalationAgent —— 和 agents/action_policy.py（受控业务执行层的
工具，走 action_runtime 的确认/审批流程）是两套完全独立的工具体系：这里的工具
只读、确定性、无副作用，Agent 可以在回答闲聊/咨询类问题时自由调用，不需要经过
确认或审批。

每个 Agent 通过 TOOL_SCOPES 拿到自己的工具子集；共享的知识库检索工具由
build_shared_rag_tool() 在启动时用真实的 MCPToolManager 包一层，实现"Agent 自己
判断要不要检索知识库"，而不是像过去那样由上层按意图硬编码决定。
"""
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

ToolHandler = Callable[[Any, Dict[str, Any]], Union[Dict[str, Any], Awaitable[Dict[str, Any]]]]


@dataclass(frozen=True)
class AgentToolSpec:
    """单个 Agent 工具的完整定义：描述、JSON Schema 参数、处理函数。"""
    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    handler: Optional[ToolHandler] = None


# ── 确定性工具：无外部依赖，纯规则/纯计算 ──────────────────────────────────────

_KNOWN_ERROR_CODES = {
    "401": "身份验证失败，通常是登录态过期或凭据错误，需要重新登录或重置密码。",
    "403": "权限不足，账号可能缺少对应权益或被限制访问该资源。",
    "404": "请求的资源不存在，常见于订单号、链接或页面拼写错误。",
    "429": "请求过于频繁被限流，建议提示用户稍后重试。",
    "500": "服务端内部错误，通常与本次输入无关，需要记录现场供技术团队排查。",
    "502": "网关或上游服务暂时不可用，多为临时性故障，建议重试。",
    "503": "服务暂时过载或正在维护，建议稍后重试。",
}


def inspect_request_context(req: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """把当前请求已知的结构化信息（意图、紧急度、实体）原样回显给模型，
    避免模型凭空猜测用户还没提供的信息。"""
    return {
        "success": True,
        "intent": req.intent.value if req.intent else None,
        "intent_group": req.intent_group,
        "urgency": req.urgency.value if req.urgency else None,
        "entities": dict(req.entities or {}),
    }


def lookup_error_code(req: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """查询常见 HTTP/业务错误码的含义，辅助技术 Agent 给出针对性排查步骤。"""
    code = str(args.get("code", "")).strip()
    if not code:
        return {"success": False, "error": "缺少 code 参数"}
    explanation = _KNOWN_ERROR_CODES.get(code)
    if explanation is None:
        return {"success": True, "known": False, "code": code,
                "explanation": "未收录该错误码，建议按通用排查步骤处理并记录日志现场。"}
    return {"success": True, "known": True, "code": code, "explanation": explanation}


def check_billing_fields(req: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """检查处理账单/退款问题所需的关键字段（订单号、金额）是否已经在当前请求
    的结构化实体里出现，缺失时明确列出，避免账单 Agent 靠猜测继续对话。"""
    entities = req.entities or {}
    missing = [field_name for field_name, key in (("order_id", "order_id"), ("amount", "amount"))
               if not entities.get(key)]
    return {"success": True, "missing_fields": missing, "has_all_fields": not missing,
            "known_order_ids": entities.get("order_id", []), "known_amounts": entities.get("amount", [])}


def compare_amounts(req: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """比较用户提到的两个金额（例如“账单显示 199，实际扣款 398”），
    给出差值和是否存在多扣/少扣，避免模型自己做不可靠的心算。"""
    try:
        expected = float(args.get("expected"))
        actual = float(args.get("actual"))
    except (TypeError, ValueError):
        return {"success": False, "error": "expected/actual 必须是数字"}
    diff = round(actual - expected, 2)
    return {"success": True, "expected": expected, "actual": actual, "difference": diff,
            "overcharged": diff > 0, "undercharged": diff < 0, "matches": diff == 0}


def create_handoff_summary(req: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    """生成转人工时的交接摘要，包含意图、紧急度和已知实体，方便人工客服快速
    接手，不需要用户重新描述一遍问题。"""
    entities = req.entities or {}
    return {
        "success": True,
        "summary": {
            "message": req.message,
            "intent": req.intent.value if req.intent else None,
            "urgency": req.urgency.value if req.urgency else None,
            "known_entities": {k: v for k, v in entities.items() if v},
            "reason": args.get("reason", ""),
        },
    }


# ── 共享 RAG 工具：包一层真实的 MCPToolManager ────────────────────────────────

def build_shared_rag_tool(tool_manager: Any, tool_name: str = "knowledge_search", top_k: int = 5) -> AgentToolSpec:
    """把已经注册好缓存/熔断/查询改写/重排能力的 MCPToolManager 包成一个
    Agent 工具，让 Agent 在回答问题时自己判断要不要检索知识库，而不是像过去
    那样由上层按意图硬编码决定是否触发 RAG。"""

    async def handler(req: Any, args: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query") or req.message)
        try:
            result = await tool_manager.search_with_rewrite(tool_name, query, top_k=int(args.get("top_k") or top_k))
        except Exception as ex:  # 工具管理器自身已有 fallback，这里兜底避免异常直接抛给 Agent 循环
            return {"success": False, "error": str(ex)}
        items = result.get("results") if isinstance(result, dict) else result
        return {
            "success": True,
            "results": items,
            "cached": bool(isinstance(result, dict) and result.get("cached")),
            "reranked": bool(isinstance(result, dict) and result.get("reranked")),
        }

    return AgentToolSpec(
        name=tool_name,
        description="检索订阅/客服知识库（向量 + BM25 混合召回，命中后自动重排）。当需要引用具体政策条款或事实性资料时调用；无关的寒暄不需要调用。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索用的查询语句，默认使用用户原话"},
                "top_k": {"type": "integer", "description": "返回结果数量，默认 5"},
            },
        },
        handler=handler,
    )


# ── 按 Agent 类型分组的工具集 ──────────────────────────────────────────────────

def general_tools() -> Dict[str, AgentToolSpec]:
    return {
        "inspect_request_context": AgentToolSpec(
            name="inspect_request_context",
            description="查看当前请求已经识别出的意图、紧急度和结构化实体，避免猜测用户还没提供的信息。",
            handler=inspect_request_context,
        ),
        "create_handoff_summary": AgentToolSpec(
            name="create_handoff_summary",
            description="当问题超出通用客服范围、需要建议用户转接专业客服或人工时，生成交接摘要。",
            parameters={"type": "object", "properties": {"reason": {"type": "string", "description": "转接原因"}}},
            handler=create_handoff_summary,
        ),
    }


def technical_tools() -> Dict[str, AgentToolSpec]:
    return {
        "lookup_error_code": AgentToolSpec(
            name="lookup_error_code",
            description="查询常见错误码（401/403/404/429/500/502/503 等）的含义，给出针对性排查方向。",
            parameters={"type": "object", "properties": {"code": {"type": "string", "description": "错误码，例如 401"}},
                        "required": ["code"]},
            handler=lookup_error_code,
        ),
        "inspect_request_context": AgentToolSpec(
            name="inspect_request_context",
            description="查看当前请求已经识别出的意图、紧急度和结构化实体（如错误码实体），避免猜测。",
            handler=inspect_request_context,
        ),
    }


def billing_tools() -> Dict[str, AgentToolSpec]:
    return {
        "check_billing_fields": AgentToolSpec(
            name="check_billing_fields",
            description="检查处理账单/退款问题所需的订单号、金额是否已在当前请求中出现，缺失时明确列出。",
            handler=check_billing_fields,
        ),
        "compare_amounts": AgentToolSpec(
            name="compare_amounts",
            description="比较用户提到的应扣金额与实际扣款金额，给出差值、是否多扣/少扣，不要自己心算。",
            parameters={"type": "object", "properties": {
                "expected": {"type": "number", "description": "应扣金额"},
                "actual": {"type": "number", "description": "实际扣款金额"},
            }, "required": ["expected", "actual"]},
            handler=compare_amounts,
        ),
    }


def escalation_tools() -> Dict[str, AgentToolSpec]:
    return {
        "create_handoff_summary": AgentToolSpec(
            name="create_handoff_summary",
            description="生成转人工交接摘要，包含意图、紧急度和已知实体。",
            parameters={"type": "object", "properties": {"reason": {"type": "string", "description": "升级原因"}}},
            handler=create_handoff_summary,
        ),
        "inspect_request_context": AgentToolSpec(
            name="inspect_request_context",
            description="查看当前请求已经识别出的意图、紧急度和结构化实体。",
            handler=inspect_request_context,
        ),
    }


TOOL_SCOPES: Dict[str, Callable[[], Dict[str, AgentToolSpec]]] = {
    "general": general_tools,
    "technical": technical_tools,
    "billing": billing_tools,
    "escalation": escalation_tools,
}


def build_tool_registry(agent_type_value: str, tool_manager: Optional[Any] = None) -> Dict[str, AgentToolSpec]:
    """按 Agent 类型组装最终工具表：确定性工具 + （如有 tool_manager）共享 RAG 工具。"""
    factory = TOOL_SCOPES.get(agent_type_value)
    tools = dict(factory()) if factory else {}
    if tool_manager is not None and agent_type_value != "escalation":
        rag_tool = build_shared_rag_tool(tool_manager)
        tools[rag_tool.name] = rag_tool
    return tools
