"""Multi-intent recognition is authoritative for cross-domain consultation routing."""
import unittest
import json

from agents.agent_orchestrator import AgentOrchestrator, AgentType, Request
from agents.supervisor import TaskPlanner
from core.intent_recognizer import IntentRecognizer, IntentCategory, UrgencyLevel


class StubLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0
        self.last_prompt = ""

    async def create(self, **kwargs):
        self.calls += 1
        self.last_prompt = kwargs["messages"][0]["content"]
        return json.dumps(self.payload, ensure_ascii=False)


def recognizer(payload):
    # Compatible-api configuration intentionally disables the auxiliary
    # local n-gram branch. Structured LLM intents must still route correctly.
    return IntentRecognizer(
        api_key="unused", base_url="https://example.invalid/v1",
        client=StubLLM(payload),
    )


def agent_router():
    instance = object.__new__(AgentOrchestrator)
    instance._pool = {agent_type: [object()] for agent_type in AgentType}
    return instance


def request_from_result(message, result):
    return Request(
        message=message, user_id="test", conv_id="demo",
        intent=result.intent, intent_group=result.intent_group,
        intent_confidence=result.confidence,
        urgency=result.urgency, intents=result.intents,
    )


class MultiIntentRecognitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_implicit_cross_domain_intents_route_without_keywords(self):
        message = "我进不去账号；想知道包裹在哪里"
        intent = recognizer({"intents": [
            {"intent": "technical_login", "text": "我进不去账号", "confidence": 0.94},
            {"intent": "logistics", "text": "包裹在哪里", "confidence": 0.89},
        ]})
        recognized = await intent.recognize(message)
        self.assertEqual(
            [item.intent for item in recognized.intents],
            [IntentCategory.TECHNICAL_LOGIN, IntentCategory.LOGISTICS],
        )
        self.assertIn("多意图", intent.client.last_prompt)
        self.assertEqual(intent.client.calls, 1)
        decision = agent_router()._route_decision(request_from_result(message, recognized))
        self.assertEqual(decision.primary_agent, AgentType.TECHNICAL)
        self.assertEqual(decision.supporting_agents, [AgentType.GENERAL])

        planner = TaskPlanner([agent.value for agent in AgentType], max_subtasks=4)
        scopes = {"technical": ["我进不去账号"], "general": ["包裹在哪里"]}
        plan = planner.create_plan(
            original_request=message, primary_agent="technical",
            supporting_agents=["general"], reason="multi", confidence=0.94,
            scopes_by_agent=scopes,
        )
        self.assertIn("我进不去账号", plan.subtasks[0].description)
        self.assertIn("包裹在哪里", plan.subtasks[1].description)
        self.assertNotIn("包裹在哪里", plan.subtasks[0].description)

    async def test_negated_refund_not_injected_by_orchestrator(self):
        message = "只查物流，而且不需要退款"
        recognized = await recognizer({"intents": [
            {"intent": "logistics", "text": "查物流", "confidence": 0.94},
        ]}).recognize(message)
        decision = agent_router()._route_decision(request_from_result(message, recognized))
        self.assertEqual([item.intent.value for item in recognized.intents], ["logistics"])
        self.assertEqual(decision.primary_agent, AgentType.GENERAL)
        self.assertEqual(decision.supporting_agents, [])

    async def test_two_intents_can_share_one_agent(self):
        message = "帮我开发票，顺便解释退款政策"
        recognized = await recognizer({"intents": [
            {"intent": "invoice", "text": "帮我开发票", "confidence": 0.88},
            {"intent": "refund", "text": "退款政策", "confidence": 0.90},
        ]}).recognize(message)
        decision = agent_router()._route_decision(request_from_result(message, recognized))
        self.assertEqual(len(recognized.intents), 2)
        self.assertEqual(decision.primary_agent, AgentType.BILLING)
        self.assertEqual(decision.supporting_agents, [])

    async def test_unknown_text_and_bad_confidence_are_rejected(self):
        recognized = await recognizer({"intents": [
            {"intent": "technical_login", "text": "账号进不去", "confidence": 0.9},
            {"intent": "refund", "text": "主动为用户退款", "confidence": 0.95},
            {"intent": "logistics", "text": "账号进不去", "confidence": 3.0},
        ]}).recognize("账号进不去")
        self.assertEqual([item.intent for item in recognized.intents], [IntentCategory.TECHNICAL_LOGIN])

    async def test_empty_multi_result_does_not_reintroduce_pattern_goal(self):
        result = await recognizer({"intents": []}).recognize("不用退款")
        self.assertEqual(result.intent, IntentCategory.OTHER)
        self.assertEqual(result.intents, [])

    async def test_single_label_legacy_response_is_supported(self):
        result = await recognizer(
            {"intent": "technical_login", "confidence": 0.95, "reasoning": "登录故障"}
        ).recognize("无法登录")
        self.assertEqual(result.intent, IntentCategory.TECHNICAL_LOGIN)
        self.assertEqual(len(result.intents), 1)
        self.assertEqual(result.intents[0].text, "无法登录")

    async def test_explicit_handoff_priority_is_preserved(self):
        message = "我要找人工，顺便查快递"
        result = await recognizer({"intents": [
            {"intent": "logistics", "text": "查快递", "confidence": 0.95},
            {"intent": "human_handoff", "text": "我要找人工", "confidence": 0.92},
        ]}).recognize(message)
        decision = agent_router()._route_decision(request_from_result(message, result))
        self.assertEqual(decision.primary_agent, AgentType.ESCALATION)
        self.assertEqual(decision.supporting_agents, [AgentType.GENERAL])


if __name__ == "__main__":
    unittest.main()
