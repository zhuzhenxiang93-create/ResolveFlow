"""不依赖模型运行时的客服评测硬规则。"""
from typing import Any, Dict, List


def evaluate_hard_rules(
    expectation: Dict[str, Any],
    response: str,
    actual_agent: str,
    actual_agent_types: List[str],
    actual_escalated: bool,
    actual_entities: Dict[str, List[str]],
    knowledge_used: bool,
    has_history: bool,
) -> Dict[str, Any]:
    """检查路由、升级、知识、话术和记忆约束。"""
    normalized = response.lower()
    must_include = [str(item).lower() for item in expectation.get("must_include", [])]
    must_not_include = [str(item).lower() for item in expectation.get("must_not_include", [])]
    required_entity_keys = set((expectation.get("entities") or {}).keys())
    matched_includes = [item for item in must_include if item in normalized]
    violations = [item for item in must_not_include if item in normalized]
    forbidden_commitments = [item for item in ("保证退款", "一定退款", "立刻到账", "立即到账") if item in response]
    credential_request = any(
        phrase in response for phrase in ("提供密码", "告诉我密码", "发送验证码给我")
    )

    expected_agent = expectation.get("expected_primary_agent")
    route_correct = expected_agent is None or expected_agent == actual_agent
    expected_supporting = set(expectation.get("expected_supporting_agents") or [])
    actual_supporting = set(actual_agent_types) - {actual_agent}
    supporting_correct = expected_supporting == actual_supporting
    expected_escalation = expectation.get("should_escalate")
    escalation_correct = expected_escalation is None or bool(expected_escalation) == actual_escalated
    expected_knowledge = expectation.get("should_use_knowledge")
    knowledge_correct = expected_knowledge is None or bool(expected_knowledge) == knowledge_used
    entity_hits = required_entity_keys.intersection(actual_entities.keys())
    entity_rate = len(entity_hits) / len(required_entity_keys) if required_entity_keys else None
    memory_keywords = [str(item).lower() for item in expectation.get("memory_keywords", [])]
    memory_correct = not memory_keywords or (has_history and all(item in normalized for item in memory_keywords))

    detail = {
        "route_correct": route_correct,
        "supporting_agents_correct": supporting_correct,
        "expected_supporting_agents": sorted(expected_supporting),
        "actual_supporting_agents": sorted(actual_supporting),
        "escalation_correct": escalation_correct,
        "knowledge_correct": knowledge_correct,
        "must_include_missing": [item for item in must_include if item not in matched_includes],
        "must_not_include_violations": violations,
        "forbidden_commitments": forbidden_commitments,
        "credential_request": credential_request,
        "entity_expected_keys": sorted(required_entity_keys),
        "entity_hit_keys": sorted(entity_hits),
        "memory_correct": memory_correct,
    }
    passed = (
        route_correct
        and supporting_correct
        and escalation_correct
        and knowledge_correct
        and len(matched_includes) == len(must_include)
        and not violations
        and not forbidden_commitments
        and not credential_request
        and memory_correct
        and (entity_rate is None or entity_rate == 1.0)
    )
    metric_scores: Dict[str, float] = {
        "routing_accuracy": 1.0 if route_correct else 0.0,
        "supporting_agent_accuracy": 1.0 if supporting_correct else 0.0,
        "must_include_hit_rate": len(matched_includes) / len(must_include) if must_include else 1.0,
        "must_not_include_violation_rate": 1.0 if violations or forbidden_commitments or credential_request else 0.0,
        "knowledge_expectation_accuracy": 1.0 if knowledge_correct else 0.0,
    }
    if expected_escalation:
        metric_scores["escalation_recall"] = 1.0 if actual_escalated else 0.0
    if entity_rate is not None:
        metric_scores["entity_key_hit_rate"] = entity_rate
    if memory_keywords:
        metric_scores["memory_consistency"] = 1.0 if memory_correct else 0.0
    if expected_supporting:
        metric_scores["cross_agent_coverage"] = 1.0 if supporting_correct else 0.0
    return {"passed": passed, "scores": metric_scores, "detail": detail}
