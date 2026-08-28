"""受控的多 Agent 任务规划、DAG 调度与结果合成。

该模块刻意不实现开放式 ReAct 循环。规划器只把已经由路由层确认的领域拆成
有限子任务；调度器只执行经过验证的 DAG；合成器只使用 Agent 已返回的事实。
因此核心控制面可以在没有外部 LLM API 的情况下做确定性测试。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Sequence


class PlanValidationError(ValueError):
    """TaskPlan 不满足结构或 DAG 约束。"""


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class SubTask:
    id: str
    description: str
    agent_type: str
    dependencies: List[str] = field(default_factory=list)
    risk_level: str = "low"
    expected_output: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TaskPlan:
    original_request: str
    subtasks: List[SubTask]
    reason: str
    confidence: float
    method: str = "deterministic"

    def to_dict(self, *, include_original_request: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "subtasks": [task.to_dict() for task in self.subtasks],
            "reason": self.reason,
            "confidence": self.confidence,
            "method": self.method,
        }
        # API Trace 默认不回显用户原文，避免敏感数据被重复传播。
        if include_original_request:
            payload["original_request"] = self.original_request
        return payload


@dataclass
class SubTaskResult:
    task_id: str
    agent_type: str
    success: bool
    content: str = ""
    latency_ms: float = 0.0
    error: Optional[str] = None
    escalated: bool = False
    status: TaskStatus = TaskStatus.PENDING
    dependencies: List[str] = field(default_factory=list)

    def to_trace(self) -> Dict[str, Any]:
        """只输出控制面信息，不把 Agent 正文或异常细节写入 Trace。"""
        return {
            "task_id": self.task_id,
            "agent_type": self.agent_type,
            "status": self.status.value,
            "success": self.success,
            "latency_ms": round(self.latency_ms, 2),
            "escalated": self.escalated,
            "dependencies": list(self.dependencies),
            "error_type": self.error.split(":", 1)[0] if self.error else None,
        }


@dataclass(frozen=True)
class SynthesisResult:
    content: str
    method: str
    escalated: bool = False
    conflicts: List[str] = field(default_factory=list)
    degradations: List[str] = field(default_factory=list)


class TaskPlanner:
    """根据已确认的主辅 Agent 生成有限、确定性的结构化任务计划。"""

    ALLOWED_RISK_LEVELS = {"low", "medium", "high", "critical"}
    _DOMAIN_TERMS: Mapping[str, Sequence[str]] = {
        "technical": (
            "登录", "报错", "错误", "error", "crash", "崩溃", "页面", "加载",
            "跳转", "网络", "验证码", "500", "401", "客户端", "系统",
        ),
        "billing": (
            "退款", "扣款", "扣费", "发票", "账单", "支付", "订阅", "金额",
            "交易", "收费", "银行卡", "refund", "invoice",
        ),
        "general": (
            "订单", "物流", "快递", "配送", "会员", "积分", "账户资料", "状态",
            "地址", "商品",
        ),
        "escalation": (
            "人工", "投诉", "升级", "负责人", "专员", "高风险", "安全",
        ),
    }
    _DEFAULT_DESCRIPTIONS = {
        "technical": "仅分析技术故障、错误原因与安全排查步骤，不处理账单结论",
        "billing": "仅核查账单、支付、退款或发票政策及升级条件，不处理技术诊断",
        "general": "仅处理订单、物流、账户资料或通用服务事项，不替代专业领域判断",
        "escalation": "仅整理升级原因、风险与已尝试步骤，不作真实处理承诺",
    }

    def __init__(self, allowed_agent_types: Iterable[str], max_subtasks: int = 3):
        self.allowed_agent_types = set(allowed_agent_types)
        self.max_subtasks = max(1, max_subtasks)

    def create_plan(
        self,
        *,
        original_request: str,
        primary_agent: str,
        supporting_agents: Sequence[str],
        reason: str,
        confidence: float,
        risk_reasons: Sequence[str] = (),
    ) -> TaskPlan:
        agent_types = list(dict.fromkeys([primary_agent, *supporting_agents]))
        if len(agent_types) > self.max_subtasks:
            agent_types = agent_types[: self.max_subtasks]

        clauses = self._split_clauses(original_request)
        task_ids: Dict[str, str] = {}
        for agent_type in agent_types:
            task_ids[agent_type] = f"{agent_type}_1"

        dependencies = self._infer_dependencies(original_request, agent_types, task_ids)
        subtasks = []
        for agent_type in agent_types:
            relevant = self._relevant_clauses(agent_type, clauses)
            raw_scope = "；".join(relevant) if relevant else original_request.strip()
            scope = self._sanitize_scope(raw_scope)
            guard = self._DEFAULT_DESCRIPTIONS.get(
                agent_type, f"仅完成分配给 {agent_type} Agent 的领域任务"
            )
            description = f"{guard}。聚焦：{scope}" if scope else guard
            risk_level = self._risk_level(agent_type, risk_reasons)
            subtasks.append(SubTask(
                id=task_ids[agent_type],
                description=description,
                agent_type=agent_type,
                dependencies=dependencies.get(agent_type, []),
                risk_level=risk_level,
                expected_output="给出可执行、可核实且不越权的处理结果",
            ))

        plan = TaskPlan(
            original_request=original_request,
            subtasks=subtasks,
            reason=reason,
            confidence=max(0.0, min(float(confidence), 1.0)),
            method="deterministic",
        )
        self.validate(plan)
        return plan

    def parse_json_plan(self, raw: str, *, original_request: str) -> TaskPlan:
        """解析可选 LLM Planner 输出；当前生产路径仍默认使用确定性规划。"""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as ex:
            raise PlanValidationError(f"planner_json_invalid: {ex.msg}") from ex
        if not isinstance(payload, dict) or not isinstance(payload.get("subtasks"), list):
            raise PlanValidationError("planner_schema_invalid: subtasks 必须是数组")
        try:
            subtasks = [
                SubTask(
                    id=item["id"],
                    description=item["description"],
                    agent_type=item["agent_type"],
                    dependencies=list(item.get("dependencies") or []),
                    risk_level=item.get("risk_level", "low"),
                    expected_output=item.get("expected_output", ""),
                )
                for item in payload["subtasks"]
            ]
            plan = TaskPlan(
                original_request=original_request,
                subtasks=subtasks,
                reason=str(payload.get("reason", "llm planner")),
                confidence=float(payload.get("confidence", 0.0)),
                method="llm",
            )
        except (KeyError, TypeError, ValueError) as ex:
            raise PlanValidationError(f"planner_schema_invalid: {type(ex).__name__}") from ex
        self.validate(plan)
        return plan

    def validate(self, plan: TaskPlan) -> None:
        if not isinstance(plan, TaskPlan):
            raise PlanValidationError("plan_type_invalid")
        if not plan.subtasks:
            raise PlanValidationError("plan_empty")
        if len(plan.subtasks) > self.max_subtasks:
            raise PlanValidationError("too_many_subtasks")
        ids = [task.id for task in plan.subtasks]
        if any(not isinstance(task_id, str) or not task_id.strip() for task_id in ids):
            raise PlanValidationError("task_id_empty")
        if len(ids) != len(set(ids)):
            raise PlanValidationError("duplicate_task_id")
        known_ids = set(ids)
        for task in plan.subtasks:
            if task.agent_type not in self.allowed_agent_types:
                raise PlanValidationError(f"unknown_agent_type:{task.agent_type}")
            if not isinstance(task.description, str) or not task.description.strip():
                raise PlanValidationError(f"empty_description:{task.id}")
            if task.risk_level not in self.ALLOWED_RISK_LEVELS:
                raise PlanValidationError(f"invalid_risk_level:{task.id}")
            if len(task.dependencies) != len(set(task.dependencies)):
                raise PlanValidationError(f"duplicate_dependency:{task.id}")
            if task.id in task.dependencies:
                raise PlanValidationError(f"self_dependency:{task.id}")
            missing = set(task.dependencies) - known_ids
            if missing:
                raise PlanValidationError(f"missing_dependency:{task.id}")
        self._assert_acyclic(plan.subtasks)

    @staticmethod
    def _assert_acyclic(tasks: Sequence[SubTask]) -> None:
        graph = {task.id: list(task.dependencies) for task in tasks}
        state: Dict[str, int] = {}

        def visit(task_id: str) -> None:
            marker = state.get(task_id, 0)
            if marker == 1:
                raise PlanValidationError("cyclic_dependency")
            if marker == 2:
                return
            state[task_id] = 1
            for dependency in graph[task_id]:
                visit(dependency)
            state[task_id] = 2

        for task_id in graph:
            visit(task_id)

    @classmethod
    def _split_clauses(cls, text: str) -> List[str]:
        clauses = [
            part.strip(" ，,。；;！!？?")
            for part in re.split(r"(?:，|,|。|；|;|！|!|？|\?|同时|以及|并且|而且|然后|再)", text)
        ]
        return [part for part in clauses if part]

    @classmethod
    def _relevant_clauses(cls, agent_type: str, clauses: Sequence[str]) -> List[str]:
        terms = cls._DOMAIN_TERMS.get(agent_type, ())
        matched = [clause for clause in clauses if any(term in clause.lower() for term in terms)]
        return matched[:2]

    @staticmethod
    def _sanitize_scope(text: str) -> str:
        """计划可观测字段只保留任务语义，遮盖常见敏感标识符。"""
        value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]", text)
        value = re.sub(r"(?<!\d)1\d{10}(?!\d)", "[REDACTED_PHONE]", value)
        value = re.sub(r"(?<!\d)\d{12,19}(?!\d)", "[REDACTED_NUMBER]", value)
        value = re.sub(
            r"(?i)(密码|验证码)\s*[:：=]?\s*[A-Za-z0-9_-]{4,}",
            r"\1=[REDACTED]",
            value,
        )
        return value

    @classmethod
    def _agent_position(cls, text: str, agent_type: str) -> int:
        positions = [text.lower().find(term) for term in cls._DOMAIN_TERMS.get(agent_type, ())]
        valid = [position for position in positions if position >= 0]
        return min(valid) if valid else len(text) + 1

    @classmethod
    def _infer_dependencies(
        cls,
        text: str,
        agent_types: Sequence[str],
        task_ids: Mapping[str, str],
    ) -> Dict[str, List[str]]:
        dependencies = {agent_type: [] for agent_type in agent_types}
        sequential = "先" in text and any(marker in text for marker in ("再", "然后", "根据", "之后"))
        if not sequential or len(agent_types) < 2:
            return dependencies
        ordered = sorted(agent_types, key=lambda agent: cls._agent_position(text, agent))
        for previous, current in zip(ordered, ordered[1:]):
            dependencies[current].append(task_ids[previous])
        return dependencies

    @staticmethod
    def _risk_level(agent_type: str, risk_reasons: Sequence[str]) -> str:
        if agent_type == "escalation":
            return "critical"
        if risk_reasons and agent_type in {"billing", "general"}:
            return "high"
        return "medium" if risk_reasons else "low"


TaskExecutor = Callable[[SubTask, Mapping[str, SubTaskResult]], Awaitable[SubTaskResult]]


class DAGScheduler:
    """按依赖分轮执行任务；同一轮无依赖任务并发执行。"""

    def __init__(self, planner: TaskPlanner, timeout_s: float = 30.0):
        self.planner = planner
        self.timeout_s = max(0.01, float(timeout_s))

    async def execute(self, plan: TaskPlan, executor: TaskExecutor) -> List[SubTaskResult]:
        self.planner.validate(plan)
        tasks = {task.id: task for task in plan.subtasks}
        pending = set(tasks)
        results: Dict[str, SubTaskResult] = {}
        rounds = 0

        while pending:
            rounds += 1
            if rounds > len(tasks) + 1:
                raise RuntimeError("scheduler_round_limit_exceeded")

            blocked = [
                task_id for task_id in pending
                if any(
                    dependency in results
                    and results[dependency].status != TaskStatus.SUCCESS
                    for dependency in tasks[task_id].dependencies
                )
            ]
            for task_id in blocked:
                task = tasks[task_id]
                results[task_id] = SubTaskResult(
                    task_id=task.id,
                    agent_type=task.agent_type,
                    success=False,
                    error="dependency_failed",
                    status=TaskStatus.BLOCKED,
                    dependencies=list(task.dependencies),
                )
                pending.remove(task_id)

            ready = [
                tasks[task_id] for task_id in plan_task_order(plan, pending)
                if all(
                    dependency in results
                    and results[dependency].status == TaskStatus.SUCCESS
                    for dependency in tasks[task_id].dependencies
                )
            ]
            if not ready and pending:
                raise RuntimeError("scheduler_deadlock")
            if not ready:
                continue

            async def run_one(task: SubTask) -> SubTaskResult:
                started = time.monotonic()
                dependency_results = {
                    dependency: results[dependency] for dependency in task.dependencies
                }
                try:
                    result = await asyncio.wait_for(
                        executor(task, dependency_results), timeout=self.timeout_s
                    )
                    if not isinstance(result, SubTaskResult):
                        raise TypeError("executor_result_invalid")
                    result.latency_ms = result.latency_ms or (time.monotonic() - started) * 1000
                    result.dependencies = list(task.dependencies)
                    if result.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
                        result.status = TaskStatus.SUCCESS if result.success else TaskStatus.FAILED
                    return result
                except asyncio.TimeoutError:
                    return SubTaskResult(
                        task_id=task.id,
                        agent_type=task.agent_type,
                        success=False,
                        latency_ms=(time.monotonic() - started) * 1000,
                        error="TimeoutError",
                        status=TaskStatus.TIMEOUT,
                        dependencies=list(task.dependencies),
                    )
                except Exception as ex:
                    return SubTaskResult(
                        task_id=task.id,
                        agent_type=task.agent_type,
                        success=False,
                        latency_ms=(time.monotonic() - started) * 1000,
                        error=type(ex).__name__,
                        status=TaskStatus.FAILED,
                        dependencies=list(task.dependencies),
                    )

            round_results = await asyncio.gather(*(run_one(task) for task in ready))
            for result in round_results:
                results[result.task_id] = result
                pending.remove(result.task_id)

        return [results[task.id] for task in plan.subtasks]


def plan_task_order(plan: TaskPlan, selected: Iterable[str]) -> List[str]:
    selected_set = set(selected)
    return [task.id for task in plan.subtasks if task.id in selected_set]


class ResponseSynthesizer:
    """确定性结果合成器：去重、保留失败信息、检测明显冲突。"""

    _HEADINGS = {
        "technical": "技术问题",
        "billing": "账单与支付",
        "general": "订单与通用事项",
        "escalation": "人工升级",
    }

    def synthesize(
        self,
        *,
        original_request: str,
        plan: TaskPlan,
        results: Sequence[SubTaskResult],
        force_escalation: bool = False,
    ) -> SynthesisResult:
        del original_request  # 合成只使用已验证的子任务结果，不再复述用户敏感原文。
        successful = [result for result in results if result.success and result.content.strip()]
        failed = [result for result in results if not result.success]
        if not successful:
            return SynthesisResult(
                content="抱歉，当前各专业处理环节均未能完成。请稍后重试；如涉及账户或资金安全，请转人工核实。",
                method="deterministic",
                escalated=True,
                degradations=["all_subtasks_failed"],
            )

        conflicts = self.detect_conflicts(successful)
        seen = set()
        sections = []
        for result in successful:
            normalized = self._normalize(result.content)
            if normalized in seen:
                continue
            seen.add(normalized)
            heading = self._HEADINGS.get(result.agent_type, result.agent_type)
            sections.append(f"【{heading}】\n{result.content.strip()}")

        if failed:
            labels = "、".join(
                self._HEADINGS.get(result.agent_type, result.agent_type) for result in failed
            )
            sections.append(f"【暂未完成】\n{labels}相关环节暂时不可用，未返回的部分需要稍后重试或人工核实。")

        escalated = force_escalation or bool(conflicts) or any(result.escalated for result in results)
        if conflicts:
            sections.append("【需要核实】\n不同处理结果存在数字或允许条件冲突，系统未自行选择结论，请转人工核实。")
        elif escalated:
            sections.append("【后续处理】\n该请求包含高风险或需人工确认的事项，建议转人工继续核实。")

        degradations = []
        if failed:
            degradations.append("partial_subtask_failure")
        return SynthesisResult(
            content="\n\n".join(sections),
            method="deterministic",
            escalated=escalated,
            conflicts=conflicts,
            degradations=degradations,
        )

    @classmethod
    def detect_conflicts(cls, results: Sequence[SubTaskResult]) -> List[str]:
        conflicts: List[str] = []
        for index, left in enumerate(results):
            for right in results[index + 1:]:
                if cls._polarity_conflict(left.content, right.content):
                    conflicts.append(f"polarity:{left.task_id}:{right.task_id}")
                elif cls._numeric_conflict(left.content, right.content):
                    conflicts.append(f"numeric:{left.task_id}:{right.task_id}")
        return conflicts

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", "", text).strip("。.!！")

    @staticmethod
    def _polarity_conflict(left: str, right: str) -> bool:
        positive = ("允许", "可以", "支持", "能够")
        negative = ("不允许", "不可以", "不支持", "不能")
        left_positive = any(term in left for term in positive) and not any(term in left for term in negative)
        right_positive = any(term in right for term in positive) and not any(term in right for term in negative)
        left_negative = any(term in left for term in negative)
        right_negative = any(term in right for term in negative)
        shared_topics = ("退款", "支付", "登录", "订单", "发票", "账户")
        shared = any(topic in left and topic in right for topic in shared_topics)
        return shared and ((left_positive and right_negative) or (right_positive and left_negative))

    @staticmethod
    def _numeric_conflict(left: str, right: str) -> bool:
        topics = ("退款", "到账", "有效期", "保修", "金额", "费用", "小时", "天")
        shared = any(topic in left and topic in right for topic in topics)
        if not shared:
            return False
        left_numbers = set(re.findall(r"\d+(?:\.\d+)?", left))
        right_numbers = set(re.findall(r"\d+(?:\.\d+)?", right))
        return bool(left_numbers and right_numbers and left_numbers != right_numbers)
