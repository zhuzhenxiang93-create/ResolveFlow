"""Summarize existing P0 reports without model calls or changing evidence."""
import argparse
import json
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", default="p0-20260911")
    args = parser.parse_args()
    directory = ROOT / "data/eval/reports/agent"
    runs = sorted([json.loads(p.read_text()) for p in directory.glob("p0-live-*.json")], key=lambda r: r["timestamp"])
    runs = [r for r in runs if r["campaign"] == args.campaign]
    if not runs:
        raise SystemExit("No matching live reports")
    final = next(r for r in reversed(runs) if len(r["results"]) == 11)
    journeys = [r for r in final["results"] if r["kind"] == "journey"]
    semantics = [r for r in final["results"] if r["kind"] == "semantic"]
    latencies = sorted(r["latency_ms"] for r in final["results"] if "latency_ms" in r)
    usage = [u for r in runs for u in r["metrics"]["usage"]]
    report = {"campaign": args.campaign, "latest_full_run": final["run_id"], "dataset_sha256": final["dataset_sha256"],
              "code_sha256": final["code_sha256"], "model": final["model"], "sample_count": len(final["results"]),
              "expected_outcomes": final["metrics"]["expected_outcomes"], "raw_model_semantics": final["metrics"]["raw_model_semantics"],
              "guarded_semantic_cases": sum(bool(r.get("record", {}).get("server_guard")) for r in semantics),
              "journey_expected_outcomes": {"numerator": sum(r["passed"] for r in journeys), "denominator": len(journeys)},
              "business_completed": {"numerator": sum(r.get("status") == "completed" for r in journeys), "denominator": len(journeys)},
              "unauthorized_operations": sum(r.get("unauthorized_operations", 0) for r in journeys),
              "duplicate_operations": sum(r.get("duplicate_operations", 0) for r in journeys),
              "campaign_calls": sum(r["metrics"]["actual_calls_this_run"] for r in runs),
              "campaign_input_tokens": sum(u["input_tokens"] for u in usage), "campaign_output_tokens": sum(u["output_tokens"] for u in usage),
              "cost_usd": None, "test_latency_mean_ms": mean(latencies), "test_latency_p95_ms": latencies[min(len(latencies)-1, int(len(latencies)*0.95))],
              "runs": [{"id": r["run_id"], "cases": len(r["results"]), "outcomes": r["metrics"]["expected_outcomes"]} for r in runs],
              "limitations": ["Synthetic internal regression, not unseen model accuracy", "8 semantic probes do not score final answer quality", "3 simulated journeys with automated human-role actions", "No changes to user's original task", "Offline natural-language rules are not equivalent to live semantics"]}
    json_path = directory / "latest_p0_metrics.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = ["# P0 真实验收结果", "", "内部合成回归集，不是生产指标或独立盲测。", "",
             "| 项目 | 结果 |", "| --- | --- |",
             "| 最终预期结果 | %s / 11 |" % report["expected_outcomes"]["numerator"],
             "| 原始模型目标判断 | %s / 8 |" % report["raw_model_semantics"]["numerator"],
             "| 依赖服务端校正的语义案例 | %s |" % report["guarded_semantic_cases"],
             "| 执行流程预期结果 | %s / 3 |" % report["journey_expected_outcomes"]["numerator"],
             "| 业务完成 | %s / 3（另含用户拒绝场景） |" % report["business_completed"]["numerator"],
             "| 未确认 / 重复操作 | %s / %s（仅3个流程） |" % (report["unauthorized_operations"], report["duplicate_operations"]),
             "| 累计调用 | %s / 60 |" % report["campaign_calls"],
             "| 累计输入 / 输出Token | %s / %s |" % (report["campaign_input_tokens"], report["campaign_output_tokens"]),
             "| 费用 | unavailable |", "", "## 保留失败与修复过程"]
    lines += ["- %s：%s/%s，通过数/所选数。" % (r["id"], r["outcomes"]["numerator"], r["cases"]) for r in report["runs"]]
    lines += ["", "## 证据边界", "- 首轮结果不能删除；根据失败修复后，该集合仅用于回归验收。",
              "- 未改动或批准用户原任务，原任务是否恢复仍须用户在新版本服务中操作。",
              "- 真实恢复测试使用隔离临时库与模拟审核员，不等于生产重启容灾验证。",
              "- 离线自然语言规则首轮仅3/11，不应冒充等价的真实模型能力。",
              "- 完整报告包含自动化耗时；其中不包括真实人的确认等待，不适合当线上响应延迟。"]
    (directory / "latest_p0_metrics.md").write_text("\n".join(lines) + "\n")
    print(json_path)


if __name__ == "__main__":
    main()
