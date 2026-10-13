import asyncio
import time
import unittest

from agents.agent_orchestrator import (
    AgentOrchestrator,
    AgentResponse,
    AgentType,
    Request,
    RoutingDecision,
    TechnicalAgent,
)
from agents.supervisor import (
    DAGScheduler,
    PlanValidationError,
    ResponseSynthesizer,
    SubTask,
    SubTaskResult,
    TaskPlan,
    TaskStatus,
)
from core.intent_recognizer import IntentCategory, RecognizedIntent, UrgencyLevel


def make_scheduler(max_subtasks=4, timeout_s=30.0):
    return DAGScheduler(
        (agent.value for agent in AgentType),
        max_subtasks=max_subtasks,
        timeout_s=timeout_s,
    )


def built_plan(message, intents, primary, supporting=(), risk_reasons=()):
    """Exercise the real lightweight plan builder, without a second Planner."""
    orch = object.__new__(AgentOrchestrator)
    orch._pool = {agent: [object()] for agent in AgentType}
    orch._max_subtasks = 4
    orch._scheduler = make_scheduler()
    req = Request(
        message=message, user_id="test", conv_id="demo",
        intent=intents[0].intent, intent_group="",
        intent_confidence=intents[0].confidence,
        urgency=UrgencyLevel.LOW, intents=list(intents),
    )
    return orch._build_task_plan(
        req,
        RoutingDecision(primary_agent=primary, supporting_agents=list(supporting),
                        reason="unit_test", confidence=0.9),
        list(risk_reasons),
    )


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


class TaskPlanBuilderTests(unittest.TestCase):
    def test_multi_intent_scopes_are_grounded(self):
        text = "登录一直报错，而且银行卡被重复扣款"
        task_plan = built_plan(
            message=text,
            intents=[
                RecognizedIntent(IntentCategory.TECHNICAL_LOGIN, "登录一直报错", 0.95),
                RecognizedIntent(IntentCategory.PAYMENT_ISSUE, "银行卡被重复扣款", 0.93),
            ],
            primary=AgentType.TECHNICAL,
            supporting=[AgentType.BILLING],
            risk_reasons=["payment_risk"],
        )
        self.assertEqual([task.agent_type for task in task_plan.subtasks], ["technical", "billing"])
        self.assertIn("登录一直报错", task_plan.subtasks[0].description)
        self.assertNotIn("重复扣款", task_plan.subtasks[0].description)
        self.assertIn("银行卡被重复扣款", task_plan.subtasks[1].description)
        self.assertEqual(task_plan.subtasks[1].risk_level, "high")

    def test_structured_dependency_is_retained(self):
        text = "先查询订单状态，然后根据查询结果判断退款资格"
        task_plan = built_plan(
            message=text,
            intents=[
                RecognizedIntent(IntentCategory.ORDER_STATUS, "查询订单状态", 0.95),
                RecognizedIntent(IntentCategory.REFUND, "根据查询结果判断退款资格", 0.92, [0]),
            ],
            primary=AgentType.GENERAL,
            supporting=[AgentType.BILLING],
        )
        by_agent = {task.agent_type: task for task in task_plan.subtasks}
        self.assertEqual(by_agent["billing"].dependencies, [by_agent["general"].id])

    def test_ordering_words_do_not_force_data_dependency(self):
        text = "先排查登录问题，然后查物流"
        task_plan = built_plan(
            message=text,
            intents=[
                RecognizedIntent(IntentCategory.TECHNICAL_LOGIN, "排查登录问题", 0.94),
                RecognizedIntent(IntentCategory.LOGISTICS, "查物流", 0.95),
            ],
            primary=AgentType.TECHNICAL,
            supporting=[AgentType.GENERAL],
        )
        self.assertTrue(all(not t.dependencies for t in task_plan.subtasks))

    def test_unknown_agent_is_rejected(self):
        with self.assertRaisesRegex(PlanValidationError, "unknown_agent_type"):
            make_scheduler().validate(plan(SubTask("x", "one", "unknown")))

    def test_duplicate_ids_are_rejected(self):
        invalid = plan(SubTask("same", "one", "technical"), SubTask("same", "two", "billing"))
        with self.assertRaisesRegex(PlanValidationError, "duplicate_task_id"):
            make_scheduler().validate(invalid)

    def test_missing_dependency_is_rejected(self):
        invalid = plan(SubTask("a", "one", "technical", ["missing"]))
        with self.assertRaisesRegex(PlanValidationError, "missing_dependency"):
            make_scheduler().validate(invalid)

    def test_cycle_is_rejected(self):
        invalid = plan(
            SubTask("a", "one", "technical", ["b"]),
            SubTask("b", "two", "billing", ["a"]),
        )
        with self.assertRaisesRegex(PlanValidationError, "cyclic_dependency"):
            make_scheduler().validate(invalid)

    def test_too_many_tasks_are_rejected(self):
        invalid = plan(
            SubTask("a", "one", "technical"),
            SubTask("b", "two", "billing"),
            SubTask("c", "three", "general"),
        )
        with self.assertRaisesRegex(PlanValidationError, "too_many_subtasks"):
            make_scheduler(max_subtasks=2).validate(invalid)

    def test_empty_description_is_rejected(self):
        with self.assertRaisesRegex(PlanValidationError, "empty_description"):
            make_scheduler().validate(plan(SubTask("a", "", "technical")))

    def test_plan_scope_redacts_sensitive_values(self):
        text = "登录验证码 123456 失效，手机号13800138000也无法使用"
        task_plan = built_plan(
            message=text,
            intents=[RecognizedIntent(IntentCategory.TECHNICAL_LOGIN, text, 0.93)],
            primary=AgentType.TECHNICAL,
        )
        description = task_plan.subtasks[0].description
        self.assertNotIn("123456", description)
        self.assertNotIn("13800138000", description)
        self.assertIn("REDACTED", description)



class AgentPromptScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_agent_does_not_repeat_other_business_domains(self):
        class Recorder:
            def __init__(self):
                self.calls = []

            async def create(self, **kwargs):
                self.calls.append(kwargs)
                return "排查登录故障即可"

        client = Recorder()
        agent = TechnicalAgent(client=client, model="test")
        req = Request(
            message="我的登录故障还没解决，而且我的银行卡被重复扣款了",
            user_id="u", conv_id="c",
            task_instruction="仅排查登录故障，不讨论付款",
        )
        response = await agent.handle(req)
        self.assertTrue(response.success)
        text = "\n".join(
            str(part.get("content", "")) for part in client.calls[0]["messages"]
        )
        self.assertIn("仅排查登录故障", text)
        self.assertNotIn("银行卡被重复扣款", text)


class SchedulerTests(unittest.TestCase):
    def test_independent_tasks_really_overlap(self):
        async def run():
            scheduler = make_scheduler(timeout_s=0.5)
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
            scheduler = make_scheduler(timeout_s=0.5)
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

        rows = asyncio.run(make_scheduler().execute(
            plan(SubTask("bad", "one", "technical"), SubTask("good", "two", "billing")),
            executor,
        ))
        self.assertEqual([row.status for row in rows], [TaskStatus.FAILED, TaskStatus.SUCCESS])

    def test_dependency_failure_blocks_downstream(self):
        called = []

        async def executor(task, dependencies):
            called.append(task.id)
            return result(task.id, task.agent_type, success=False)

        rows = asyncio.run(make_scheduler().execute(
            plan(SubTask("first", "one", "general"), SubTask("second", "two", "billing", ["first"])),
            executor,
        ))
        self.assertEqual(called, ["first"])
        self.assertEqual(rows[1].status, TaskStatus.BLOCKED)

    def test_task_timeout_is_observable(self):
        async def executor(task, dependencies):
            await asyncio.sleep(0.05)
            return result(task.id, task.agent_type)

        rows = asyncio.run(make_scheduler(timeout_s=0.01).execute(
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

    def test_shared_keyword_in_unrelated_sentences_is_not_a_conflict(self):
        # Neither side ever has the topic word "退款" and a number in the SAME
        # clause — the old whole-text keyword scan still flagged this as a numeric
        # conflict (7 vs 1 anywhere in text that also mentions 退款 anywhere); the
        # sentence-scoped check correctly finds no valid clause to compare.
        synthesis = ResponseSynthesizer().synthesize(
            original_request="x",
            plan=plan(SubTask("a", "one", "general"), SubTask("b", "two", "billing")),
            results=[
                result("a", "general", "商品退货预计需要7个工作日处理。与退款流程无关。"),
                result("b", "billing", "账单确认无误，无需退款；另有一项登录记录，耗时预计1小时。"),
            ],
        )
        self.assertEqual(synthesis.conflicts, [])
        self.assertFalse(synthesis.escalated)

    def test_same_clause_numeric_conflict_still_escalates(self):
        synthesis = ResponseSynthesizer().synthesize(
            original_request="x",
            plan=plan(SubTask("a", "one", "general"), SubTask("b", "two", "billing")),
            results=[
                result("a", "general", "背景说明与本次无关。退款预计7天到账。"),
                result("b", "billing", "另有一句不相关内容。退款预计70天到账。"),
            ],
        )
        self.assertTrue(any(item.startswith("numeric:") for item in synthesis.conflicts))

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

    def test_extra_results_are_not_compared_for_conflicts(self):
        # A pre-verified action/policy result may share a keyword ("退款") with an
        # unrelated remainder subtask and carry different numbers — that coincidence
        # must not trigger detect_conflicts(), which cannot tell the two apart from
        # a real contradiction about the same fact.
        synthesis = ResponseSynthesizer().synthesize(
            original_request="x",
            plan=plan(SubTask("a", "one", "general")),
            results=[result("a", "general", "退款政策：符合条件的商品可在7天内退款。")],
            extra_results=[result("action_primary", "action", "本次核实到 2 笔重复扣款，已发起退款申请。")],
        )
        self.assertEqual(synthesis.conflicts, [])
        self.assertFalse(synthesis.escalated)
        self.assertIn("会员业务", synthesis.content)
        self.assertIn("订单与通用事项", synthesis.content)

    def test_extra_results_escalation_signal_is_preserved(self):
        extra = result("action_primary", "action", "已提交人工审批。")
        extra.escalated = True
        synthesis = ResponseSynthesizer().synthesize(
            original_request="x",
            plan=plan(SubTask("a", "one", "general")),
            results=[result("a", "general", "物流预计 3 天送达。")],
            extra_results=[extra],
        )
        self.assertTrue(synthesis.escalated)

    def test_extra_results_alone_are_not_treated_as_all_failed(self):
        synthesis = ResponseSynthesizer().synthesize(
            original_request="x",
            plan=plan(SubTask("a", "one", "general")),
            results=[result("a", "general", success=False)],
            extra_results=[result("action_primary", "action", "已完成核实。")],
        )
        self.assertIn("已完成核实", synthesis.content)
        self.assertIn("暂未完成", synthesis.content)
        self.assertNotIn("all_subtasks_failed", synthesis.degradations)


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
        orchestrator._max_subtasks = 4
        orchestrator._scheduler = make_scheduler(timeout_s=0.5)
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

    def test_invalid_task_plan_uses_primary_agent_fallback(self):
        orchestrator = self.orchestrator()

        def broken_task_builder(*args, **kwargs):
            raise RuntimeError("broken task scope")

        orchestrator._build_task_plan = broken_task_builder
        decision = RoutingDecision(
            primary_agent=AgentType.TECHNICAL,
            supporting_agents=[AgentType.BILLING],
            reason="compound",
            confidence=0.9,
        )
        output = asyncio.run(orchestrator.run_parallel(self.request(), decision))
        self.assertIn("task_plan_unavailable", output.degradations)
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

    @staticmethod
    def _remainder_request():
        return Request(
            message="帮我查一下物流",
            user_id="u",
            conv_id="c",
            intent=IntentCategory.LOGISTICS,
            intent_group="general",
            urgency=UrgencyLevel.LOW,
            intent_confidence=0.9,
        )

    def test_run_compound_includes_both_primary_and_remainder_sections(self):
        orchestrator = self.orchestrator()
        primary = result("action_primary", "action", "已核实 2 笔重复扣款，已发起退款申请。")
        output = asyncio.run(orchestrator.run_compound(self._remainder_request(), primary))
        self.assertIn("会员业务", output.response)
        self.assertIn("已核实 2 笔重复扣款", output.response)
        self.assertIn("handled:", output.response)
        self.assertFalse(output.escalated)
        self.assertEqual(output.execution_trace[0]["task_id"], "action_primary")
        self.assertIsNotNone(output.task_plan)

    def test_run_compound_preserves_primary_escalation_signal(self):
        orchestrator = self.orchestrator()
        primary = result("action_primary", "action", "已提交人工审批。")
        primary.escalated = True
        output = asyncio.run(orchestrator.run_compound(self._remainder_request(), primary))
        self.assertTrue(output.escalated)

    def test_run_compound_does_not_assign_action_as_consultation_agent(self):
        # "action"/"policy" must never need to be registered as an AgentType — they
        # bypass consultation DAG entirely via extra_results, so the remainder
        # plan produced here should only ever contain real AgentType subtasks.
        orchestrator = self.orchestrator()
        primary = result("policy_primary", "policy", "会员费退款政策说明。")
        output = asyncio.run(orchestrator.run_compound(self._remainder_request(), primary))
        plan_agent_types = {task["agent_type"] for task in output.task_plan["subtasks"]}
        self.assertTrue(plan_agent_types.issubset({agent.value for agent in AgentType}))


if __name__ == "__main__":
    unittest.main()
