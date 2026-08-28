"""正式评测基线的升级门槛，保持为无运行时依赖的纯规则。"""
from typing import Dict


FINAL_MINIMUMS = {
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
}


def meets_final_thresholds(scores: Dict[str, float]) -> bool:
    """只有完整终验指标全部达标且无禁用话术违规时才能升级基线。"""
    for metric, threshold in FINAL_MINIMUMS.items():
        if metric not in scores or scores[metric] < threshold:
            return False
    return scores.get("must_not_include_violation_rate") == 0.0
