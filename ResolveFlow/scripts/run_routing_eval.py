"""离线评测确定性 Agent 路由规则，不依赖 LLM API。"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.agent_orchestrator import AgentOrchestrator, AgentType, Request
from core.intent_recognizer import IntentCategory, UrgencyLevel
from evaluation.dataset_loader import load_multi_agent_profile


def _routing_only_orchestrator() -> AgentOrchestrator:
    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator._pool = {agent_type: [object()] for agent_type in AgentType}
    return orchestrator


def _message(record: Dict[str, Any]) -> str:
    if record.get("text"):
        return str(record["text"])
    return " ".join(str(turn) for turn in record.get("turns", []))


def evaluate_routing(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    orchestrator = _routing_only_orchestrator()
    results = []
    latencies = []

    for record in records:
        request = Request(
            message=_message(record),
            user_id="offline-eval",
            conv_id=record["id"],
            entities=record.get("entities") or {},
            intent=IntentCategory(record["expected_intent"]),
            intent_group=record.get("expected_group"),
            urgency=UrgencyLevel.MEDIUM,
            intent_confidence=1.0,
        )
        started = time.perf_counter()
        decision = orchestrator._route_decision(request)
        escalated = (
            orchestrator._requires_high_risk_escalation(request)
            or request.intent in (IntentCategory.ESCALATION, IntentCategory.HUMAN_HANDOFF)
        )
        latencies.append((time.perf_counter() - started) * 1000)

        expected_supporting = set(record.get("expected_supporting_agents") or [])
        actual_supporting = {agent.value for agent in decision.supporting_agents}
        route_correct = decision.primary_agent.value == record["expected_primary_agent"]
        supporting_correct = actual_supporting == expected_supporting
        escalation_correct = bool(escalated) == bool(record.get("should_escalate"))
        results.append({
            "id": record["id"],
            "expected_primary_agent": record["expected_primary_agent"],
            "actual_primary_agent": decision.primary_agent.value,
            "expected_supporting_agents": sorted(expected_supporting),
            "actual_supporting_agents": sorted(actual_supporting),
            "expected_escalation": bool(record.get("should_escalate")),
            "actual_escalation": bool(escalated),
            "route_correct": route_correct,
            "supporting_agents_correct": supporting_correct,
            "escalation_correct": escalation_correct,
            "routing_reason": decision.reason,
        })

    escalation_cases = [r for r in results if r["expected_escalation"]]
    predicted_escalations = [r for r in results if r["actual_escalation"]]
    cross_agent_cases = [r for r in results if r["expected_supporting_agents"]]
    count = len(results)
    return {
        "total": count,
        "escalation_case_count": len(escalation_cases),
        "cross_agent_case_count": len(cross_agent_cases),
        "routing_accuracy": _rate(results, "route_correct"),
        "supporting_agent_accuracy": _rate(results, "supporting_agents_correct"),
        "cross_agent_coverage": _rate(cross_agent_cases, "supporting_agents_correct"),
        "escalation_recall": _rate(escalation_cases, "actual_escalation"),
        "escalation_precision": _rate(predicted_escalations, "expected_escalation"),
        "escalation_accuracy": _rate(results, "escalation_correct"),
        "avg_rule_latency_ms": round(sum(latencies) / len(latencies), 4) if latencies else 0.0,
        "p95_rule_latency_ms": _percentile(latencies, 95),
        "failed": [
            result for result in results
            if not (
                result["route_correct"]
                and result["supporting_agents_correct"]
                and result["escalation_correct"]
            )
        ],
        "results": results,
    }


def _rate(records: List[Dict[str, Any]], field: str) -> float:
    if not records:
        return 0.0
    return round(sum(bool(record[field]) for record in records) / len(records), 4)


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 4)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=("dev", "holdout", "all"), default="holdout")
    args = parser.parse_args()

    data_root = ROOT / "data"
    intent_cases, dialog_cases = load_multi_agent_profile(data_root, split=args.split)
    report = evaluate_routing(intent_cases + dialog_cases)
    payload = {
        "timestamp": datetime.now().isoformat(),
        "dataset": "internal golden set",
        "split": args.split,
        "evaluation_mode": "gold_intent_rule_only",
        "intent_case_count": len(intent_cases),
        "dialog_case_count": len(dialog_cases),
        "approved_case_count": sum(
            record.get("review_status") == "approved" for record in intent_cases + dialog_cases
        ),
        **report,
    }

    reports_dir = data_root / "eval" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamped_path = reports_dir / f"{datetime.now().strftime('%Y%m%dT%H%M%S')}_routing.json"
    latest_path = reports_dir / "latest_routing.json"
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    timestamped_path.write_text(serialized, encoding="utf-8")
    latest_path.write_text(serialized, encoding="utf-8")

    print(json.dumps({
        "dataset": payload["dataset"],
        "split": args.split,
        "evaluation_mode": payload["evaluation_mode"],
        "total": payload["total"],
        "routing_accuracy": payload["routing_accuracy"],
        "supporting_agent_accuracy": payload["supporting_agent_accuracy"],
        "cross_agent_coverage": payload["cross_agent_coverage"],
        "escalation_recall": payload["escalation_recall"],
        "escalation_precision": payload["escalation_precision"],
        "escalation_accuracy": payload["escalation_accuracy"],
        "avg_rule_latency_ms": payload["avg_rule_latency_ms"],
        "p95_rule_latency_ms": payload["p95_rule_latency_ms"],
        "failed_count": len(payload["failed"]),
        "report_path": str(timestamped_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
