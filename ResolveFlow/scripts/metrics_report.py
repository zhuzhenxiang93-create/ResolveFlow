"""生成可复现的 ResolveFlow 简历指标报告。

该脚本只汇总已经真实产生的评测报告，不编造线上指标。
无 LLM API Key 时也可运行，用于离线检查规则指标和 RAG 指标。
"""
import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


ROOT = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / "data/eval/reports"


CORE_METRICS = [
    "intent_accuracy",
    "macro_f1",
    "routing_accuracy",
    "supporting_agent_accuracy",
    "cross_agent_coverage",
    "escalation_recall",
    "knowledge_expectation_accuracy",
    "must_not_include_violation_rate",
]


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_report(pattern: str, excluded_names: Iterable[str] = ()) -> Optional[Path]:
    excluded = set(excluded_names)
    candidates = [
        path for path in REPORTS_DIR.glob(pattern)
        if path.name not in excluded and path.is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def latest_eval_report() -> Optional[Path]:
    candidates = [
        path for path in REPORTS_DIR.glob("*.json")
        if path.name not in {"latest_metrics.json", "latest_rag.json"}
        and not path.name.endswith("_rag.json")
        and not path.name.endswith("_routing.json")
        and path.name != "latest_routing.json"
        and not path.name.endswith("_supervisor.json")
        and path.name != "latest_supervisor.json"
        and path.is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def latest_rag_report() -> Optional[Path]:
    path = REPORTS_DIR / "latest_rag.json"
    if path.exists():
        return path
    return _latest_report("*_rag.json")


def latest_routing_report() -> Optional[Path]:
    path = REPORTS_DIR / "latest_routing.json"
    if path.exists():
        return path
    return _latest_report("*_routing.json")


def latest_supervisor_report() -> Optional[Path]:
    path = REPORTS_DIR / "latest_supervisor.json"
    if path.exists():
        return path
    return _latest_report("*_supervisor.json")


def extract_eval_metrics(eval_report: Dict[str, Any]) -> Dict[str, Any]:
    avg_scores = dict(eval_report.get("avg_scores") or {})
    results = list(eval_report.get("results") or [])
    metrics = {key: avg_scores[key] for key in CORE_METRICS if key in avg_scores}

    for result in results:
        if result.get("test_id") == "intent_recognition":
            scores = result.get("scores") or {}
            metadata = result.get("metadata") or {}
            if "accuracy" in scores:
                metrics.setdefault("intent_accuracy", scores["accuracy"])
            if "macro_f1" in scores:
                metrics.setdefault("macro_f1", scores["macro_f1"])
            metrics["intent_case_count"] = metadata.get("total", 0)
            metrics["intent_correct_count"] = metadata.get("correct", 0)
            break

    dialog_results = [item for item in results if item.get("test_id") != "intent_recognition"]
    metrics["dialog_eval_count"] = len(dialog_results)
    metrics["eval_result_count"] = eval_report.get("total", len(results))
    metrics["eval_pass_rate"] = eval_report.get("pass_rate", 0.0)

    latency_values = [
        float((item.get("metadata") or {}).get("latency_ms"))
        for item in dialog_results
        if isinstance((item.get("metadata") or {}).get("latency_ms"), (int, float))
    ]
    if latency_values:
        metrics["avg_latency_ms"] = round(statistics.mean(latency_values), 2)
        metrics["p95_latency_ms"] = percentile(latency_values, 95)
    return metrics


def extract_rag_metrics(rag_report: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not rag_report:
        return {}
    return {
        "rag_query_count": rag_report.get("total", 0),
        "rag_retriever_mode": rag_report.get("retriever_mode", "unknown"),
        "rag_top_1_hit_rate": rag_report.get("top_1_hit_rate"),
        "rag_top_3_hit_rate": rag_report.get("top_3_hit_rate", rag_report.get("top_k_hit_rate")),
        "rag_top_5_hit_rate": rag_report.get("top_5_hit_rate"),
        "rag_mrr": rag_report.get("mrr"),
        "rag_exact_identifier_count": rag_report.get("exact_identifier_total", 0),
        "rag_exact_identifier_top_3_hit_rate": rag_report.get("exact_identifier_top_3_hit_rate"),
        "rag_no_match_count": rag_report.get("no_match_total", 0),
        "rag_no_match_accuracy": rag_report.get("no_match_accuracy"),
        "rag_index_build_latency_ms": rag_report.get("index_build_latency_ms"),
        "rag_warmup_query_latency_ms": rag_report.get("warmup_query_latency_ms"),
        "rag_avg_latency_ms": rag_report.get("avg_latency_ms"),
        "rag_p95_latency_ms": rag_report.get("p95_latency_ms"),
    }


def percentile(values: List[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return round(ordered[0], 2)
    rank = (len(ordered) - 1) * pct / 100
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 2)


def _report_ref(path: Optional[Path]) -> Optional[str]:
    """优先记录项目内相对路径，避免报告绑定开发机目录。"""
    if path is None:
        return None
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return path.name


def build_metrics_report(
    eval_report: Dict[str, Any],
    rag_report: Optional[Dict[str, Any]] = None,
    routing_report: Optional[Dict[str, Any]] = None,
    supervisor_report: Optional[Dict[str, Any]] = None,
    *,
    eval_path: Optional[Path] = None,
    rag_path: Optional[Path] = None,
    routing_path: Optional[Path] = None,
    supervisor_path: Optional[Path] = None,
) -> Dict[str, Any]:
    metrics = extract_eval_metrics(eval_report)
    metrics.update({k: v for k, v in extract_rag_metrics(rag_report).items() if v is not None})
    if routing_report:
        for key in (
            "routing_accuracy", "supporting_agent_accuracy", "cross_agent_coverage",
            "escalation_recall", "escalation_precision", "escalation_accuracy",
            "avg_rule_latency_ms", "escalation_case_count",
            "p95_rule_latency_ms", "cross_agent_case_count",
        ):
            if routing_report.get(key) is not None:
                metrics[key] = routing_report[key]
        metrics["routing_case_count"] = routing_report.get("total", 0)
        metrics["routing_approved_case_count"] = routing_report.get("approved_case_count", 0)
        metrics["routing_eval_mode"] = routing_report.get("evaluation_mode", "unknown")
        metrics["routing_eval_split"] = routing_report.get("split", "unknown")
    if supervisor_report:
        for key in (
            "compound_detection_accuracy", "subtask_coverage",
            "agent_assignment_accuracy", "dependency_accuracy",
            "multi_agent_routing_accuracy", "control_plane_task_success_rate",
            "planner_fallback_rate",
            "synthesis_fallback_rate", "avg_control_plane_latency_ms",
            "p95_control_plane_latency_ms",
        ):
            if supervisor_report.get(key) is not None:
                metrics[key] = supervisor_report[key]
        if supervisor_report.get("escalation_accuracy") is not None:
            metrics["supervisor_escalation_accuracy"] = supervisor_report["escalation_accuracy"]
        metrics["supervisor_case_count"] = supervisor_report.get("total", 0)
        metrics["supervisor_eval_mode"] = supervisor_report.get("evaluation_mode", "unknown")
    return {
        "timestamp": datetime.now().isoformat(),
        "dataset_note": "on internal golden set; not production traffic",
        "source_reports": {
            "eval_report": _report_ref(eval_path),
            "rag_report": _report_ref(rag_path),
            "routing_report": _report_ref(routing_path),
            "supervisor_report": _report_ref(supervisor_path),
        },
        "metrics": metrics,
        "resume_safe_bullets": resume_safe_bullets(metrics),
        "not_resume_safe": not_resume_safe(metrics),
    }


def _pct(value: Any) -> str:
    return f"{float(value) * 100:.1f}%"


def resume_safe_bullets(metrics: Dict[str, Any]) -> List[str]:
    bullets: List[str] = []
    intent_count = int(metrics.get("intent_case_count") or 0)
    dialog_count = int(metrics.get("dialog_eval_count") or 0)
    rag_count = int(metrics.get("rag_query_count") or 0)
    routing_count = int(metrics.get("routing_case_count") or 0)
    supervisor_count = int(metrics.get("supervisor_case_count") or 0)
    if intent_count or dialog_count or routing_count or rag_count or supervisor_count:
        bullets.append(
            f"构建并维护内部 golden set：{intent_count} 条意图样本、"
            f"{dialog_count} 条端到端对话评测、{routing_count} 条路由 holdout 样本、"
            f"{supervisor_count} 条 Supervisor 控制面样本、{rag_count} 条 RAG 查询，"
            "用于可复现质量验收。"
        )
    if "intent_accuracy" in metrics and "macro_f1" in metrics:
        bullets.append(
            f"在内部 golden set 上验证细粒度意图识别，Accuracy={_pct(metrics['intent_accuracy'])}，"
            f"Macro-F1={float(metrics['macro_f1']):.3f}。"
        )
    if "routing_accuracy" in metrics:
        mode = metrics.get("routing_eval_mode")
        prefix = "在内部合成 holdout 上进行给定金标意图的离线路由评测" if mode == "gold_intent_rule_only" else "通过结构化主辅 Agent 路由评测"
        text = f"{prefix}，Routing Accuracy={_pct(metrics['routing_accuracy'])}"
        if "supporting_agent_accuracy" in metrics:
            text += f"，Supporting Agent Accuracy={_pct(metrics['supporting_agent_accuracy'])}"
        if "cross_agent_coverage" in metrics:
            text += f"，Cross-Agent Coverage={_pct(metrics['cross_agent_coverage'])}"
        bullets.append(text + "。")
    if "escalation_recall" in metrics:
        text = f"针对高风险客服场景建立升级评测，Escalation Recall={_pct(metrics['escalation_recall'])}"
        if "escalation_precision" in metrics:
            text += f"，Precision={_pct(metrics['escalation_precision'])}"
        bullets.append(text + "。")
    if "compound_detection_accuracy" in metrics:
        bullets.append(
            "在内部确定性 Supervisor 集上验证复合请求拆分与依赖规划："
            f"Compound Detection={_pct(metrics['compound_detection_accuracy'])}，"
            f"Agent Assignment={_pct(metrics['agent_assignment_accuracy'])}，"
            f"Dependency Accuracy={_pct(metrics['dependency_accuracy'])}。"
        )
    if "must_not_include_violation_rate" in metrics:
        bullets.append(
            f"引入 hard rules 约束退款承诺和敏感凭据索取，禁用话术违规率={_pct(metrics['must_not_include_violation_rate'])}。"
        )
    if "rag_top_3_hit_rate" in metrics and metrics.get("rag_retriever_mode") != "lexical_fallback":
        bullets.append(
            f"基于 expected_document_id 验收 RAG 检索质量（{metrics.get('rag_retriever_mode', 'unknown')}），"
            f"Top-3 Hit Rate={_pct(metrics['rag_top_3_hit_rate'])}"
            + (f"，Top-5 Hit Rate={_pct(metrics['rag_top_5_hit_rate'])}" if "rag_top_5_hit_rate" in metrics else "")
            + (f"，MRR={float(metrics['rag_mrr']):.3f}" if "rag_mrr" in metrics else "")
            + (
                f"，精确标识符 Top-3={_pct(metrics['rag_exact_identifier_top_3_hit_rate'])}"
                if metrics.get("rag_exact_identifier_top_3_hit_rate") is not None else ""
            )
            + "。"
        )
    return bullets


def not_resume_safe(metrics: Dict[str, Any]) -> List[str]:
    warnings = [
        "未接入真实线上流量，不能写生产用户数、线上 QPS 或线上 SLA。",
        "LLM 端到端延迟受外部模型服务影响，未压测前不要写生产 P95。",
    ]
    if "rag_top_3_hit_rate" not in metrics:
        warnings.append("尚未生成 RAG 报告，不能写 RAG Top-K 指标。")
    if metrics.get("rag_retriever_mode") == "lexical_fallback":
        warnings.append("当前 RAG 指标来自 lexical fallback，只能证明评测链路可跑；写简历前应安装 ChromaDB 并重跑。")
    if not metrics.get("avg_latency_ms"):
        warnings.append("当前评测报告缺少端到端延迟采样，不能写 /chat 平均或 P95 延迟。")
    routing_total = int(metrics.get("routing_case_count") or 0)
    approved_total = int(metrics.get("routing_approved_case_count") or 0)
    if routing_total and approved_total < routing_total:
        warnings.append(
            f"路由 holdout 当前为内部合成集，且仅 {approved_total}/{routing_total} 条完成人工 approved 标记；"
            "简历须注明 internal synthetic golden set，不能宣称人工标注数据集。"
        )
    return warnings


def write_markdown(report: Dict[str, Any], path: Path) -> None:
    metrics = report["metrics"]
    lines = [
        "# ResolveFlow Metrics Report",
        "",
        f"- Generated at: {report['timestamp']}",
        f"- Dataset: {report['dataset_note']}",
        "",
        "## Metrics",
        "",
    ]
    for key in sorted(metrics):
        lines.append(f"- `{key}`: {metrics[key]}")
    lines.extend(["", "## Resume-safe bullets", ""])
    for bullet in report["resume_safe_bullets"]:
        lines.append(f"- {bullet}")
    lines.extend(["", "## Not resume-safe", ""])
    for item in report["not_resume_safe"]:
        lines.append(f"- {item}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval-report", type=Path, default=None)
    parser.add_argument("--rag-report", type=Path, default=None)
    parser.add_argument("--routing-report", type=Path, default=None)
    parser.add_argument("--supervisor-report", type=Path, default=None)
    args = parser.parse_args(argv)

    eval_path = args.eval_report or latest_eval_report()
    if eval_path is None or not eval_path.exists():
        raise SystemExit("未找到 eval JSON 报告，请先运行评测或保留 data/eval/reports 下的报告。")
    rag_path = args.rag_report or latest_rag_report()
    routing_path = args.routing_report or latest_routing_report()
    supervisor_path = args.supervisor_report or latest_supervisor_report()
    eval_report = _load_json(eval_path)
    rag_report = _load_json(rag_path) if rag_path and rag_path.exists() else None
    routing_report = _load_json(routing_path) if routing_path and routing_path.exists() else None
    supervisor_report = _load_json(supervisor_path) if supervisor_path and supervisor_path.exists() else None
    report = build_metrics_report(
        eval_report,
        rag_report,
        routing_report,
        supervisor_report,
        eval_path=eval_path,
        rag_path=rag_path,
        routing_path=routing_path,
        supervisor_path=supervisor_path,
    )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORTS_DIR / "latest_metrics.json"
    md_path = REPORTS_DIR / "latest_metrics.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, md_path)

    print(json.dumps({
        "dataset": report["dataset_note"],
        "metrics": report["metrics"],
        "json_path": str(json_path),
        "markdown_path": str(md_path),
    }, ensure_ascii=False, indent=2))
    print("\nResume-safe bullets:")
    for bullet in report["resume_safe_bullets"]:
        print(f"- {bullet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
