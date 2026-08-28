import asyncio
import json
import time
import unittest

from agents.agent_orchestrator import (
    AgentOrchestrator,
    AgentResponse,
    AgentType,
    Request,
    RoutingDecision,
)
from agents.supervisor import (
    DAGScheduler,
    PlanValidationError,
    ResponseSynthesizer,
    SubTask,
    SubTaskResult,
    TaskPlan,
    TaskPlanner,
    TaskStatus,
)
from core.intent_recognizer import IntentCategory, UrgencyLevel


def planner(max_subtasks=3):
    return TaskPlanner([agent.value for agent in AgentType], max_subtasks=max_subtasks)


def plan(*tasks):
    return TaskPlan(
        original_request="test request",
        subtasks=list(tasks),
        reason="test",
        confidence=1.0,
    )


def result(task_id, agent_type, content="ok", success=True, status=None):
    return SubTaskResult(
        task_id=task_id,
        agent_type=agent_type,
        success=success,
        content=content,
        status=status or (TaskStatus.SUCCESS if success else TaskStatus.FAILED),
    )


class PlannerTests(unittest.TestCase):
    def test_compound_request_gets_scoped_subtasks(self):
        task_plan = planner().create_plan(
            original_request="登录一直报错，而且银行卡被重复扣款",
            primary_agent="technical",
            supporting_agents=["billing"],
            reason="compound",
            confidence=0.9,
            risk_reasons=["payment_risk"],
        )
        self.assertEqual([task.agent_type for task in task_plan.subtasks], ["technical", "billing"])
        self.assertIn("登录", task_plan.subtasks[0].description)
        self.assertNotIn("重复扣款", task_plan.subtasks[0].description)
        self.assertIn("重复扣款", task_plan.subtasks[1].description)
        self.assertEqual(task_plan.subtasks[1].risk_level, "high")

    def test_sequential_language_creates_dependency(self):
        task_plan = planner().create_plan(
            original_request="先查询订单状态，然后根据查询结果判断退款资格",
            primary_agent="billing",
            supporting_agents=["general"],
            reason="dependent",
            confidence=1.0,
        )
        by_agent = {task.agent_type: task for task in task_plan.subtasks}
        self.assertEqual(by_agent["billing"].dependencies, [by_agent["general"].id])

    def test_invalid_json_is_rejected(self):
        with self.assertRaisesRegex(PlanValidationError, "planner_json_invalid"):
            planner().parse_json_plan("not-json", original_request="x")

    def test_unknown_agent_is_rejected(self):
        payload = {"subtasks": [{
            "id": "x", "description": "x", "agent_type": "unknown", "dependencies": []
        }]}
        with self.assertRaisesRegex(PlanValidationError, "unknown_agent_type"):
            planner().parse_json_plan(json.dumps(payload), original_request="x")

    def test_duplicate_ids_are_rejected(self):
        invalid = plan(
            SubTask("same", "one", "technical"),
            SubTask("same", "two", "billing"),
        )
        with self.assertRaisesRegex(PlanValidationError, "duplicate_task_id"):
            planner().validate(invalid)

    def test_missing_dependency_is_rejected(self):
        invalid = plan(SubTask("a", "one", "technical", ["missing"]))
        with self.assertRaisesRegex(PlanValidationError, "missing_dependency"):
            planner().validate(invalid)

    def test_cycle_is_rejected(self):
        invalid = plan(
            SubTask("a", "one", "technical", ["b"]),
            SubTask("b", "two", "billing", ["a"]),
        )
        with self.assertRaisesRegex(PlanValidationError, "cyclic_dependency"):
            planner().validate(invalid)

    def test_too_many_tasks_are_rejected(self):
        invalid = plan(
            SubTask("a", "one", "technical"),
            SubTask("b", "two", "billing"),
            SubTask("c", "three", "general"),
        )
        with self.assertRaisesRegex(PlanValidationError, "too_many_subtasks"):
            planner(max_subtasks=2).validate(invalid)

    def test_empty_description_is_rejected(self):
        with self.assertRaisesRegex(PlanValidationError, "empty_description"):
            planner().validate(plan(SubTask("a", "", "technical")))

    def test_plan_description_redacts_sensitive_values(self):
        task_plan = planner().create_plan(
            original_request="登录验证码 123456 失效，手机号13800138000也无法使用",
            primary_agent="technical",
            supporting_agents=[],
            reason="sensitive",
            confidence=1.0,
        )
        description = task_plan.subtasks[0].description
        self.assertNotIn("123456", description)
        self.assertNotIn("13800138000", description)
        self.assertIn("REDACTED", description)


