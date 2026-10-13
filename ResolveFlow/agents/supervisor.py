"""Validated DAG execution and deterministic response synthesis.

The orchestrator builds task plans directly from structured recognized intents.
This module only validates the DAG, executes tasks and synthesizes results.
It never reclassifies or re-splits the user's request.
"""
from __future__ import annotations

import asyncio
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
    tools_used: List[str] = field(default_factory=list)
    tool_traces: List[Dict[str, Any]] = field(default_factory=list)

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


TaskExecutor = Callable[[SubTask, Mapping[str, SubTaskResult]], Awaitable[SubTaskResult]]


class DAGScheduler:
    """Validate an explicit plan; run independent ready tasks concurrently."""

    def __init__(self, allowed_agent_types: Iterable[str], max_subtasks: int = 4,
                 timeout_s: float = 30.0):
        self.allowed_agent_types = set(allowed_agent_types)
        self.max_subtasks = max(1, int(max_subtasks))
        self.timeout_s = max(0.01, float(timeout_s))

    def validate(self, plan: TaskPlan) -> None:
        """Validate task IDs, agent permissions, dependency references and DAG cycles.

        This is a scheduling safety boundary, not a second semantic planner.
        """
        if not isinstance(plan, TaskPlan):
            raise PlanValidationError("plan_type_invalid")
        if not plan.subtasks:
            raise PlanValidationError("plan_empty")
        if len(plan.subtasks) > self.max_subtasks:
            raise PlanValidationError("too_many_subtasks")
        task_ids = [task.id for task in plan.subtasks]
        if any(not isinstance(task_id, str) or not task_id.strip() for task_id in task_ids):
            raise PlanValidationError("task_id_empty")
        if len(set(task_ids)) != len(task_ids):
            raise PlanValidationError("duplicate_task_id")
        known_ids = set(task_ids)
        for task in plan.subtasks:
            if task.agent_type not in self.allowed_agent_types:
                raise PlanValidationError(f"unknown_agent_type:{task.agent_type}")
            if not isinstance(task.description, str) or not task.description.strip():
                raise PlanValidationError(f"empty_description:{task.id}")
            if task.risk_level not in {"low", "medium", "high", "critical"}:
                raise PlanValidationError(f"invalid_risk_level:{task.id}")
            if len(task.dependencies) != len(set(task.dependencies)):
                raise PlanValidationError(f"duplicate_dependency:{task.id}")
            if task.id in task.dependencies:
                raise PlanValidationError(f"self_dependency:{task.id}")
            missing = set(task.dependencies) - known_ids
            if missing:
                raise PlanValidationError(f"missing_dependency:{task.id}")
        graph = {task.id: list(task.dependencies) for task in plan.subtasks}
        states: Dict[str, int] = {}

        def visit(task_id: str) -> None:
            state = states.get(task_id, 0)
            if state == 1:
                raise PlanValidationError("cyclic_dependency")
            if state == 2:
                return
            states[task_id] = 1
            for dep in graph[task_id]:
                visit(dep)
            states[task_id] = 2

        for task_id in graph:
            visit(task_id)

    async def execute(self, plan: TaskPlan, executor: TaskExecutor) -> List[SubTaskResult]:
        self.validate(plan)
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
        "action": "会员业务",
        "policy": "会员政策说明",
        "unsupported": "超出支持范围",
    }

    def synthesize(
        self,
        *,
        original_request: str,
        plan: TaskPlan,
        results: Sequence[SubTaskResult],
        extra_results: Sequence[SubTaskResult] = (),
        force_escalation: bool = False,
    ) -> SynthesisResult:
        """extra_results 是调用方在这次调度之外预先算好、已经验证过的结果（例如 Action
        层的执行结果或 RAG grounded 的政策回答）——它们参与最终内容拼接和 escalated 信号，
        但刻意不参与 detect_conflicts()：那是一个不区分语义的关键词/数字启发式，没有能力
        去仲裁一段已验证事实和一段未验证文本谁对谁错，让它们参与比较只会制造误报冲突。"""
        del original_request  # 合成只使用已验证的子任务结果，不再复述用户敏感原文。
        successful = [result for result in results if result.success and result.content.strip()]
        extra_successful = [result for result in extra_results if result.success and result.content.strip()]
        failed = [result for result in results if not result.success]
        if not successful and not extra_successful:
            return SynthesisResult(
                content="抱歉，当前各专业处理环节均未能完成。请稍后重试；如涉及账户或资金安全，请转人工核实。",
                method="deterministic",
                escalated=True,
                degradations=["all_subtasks_failed"],
            )

        conflicts = self.detect_conflicts(successful)
        seen = set()
        sections = []
        for result in [*extra_successful, *successful]:
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

        escalated = (force_escalation or bool(conflicts) or any(result.escalated for result in results)
                     or any(result.escalated for result in extra_results))
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
    def _split_sentences(text: str) -> List[str]:
        """Sentence-level split so conflict checks compare clauses that actually
        share a subject, not just a document that happens to mention the same
        keyword somewhere unrelated. Deliberately simple (no NLU, no external
        model call — this module stays testable without one by design)."""
        parts = re.split(r"[。！？；\n]|(?<=[a-zA-Z0-9])\.\s", text)
        return [part.strip() for part in parts if part.strip()]

    @classmethod
    def _polarity_conflict(cls, left: str, right: str) -> bool:
        positive = ("允许", "可以", "支持", "能够")
        negative = ("不允许", "不可以", "不支持", "不能")
        shared_topics = ("退款", "支付", "登录", "订单", "发票", "账户")

        def stance(sentence: str):
            is_positive = any(term in sentence for term in positive) and not any(term in sentence for term in negative)
            is_negative = any(term in sentence for term in negative)
            return is_positive, is_negative

        for left_sentence in cls._split_sentences(left):
            left_topics = [topic for topic in shared_topics if topic in left_sentence]
            left_positive, left_negative = stance(left_sentence)
            if not left_topics or not (left_positive or left_negative):
                continue
            for right_sentence in cls._split_sentences(right):
                if not any(topic in right_sentence for topic in left_topics):
                    continue
                right_positive, right_negative = stance(right_sentence)
                if (left_positive and right_negative) or (left_negative and right_positive):
                    return True
        return False

    @classmethod
    def _numeric_conflict(cls, left: str, right: str) -> bool:
        topics = ("退款", "到账", "有效期", "保修", "金额", "费用", "小时", "天")
        for left_sentence in cls._split_sentences(left):
            left_topics = [topic for topic in topics if topic in left_sentence]
            left_numbers = set(re.findall(r"\d+(?:\.\d+)?", left_sentence))
            if not left_topics or not left_numbers:
                continue
            for right_sentence in cls._split_sentences(right):
                if not any(topic in right_sentence for topic in left_topics):
                    continue
                right_numbers = set(re.findall(r"\d+(?:\.\d+)?", right_sentence))
                if right_numbers and right_numbers != left_numbers:
                    return True
        return False
