"""Evaluate deterministic intent->Agent mapping, DAG validation and escalation.

Inputs are pre-annotated synthetic multi-intent cases; this does not measure
the LLM's multi-intent recognition accuracy or real end-to-end task success.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.agent_orchestrator import AgentOrchestrator, AgentType, Request
from agents.supervisor import DAGScheduler
from core.intent_recognizer import IntentCategory, RecognizedIntent, UrgencyLevel


def _load_cases(path: Path) -> List[Dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        required = {
            "id", "message", "intent", "expected_primary_agent",
            "expected_supporting_agents", "expected_dependencies", "compound",
            "should_escalate", "intents",
        }
        missing = required - set(row)
        if missing:
            raise ValueError(f"{path}:{line_number} 缺少字段: {sorted(missing)}")
        rows.append(row)
    if len(rows) < 20:
        raise ValueError("Supervisor 评测集至少需要20条样本")
    return rows


def _routing_only_orchestrator() -> AgentOrchestrator:
    orchestrator = object.__new__(AgentOrchestrator)
    orchestrator._pool = {agent_type: [object()] for agent_type in AgentType}
    orchestrator._max_subtasks = 4
    orchestrator._scheduler = DAGScheduler(
        [agent.value for agent in AgentType], max_subtasks=4,
    )
    return orchestrator


def _rate(rows: List[Dict[str, Any]], field: str) -> float:
    return round(sum(bool(row[field]) for row in rows) / len(rows), 4) if rows else 0.0


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 4)


def evaluate_supervisor(cases: List[Dict[str, Any]]) -> Dict[str, Any]:
    orchestrator = _routing_only_orchestrator()
    results = []
    latencies = []
    for row in cases:
        intents = [
            RecognizedIntent(
                IntentCategory(item["intent"]), item["text"],
                float(item.get("confidence", 1.0)),
                list(item.get("depends_on", [])),
            )
            for item in row["intents"]
        ]
        request = Request(
            message=row["message"],
            user_id="offline-eval",
            conv_id=row["id"],
            intent=intents[0].intent if intents else IntentCategory.OTHER,
            intent_group=row.get("intent_group"),
            intents=intents,
            urgency=UrgencyLevel.MEDIUM,
            intent_confidence=1.0 if intents else 0.0,
        )
        started = time.perf_counter()
        decision = orchestrator._route_decision(request)
        actual_compound = decision.multi_agent
        plan = None
        plan_fallback = False
        if actual_compound:
            try:
                plan = orchestrator._build_task_plan(
                    request, decision, orchestrator._high_risk_reasons(request),
                )
            except Exception:
                plan_fallback = True
        latencies.append((time.perf_counter() - started) * 1000)

        actual_agents = {decision.primary_agent.value, *[agent.value for agent in decision.supporting_agents]}
        expected_agents = {row["expected_primary_agent"], *row["expected_supporting_agents"]}
        actual_dependencies = {
            task.id: task.dependencies for task in plan.subtasks if task.dependencies
        } if plan else {}
        escalated = (
            orchestrator._requires_high_risk_escalation(request)
            or decision.primary_agent == AgentType.ESCALATION
        )
        compound_correct = actual_compound == bool(row["compound"])
        route_correct = decision.primary_agent.value == row["expected_primary_agent"]
        coverage_correct = actual_agents == expected_agents
        dependency_correct = actual_dependencies == row["expected_dependencies"]
        escalation_correct = escalated == bool(row["should_escalate"])
        plan_valid = (plan is not None and not plan_fallback) if row["compound"] else plan is None
        results.append({
            "id": row["id"],
            "tags": row.get("tags", []),
            "expected_compound": bool(row["compound"]),
            "compound_correct": compound_correct,
            "route_correct": route_correct,
            "subtask_coverage_correct": coverage_correct,
            "agent_assignment_correct": coverage_correct and route_correct,
            "dependency_correct": dependency_correct,
            "escalation_correct": escalation_correct,
            "plan_valid": plan_valid,
            "plan_fallback": plan_fallback,
            "actual_primary_agent": decision.primary_agent.value,
            "actual_supporting_agents": [agent.value for agent in decision.supporting_agents],
            "actual_dependencies": actual_dependencies,
            "passed": all((
                compound_correct, route_correct, coverage_correct,
                dependency_correct, escalation_correct, plan_valid,
            )),
        })

    dependency_cases = [row for row in results if "dependent" in row["tags"]]
    compound_cases = [row for row in results if row["expected_compound"]]
    return {
        "total": len(results),
        "compound_case_count": len(compound_cases),
        "compound_detection_accuracy": _rate(results, "compound_correct"),
        "subtask_coverage": _rate(compound_cases, "subtask_coverage_correct"),
        "agent_assignment_accuracy": _rate(compound_cases, "agent_assignment_correct"),
        "dependency_accuracy": _rate(dependency_cases, "dependency_correct"),
        "multi_agent_routing_accuracy": _rate(results, "route_correct"),
        "escalation_accuracy": _rate(results, "escalation_correct"),
        "control_plane_task_success_rate": _rate(results, "passed"),
        "end_to_end_task_success_rate": None,
        "task_plan_fallback_rate": _rate(results, "plan_fallback"),
        "synthesis_fallback_rate": None,
        "synthesis_evaluated": False,
        "avg_control_plane_latency_ms": round(statistics.mean(latencies), 4),
        "p95_control_plane_latency_ms": _percentile(latencies, 95),
        "failed": [row for row in results if not row["passed"]],
        "results": results,
    }


def main() -> int:
    cases = _load_cases(ROOT / "data/eval/supervisor_multi_intent_cases.jsonl")
    report = evaluate_supervisor(cases)
    payload = {
        "timestamp": datetime.now().isoformat(),
        "dataset": "internal synthetic annotated multi-intent control-plane set",
        "evaluation_mode": "gold_multi_intent_mapping_and_dag_only",
        **report,
    }
    reports_dir = ROOT / "data/eval/reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamped = reports_dir / f"{datetime.now().strftime('%Y%m%dT%H%M%S')}_supervisor.json"
    latest = reports_dir / "latest_supervisor.json"
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    timestamped.write_text(serialized, encoding="utf-8")
    latest.write_text(serialized, encoding="utf-8")
    print(json.dumps({
        key: payload[key] for key in (
            "dataset", "evaluation_mode", "total", "compound_detection_accuracy",
            "subtask_coverage", "agent_assignment_accuracy", "dependency_accuracy",
            "multi_agent_routing_accuracy", "escalation_accuracy",
            "control_plane_task_success_rate", "task_plan_fallback_rate",
            "avg_control_plane_latency_ms", "p95_control_plane_latency_ms",
        )
    } | {"failed_count": len(payload["failed"]), "report_path": str(timestamped)}, ensure_ascii=False, indent=2))

    failures = []
    if payload["compound_detection_accuracy"] < 0.90:
        failures.append("Compound Detection Accuracy < 0.90")
    if payload["agent_assignment_accuracy"] < 0.90:
        failures.append("Agent Assignment Accuracy < 0.90")
    high_risk = [row for row in payload["results"] if "high_risk" in row["tags"]]
    if high_risk and _rate(high_risk, "escalation_correct") < 0.95:
        failures.append("高风险升级准确率 < 0.95")
    if failures:
        raise SystemExit("; ".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
