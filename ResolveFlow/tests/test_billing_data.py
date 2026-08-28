import unittest
from pathlib import Path

from evaluation.baseline import meets_final_thresholds
from evaluation.dataset_loader import load_billing_profile, load_multi_agent_profile, stratified_limit, validate_data_assets, validate_records
from evaluation.rag_evaluator import evaluate_retrieval
from evaluation.rules import evaluate_hard_rules
from scripts.generate_multi_agent_data import policies, rag_queries
from scripts.metrics_report import build_metrics_report


ROOT = Path(__file__).resolve().parents[1]


class BillingDataTests(unittest.TestCase):
    @staticmethod
    def _routing_only_orchestrator():
        from agents.agent_orchestrator import AgentOrchestrator, AgentType

        orchestrator = object.__new__(AgentOrchestrator)
        orchestrator._pool = {
            AgentType.GENERAL: [object()],
            AgentType.TECHNICAL: [object()],
            AgentType.BILLING: [object()],
            AgentType.ESCALATION: [object()],
        }
        return orchestrator

    def test_billing_profile_is_valid_and_complete(self):
        intents, dialogs = load_billing_profile(ROOT / "data")
        self.assertEqual(len(intents), 268)
        self.assertEqual(len(dialogs), 40)
        self.assertEqual(validate_records(intents, "intent"), [])
        self.assertEqual(validate_records(dialogs, "dialog"), [])
        self.assertTrue(all(item["review_status"] == "pending_human_review" for item in intents + dialogs))

    def test_multi_agent_profile_and_splits_are_valid(self):
        all_intents, all_dialogs = load_multi_agent_profile(ROOT / "data")
        dev_intents, dev_dialogs = load_multi_agent_profile(ROOT / "data", split="dev")
        holdout_intents, holdout_dialogs = load_multi_agent_profile(ROOT / "data", split="holdout")
        self.assertEqual(len(all_intents), 698)
        self.assertEqual(len(all_dialogs), 170)
        self.assertEqual(len(dev_intents) + len(holdout_intents), len(all_intents))
        self.assertEqual(len(dev_dialogs) + len(holdout_dialogs), len(all_dialogs))
        self.assertEqual(validate_records(all_intents, "intent"), [])
        self.assertEqual(validate_records(all_dialogs, "dialog"), [])
        self.assertEqual(validate_data_assets(ROOT / "data"), [])
        smoke = stratified_limit(dev_intents, "expected_intent", 2)
        self.assertTrue(all(sum(1 for item in smoke if item["expected_intent"] == label) <= 2 for label in {item["expected_intent"] for item in smoke}))

    def test_hard_rules_reject_prohibited_commitments(self):
        result = evaluate_hard_rules(
            {
                "expected_primary_agent": "billing",
                "expected_supporting_agents": [],
                "should_escalate": True,
                "should_use_knowledge": True,
                "entities": {},
                "must_include": ["人工"],
                "must_not_include": ["保证退款"],
            },
            "我保证退款，而且立刻到账。请转人工。",
            "billing",
            ["billing"],
            True,
            {},
            True,
            False,
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["scores"]["must_not_include_violation_rate"], 1.0)

    def test_hard_rules_accept_a_compliant_escalation(self):
        result = evaluate_hard_rules(
            {
                "expected_primary_agent": "billing",
                "expected_supporting_agents": [],
                "should_escalate": True,
                "should_use_knowledge": True,
                "entities": {},
                "must_include": ["人工"],
                "must_not_include": ["保证退款"],
            },
            "我会为您升级人工审核，并以实际处理结果为准。",
            "billing",
            ["billing"],
            True,
            {},
            True,
            False,
        )
        self.assertTrue(result["passed"])

    def test_hard_rules_check_supporting_agents(self):
        result = evaluate_hard_rules(
            {
                "expected_primary_agent": "technical",
                "expected_supporting_agents": ["billing"],
                "should_escalate": False,
                "should_use_knowledge": True,
                "entities": {},
                "must_include": [],
                "must_not_include": [],
            },
            "我会同时检查技术问题和账单状态。",
            "technical",
            ["technical", "billing"],
            False,
            {},
            True,
            False,
        )
        self.assertTrue(result["passed"])
        self.assertEqual(result["scores"]["cross_agent_coverage"], 1.0)

    def test_rag_evaluator_uses_document_ids(self):
        async def search(query, top_k):
            return [{"document_id": "demo-refund-eligibility"}]

        import asyncio
        report = asyncio.run(evaluate_retrieval(
            [{"id": "q1", "query": "退款资格", "expected_document_id": "demo-refund-eligibility", "top_k": 3}],
            search,
        ))
        self.assertEqual(report["top_k_hit_rate"], 1.0)
        self.assertEqual(report["top_1_hit_rate"], 1.0)
        self.assertEqual(report["mrr"], 1.0)

    def test_rag_evaluator_calculates_top_k_and_mrr(self):
        async def search(query, top_k):
            return [
                {"document_id": "wrong-a"},
                {"document_id": "demo-refund-eligibility"},
                {"document_id": "wrong-b"},
            ][:top_k]

        import asyncio
        report = asyncio.run(evaluate_retrieval(
            [{"id": "q1", "query": "退款资格", "expected_document_id": "demo-refund-eligibility", "top_k": 3}],
            search,
        ))
        self.assertEqual(report["top_1_hit_rate"], 0.0)
        self.assertEqual(report["top_3_hit_rate"], 1.0)
        self.assertEqual(report["mrr"], 0.5)
        self.assertIn("p95_latency_ms", report)

    def test_rag_queries_are_natural_and_complete(self):
        documents = policies()
        queries = rag_queries(documents)
        self.assertEqual(len(queries), 140)
        titles = {doc["id"]: doc["title"] for rows in documents.values() for doc in rows}
        self.assertTrue(all(titles[item["expected_document_id"]] not in item["query"] for item in queries))

    def test_baseline_promotion_requires_all_final_metrics(self):
        incomplete = {
            "intent_accuracy": 1.0,
            "routing_accuracy": 1.0,
            "must_not_include_violation_rate": 0.0,
        }
        self.assertFalse(meets_final_thresholds(incomplete))

        complete = {
            "intent_accuracy": 0.90,
            "routing_accuracy": 0.90,
            "knowledge_expectation_accuracy": 0.80,
            "memory_consistency": 0.80,
            "relevance": 0.75,
            "accuracy": 0.75,
            "completeness": 0.75,
            "helpfulness": 0.75,
            "escalation_recall": 0.85,
            "supporting_agent_accuracy": 0.85,
            "cross_agent_coverage": 0.85,
            "must_not_include_violation_rate": 0.0,
        }
        self.assertTrue(meets_final_thresholds(complete))

    def test_high_risk_payment_escalates_without_stealing_primary_route(self):
        from agents.agent_orchestrator import AgentType, Request
        from core.intent_recognizer import IntentCategory, UrgencyLevel

        orchestrator = self._routing_only_orchestrator()
        req = Request(
            message="有笔不认识的扣款，必须人工核实真实状态",
            user_id="u1",
            conv_id="c1",
            intent=IntentCategory.PAYMENT_ISSUE,
            intent_group="billing",
            urgency=UrgencyLevel.MEDIUM,
            intent_confidence=0.9,
        )
        decision = orchestrator._route_decision(req)
        self.assertEqual(decision.primary_agent, AgentType.BILLING)
        self.assertTrue(orchestrator._requires_high_risk_escalation(req))

    def test_account_security_keeps_billing_primary_and_escalates(self):
        from agents.agent_orchestrator import AgentType, Request
        from core.intent_recognizer import IntentCategory, UrgencyLevel

        orchestrator = self._routing_only_orchestrator()
        req = Request(
            message="账号可能被盗用，请不要向我索要密码或验证码",
            user_id="u1",
            conv_id="c1",
            intent=IntentCategory.ACCOUNT_SECURITY,
            intent_group="account",
            urgency=UrgencyLevel.HIGH,
            intent_confidence=0.9,
        )
        decision = orchestrator._route_decision(req)
        self.assertEqual(decision.primary_agent, AgentType.BILLING)
        self.assertTrue(orchestrator._requires_high_risk_escalation(req))
        self.assertIn("signals=", decision.reason)

    def test_cross_agent_problem_gets_supporting_agent(self):
        from agents.agent_orchestrator import AgentType, Request
        from core.intent_recognizer import IntentCategory, UrgencyLevel

        orchestrator = self._routing_only_orchestrator()
        req = Request(
            message="登录失败后又发现账单重复扣款，两个问题需要一起处理",
            user_id="u1",
            conv_id="c1",
            intent=IntentCategory.PAYMENT_ISSUE,
            intent_group="billing",
            urgency=UrgencyLevel.HIGH,
            intent_confidence=0.9,
        )
        decision = orchestrator._route_decision(req)
        self.assertEqual(decision.primary_agent, AgentType.BILLING)
        self.assertIn(AgentType.TECHNICAL, decision.supporting_agents)

    def test_escalation_inquiry_does_not_steal_technical_route(self):
        from agents.agent_orchestrator import AgentType, Request
        from core.intent_recognizer import IntentCategory, UrgencyLevel

        orchestrator = self._routing_only_orchestrator()
        req = Request(
            message="页面报 500，这种情况是否需要升级处理？",
            user_id="u1",
            conv_id="c1",
            intent=IntentCategory.TECHNICAL_CRASH,
            intent_group="technical",
            urgency=UrgencyLevel.MEDIUM,
        )
        decision = orchestrator._route_decision(req)
        self.assertEqual(decision.primary_agent, AgentType.TECHNICAL)
        self.assertFalse(orchestrator._requires_high_risk_escalation(req))

    def test_knowledge_base_lexical_score_prefers_business_term_hits(self):
        from mcp.knowledge_base import KnowledgeBase

        matched = KnowledgeBase._lexical_score(
            "为什么出现重复扣款",
            "支付失败与重复扣款处理流程",
            {"title": "支付政策", "search_terms": '["重复扣款", "支付"]'},
        )
        unrelated = KnowledgeBase._lexical_score(
            "为什么出现重复扣款",
            "物流配送轨迹查询流程",
            {"title": "物流政策", "search_terms": '["物流", "快递"]'},
        )
        self.assertGreater(matched, unrelated)

    def test_metrics_report_contains_required_fields(self):
        eval_report = {
            "total": 2,
            "pass_rate": 1.0,
            "avg_scores": {
                "routing_accuracy": 1.0,
                "supporting_agent_accuracy": 1.0,
                "cross_agent_coverage": 1.0,
                "escalation_recall": 1.0,
                "knowledge_expectation_accuracy": 1.0,
                "must_not_include_violation_rate": 0.0,
            },
            "results": [
                {
                    "test_id": "intent_recognition",
                    "scores": {"accuracy": 0.95, "macro_f1": 0.93},
                    "metadata": {"total": 20, "correct": 19},
                },
                {"test_id": "dialog_0", "scores": {}, "metadata": {}},
            ],
        }
        rag_report = {
            "total": 10,
            "top_1_hit_rate": 0.7,
            "top_3_hit_rate": 0.9,
            "top_5_hit_rate": 1.0,
            "mrr": 0.82,
            "avg_latency_ms": 12.3,
            "p95_latency_ms": 18.7,
        }
        routing_report = {
            "total": 25,
            "split": "holdout",
            "evaluation_mode": "gold_intent_rule_only",
            "routing_accuracy": 0.96,
            "supporting_agent_accuracy": 0.92,
            "cross_agent_coverage": 0.90,
            "escalation_recall": 0.95,
            "escalation_precision": 0.94,
        }
        report = build_metrics_report(eval_report, rag_report, routing_report)
        metrics = report["metrics"]
        self.assertEqual(metrics["intent_case_count"], 20)
        self.assertEqual(metrics["dialog_eval_count"], 1)
        self.assertEqual(metrics["rag_query_count"], 10)
        self.assertEqual(metrics["rag_p95_latency_ms"], 18.7)
        self.assertEqual(metrics["routing_case_count"], 25)
        self.assertEqual(metrics["routing_accuracy"], 0.96)
        self.assertEqual(metrics["routing_eval_mode"], "gold_intent_rule_only")
        self.assertIn("resume_safe_bullets", report)


if __name__ == "__main__":
    unittest.main()