class SchedulerTests(unittest.TestCase):
    def test_independent_tasks_really_overlap(self):
        async def run():
            scheduler = DAGScheduler(planner(), timeout_s=0.5)
            task_plan = plan(
                SubTask("a", "one", "technical"),
                SubTask("b", "two", "billing"),
            )
            both_started = asyncio.Event()
            started = 0
            lock = asyncio.Lock()

            async def executor(task, dependencies):
                nonlocal started
                async with lock:
                    started += 1
                    if started == 2:
                        both_started.set()
                await asyncio.wait_for(both_started.wait(), timeout=0.2)
                return result(task.id, task.agent_type)

            return await scheduler.execute(task_plan, executor)

        rows = asyncio.run(run())
        self.assertTrue(all(row.success for row in rows))

    def test_dependencies_execute_in_order(self):
        async def run():
            scheduler = DAGScheduler(planner(), timeout_s=0.5)
            task_plan = plan(
                SubTask("first", "one", "general"),
                SubTask("second", "two", "billing", ["first"]),
            )
            order = []

            async def executor(task, dependencies):
                order.append((task.id, sorted(dependencies)))
                return result(task.id, task.agent_type)

            rows = await scheduler.execute(task_plan, executor)
            return rows, order

        rows, order = asyncio.run(run())
        self.assertEqual(order, [("first", []), ("second", ["first"])])
        self.assertTrue(all(row.success for row in rows))

    def test_one_failure_does_not_cancel_independent_task(self):
        async def executor(task, dependencies):
            return result(task.id, task.agent_type, success=task.id != "bad")

        rows = asyncio.run(DAGScheduler(planner()).execute(
            plan(SubTask("bad", "one", "technical"), SubTask("good", "two", "billing")),
            executor,
        ))
        self.assertEqual([row.status for row in rows], [TaskStatus.FAILED, TaskStatus.SUCCESS])

    def test_dependency_failure_blocks_downstream(self):
        called = []

        async def executor(task, dependencies):
            called.append(task.id)
            return result(task.id, task.agent_type, success=False)

        rows = asyncio.run(DAGScheduler(planner()).execute(
            plan(SubTask("first", "one", "general"), SubTask("second", "two", "billing", ["first"])),
            executor,
        ))
        self.assertEqual(called, ["first"])
        self.assertEqual(rows[1].status, TaskStatus.BLOCKED)

    def test_task_timeout_is_observable(self):
        async def executor(task, dependencies):
            await asyncio.sleep(0.05)
            return result(task.id, task.agent_type)

        rows = asyncio.run(DAGScheduler(planner(), timeout_s=0.01).execute(
            plan(SubTask("slow", "one", "technical")), executor
        ))
        self.assertEqual(rows[0].status, TaskStatus.TIMEOUT)
        self.assertEqual(rows[0].error, "TimeoutError")


class SynthesizerTests(unittest.TestCase):
    def test_duplicate_agent_content_is_removed(self):
        synthesis = ResponseSynthesizer().synthesize(
            original_request="x",
            plan=plan(SubTask("a", "one", "technical"), SubTask("b", "two", "billing")),
            results=[
                result("a", "technical", "请先核实当前状态。"),
                result("b", "billing", "请先核实当前状态。"),
            ],
        )
        self.assertEqual(synthesis.content.count("请先核实当前状态"), 1)

    def test_polarity_conflict_escalates(self):
        synthesis = ResponseSynthesizer().synthesize(
            original_request="x",
            plan=plan(SubTask("a", "one", "general"), SubTask("b", "two", "billing")),
            results=[
                result("a", "general", "该订单允许退款。"),
                result("b", "billing", "该订单不允许退款。"),
            ],
        )
        self.assertTrue(synthesis.escalated)
        self.assertTrue(any(item.startswith("polarity:") for item in synthesis.conflicts))
        self.assertIn("需要核实", synthesis.content)

    def test_numeric_conflict_escalates(self):
        synthesis = ResponseSynthesizer().synthesize(
            original_request="x",
            plan=plan(SubTask("a", "one", "general"), SubTask("b", "two", "billing")),
            results=[
                result("a", "general", "退款需要7天到账。"),
                result("b", "billing", "退款需要70天到账。"),
            ],
        )
        self.assertTrue(synthesis.escalated)
        self.assertTrue(any(item.startswith("numeric:") for item in synthesis.conflicts))

    def test_partial_failure_keeps_successful_evidence(self):
        synthesis = ResponseSynthesizer().synthesize(
            original_request="x",
            plan=plan(SubTask("a", "one", "technical"), SubTask("b", "two", "billing")),
            results=[
                result("a", "technical", "技术处理成功"),
                result("b", "billing", success=False),
            ],
        )
        self.assertIn("技术处理成功", synthesis.content)
        self.assertIn("暂未完成", synthesis.content)
        self.assertIn("partial_subtask_failure", synthesis.degradations)

    def test_all_failures_use_safe_escalation(self):
        synthesis = ResponseSynthesizer().synthesize(
            original_request="x",
            plan=plan(SubTask("a", "one", "technical")),
            results=[result("a", "technical", success=False)],
        )
        self.assertTrue(synthesis.escalated)
        self.assertIn("all_subtasks_failed", synthesis.degradations)


class _Stats:
    @staticmethod
    def routing_score():
        return 1.0


class _FakeAgent:
    def __init__(self, agent_type):
        self.agent_type = agent_type
        self.stats = _Stats()
        self.requests = []

    async def handle(self, request):
        self.requests.append(request)
        return AgentResponse(
            agent_type=self.agent_type,
            content=f"handled:{request.task_instruction}",
            success=True,
            latency_ms=1.0,
        )


class OrchestratorIntegrationTests(unittest.TestCase):
    @staticmethod
    def orchestrator():
        orchestrator = object.__new__(AgentOrchestrator)
        orchestrator._pool = {
            agent_type: [_FakeAgent(agent_type)] for agent_type in AgentType
        }
        orchestrator._planner = planner()
        orchestrator._scheduler = DAGScheduler(orchestrator._planner, timeout_s=0.5)
        orchestrator._synthesizer = ResponseSynthesizer()
        return orchestrator

    @staticmethod
    def request():
        return Request(
            message="登录报错并且需要核查退款政策",
            user_id="u",
            conv_id="c",
            intent=IntentCategory.TECHNICAL_LOGIN,
            intent_group="technical",
            urgency=UrgencyLevel.MEDIUM,
            intent_confidence=0.9,
        )

    def test_agents_receive_different_scoped_tasks(self):
        orchestrator = self.orchestrator()
        decision = RoutingDecision(
            primary_agent=AgentType.TECHNICAL,
            supporting_agents=[AgentType.BILLING],
            reason="compound",
            confidence=0.9,
        )
        output = asyncio.run(orchestrator.run_parallel(self.request(), decision))
        technical_request = orchestrator._pool[AgentType.TECHNICAL][0].requests[0]
        billing_request = orchestrator._pool[AgentType.BILLING][0].requests[0]
        self.assertNotEqual(technical_request.task_instruction, billing_request.task_instruction)
        self.assertIn("技术", technical_request.task_instruction)
        self.assertIn("账单", billing_request.task_instruction)
        self.assertEqual(len(output.execution_trace), 2)
        self.assertEqual(output.synthesis_method, "deterministic")
        self.assertIsNotNone(output.task_plan)

    def test_synthesizer_failure_falls_back_without_losing_results(self):
        class BrokenSynthesizer:
            @staticmethod
            def synthesize(**kwargs):
                raise RuntimeError("down")

        orchestrator = self.orchestrator()
        orchestrator._synthesizer = BrokenSynthesizer()
        decision = RoutingDecision(
            primary_agent=AgentType.TECHNICAL,
            supporting_agents=[AgentType.BILLING],
            reason="compound",
            confidence=0.9,
        )
        output = asyncio.run(orchestrator.run_parallel(self.request(), decision))
        self.assertEqual(output.synthesis_method, "fallback")
        self.assertIn("response_synthesizer_unavailable", output.degradations)
        self.assertIn("handled:", output.response)

    def test_planner_failure_uses_primary_agent_fallback_plan(self):
        base_planner = planner()

        class BrokenPlanner:
            @staticmethod
            def create_plan(**kwargs):
                raise RuntimeError("down")

            @staticmethod
            def validate(task_plan):
                return base_planner.validate(task_plan)

        orchestrator = self.orchestrator()
        orchestrator._planner = BrokenPlanner()
        orchestrator._scheduler = DAGScheduler(orchestrator._planner, timeout_s=0.5)
        decision = RoutingDecision(
            primary_agent=AgentType.TECHNICAL,
            supporting_agents=[AgentType.BILLING],
            reason="compound",
            confidence=0.9,
        )
        output = asyncio.run(orchestrator.run_parallel(self.request(), decision))
        self.assertIn("task_planner_unavailable", output.degradations)
        self.assertEqual(output.task_plan["method"], "fallback")
        self.assertEqual(len(output.execution_trace), 1)
        self.assertEqual(output.execution_trace[0]["agent_type"], "technical")

    def test_single_agent_fast_path_has_no_task_plan(self):
        orchestrator = self.orchestrator()
        request = Request(
            message="你好",
            user_id="u",
            conv_id="c",
            intent=IntentCategory.GREETING,
            intent_group="general",
            urgency=UrgencyLevel.LOW,
            intent_confidence=1.0,
        )
        output = asyncio.run(orchestrator.run(request))
        self.assertIsNone(output.task_plan)
        self.assertEqual(output.synthesis_method, "single_agent")
        self.assertEqual(len(orchestrator._pool[AgentType.GENERAL][0].requests), 1)


if __name__ == "__main__":
    unittest.main()
