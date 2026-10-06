"""Build the Product Evaluation report (JSON) and its human-readable Markdown."""
from __future__ import annotations

import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from evaluation import bad_case_analyzer, product_evaluator, product_metrics

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "data/eval/reports"
LATEST = REPORTS / "product_eval_latest.json"
BASELINE = REPORTS / "product_eval_baseline.json"
DOC = ROOT.parent / "docs/product-evaluation.md"
HEADLINE = ["task_success_rate", "object_resolution_accuracy", "confirmation_compliance", "unauthorized_operations",
            "multi_intent_completion_rate", "clarification_accuracy", "action_tool_success_rate", "avg_turns_to_resolution"]


def _git(*args):
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=5).stdout.strip() or None
    except Exception:
        return None


def _trace_view(result):
    view = []
    for t in result.get("trace", []):
        item = {"step": t["step"], "kind": t["kind"]}
        if t.get("text"):
            item["text"] = t["text"]
        if t.get("skipped"):
            item["skipped"] = t["note"]
        if "http" in t:
            item["http"] = t["http"]
        if t.get("result") and isinstance(t["result"], dict) and "status" in t["result"]:
            r = t["result"]
            item.update(status=r["status"], route=r["route"], candidates=r["candidates"], operations=r["case_operations"],
                        sources=r["sources"], tools_used=r["tools_used"], degradations=r["degradations"],
                        response=r["response"][:400])
        if t.get("action"):
            a = t["action"]
            item["action"] = f"{a['object']}/{a['operation']} (was {a['status']})"
        if t.get("attack"):
            item["attack"] = t["attack"]
        if t["kind"] == "reviewer":
            item["decision"] = t["spec"]["reviewer"]
        if t["kind"] == "system":
            item["system"] = t["spec"]["system"]
        view.append(item)
    return view


def build(mode, results, label, data_path=product_evaluator.DATASET):
    graded = [(r, product_metrics.grade_case(r)) for r in results]
    metrics, categories = product_metrics.aggregate(graded)
    bad = [bad_case_analyzer.analyse(r, g) for r, g in graded if not g["passed"]]
    cases = []
    for r, g in graded:
        c = r["case"]
        analysis = next((b for b in bad if b["case_id"] == c["id"]), None)
        cases.append({"id": c["id"], "category": c["category"], "title": c.get("title", ""), "passed": g["passed"],
                      "result": "PASSED" if g["passed"] else "FAILED", "user_turns": r.get("user_turns"),
                      "max_user_turns": c["max_user_turns"], "checks": g["checks"], "failed_metrics": g["failed_metrics"],
                      "failure_type": analysis["failure_type"] if analysis else None,
                      "root_cause": analysis["root_cause"] if analysis else None,
                      "goals": [{"type": x["goal"]["type"], "object": x["goal"].get("object"), "achieved": x["achieved"],
                                 "evidence": x["evidence"]} for x in g["goals"]],
                      "writes": g["writes"], "unauthorized": g["unauthorized"],
                      "expected_behaviour": c.get("expected_behaviour", ""), "trace": _trace_view(r),
                      "latency_ms": r.get("latency_ms")})
    dataset = product_evaluator.load_cases(data_path)
    report = {
        "schema_version": 1,
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + label,
        "label": label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git("rev-parse", "--short", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "mode": mode,
        "scope": {
            "chain": "POST /chat → ConversationService → CommerceConversation → CommerceStore (isolated Demo host, real JWT user/reviewer identities)",
            "judgement": "Final business state read from the per-session SQLite database (objects, refunds, cases, audit); assistant text only judged for read-only answers",
            "storage": "SQLite per Demo session", "retrieval": "BM25 lexical policy retrieval (Demo); no ChromaDB / hybrid RAG in this evaluation",
            "simulated": True, "not_covered": ["real payments or orders", "production traffic", "Redis / ChromaDB full backend",
                                               "live-model nondeterminism (unless mode=live-model)"],
        },
        "dataset": {"path": str(Path(data_path).relative_to(ROOT)), "sha256": product_evaluator.dataset_digest(data_path),
                    "case_count": len(dataset), "categories": dict(Counter(c["category"] for c in dataset))},
        "metrics": metrics,
        "headline": HEADLINE,
        "category_breakdown": categories,
        "bad_case_summary": {"total": len(bad), "by_failure_type": bad_case_analyzer.breakdown(bad, len(graded)),
                             "root_causes": dict(Counter(b["root_cause"].split(":")[0] for b in bad))},
        "bad_cases": bad,
        "cases": cases,
    }
    return report


def compare(current, baseline):
    if not baseline or baseline.get("run_id") == current.get("run_id"):
        return None
    deltas = {}
    for key in HEADLINE + ["goal_completion_rate"]:
        a, b = baseline["metrics"].get(key, {}).get("value"), current["metrics"].get(key, {}).get("value")
        deltas[key] = {"baseline": a, "current": b, "delta": None if a is None or b is None else round(b - a, 4)}
    old = {c["id"]: c["passed"] for c in baseline["cases"]}
    new = {c["id"]: c["passed"] for c in current["cases"]}
    return {"baseline_run_id": baseline["run_id"], "baseline_commit": baseline.get("git_commit"),
            "baseline_dataset_sha256": baseline["dataset"]["sha256"],
            "same_dataset": baseline["dataset"]["sha256"] == current["dataset"]["sha256"],
            "metrics": deltas,
            "fixed_cases": sorted(k for k in new if new[k] and old.get(k) is False),
            "regressed_cases": sorted(k for k in new if not new[k] and old.get(k) is True),
            "bad_cases_baseline": baseline["bad_case_summary"]["total"],
            "bad_cases_current": current["bad_case_summary"]["total"]}


def _pct(m):
    return "n/a" if m.get("value") is None else f"{m['value'] * 100:.1f}% ({m['numerator']}/{m['denominator']})"


def _bad_case_md(bad_cases):
    lines = []
    for b in bad_cases:
        lines += [f"### {b['case_id']} · {b['category']} · {b['title']}", "",
                  "- User: " + " → ".join(f"“{u}”" if isinstance(u, str) else json.dumps(u, ensure_ascii=False) for u in b["user_turns"]),
                  f"- Expected: {b['expected']['behaviour']}",
                  "- Actual: actions " + (", ".join(f"{a['object']}/{a['operation']}={a['status']}" for a in b["actual"]["actions"]) or "none")
                  + f"; routes {b['actual']['routes']}",
                  f"- Failed metrics: {', '.join(b['failed_metrics'])}",
                  f"- Failure type: **{b['failure_type']}**" + (f"（另有 {', '.join(b['secondary_failure_types'])}）" if b["secondary_failure_types"] else ""),
                  f"- Root cause: {b['root_cause']}",
                  "- Evidence:"] + [f"  - {e}" for e in b["evidence"]] + [
                  f"- Final business state: `{json.dumps(b['final_business_state'], ensure_ascii=False)}`", ""]
    return lines


def _breakdown_md(summary):
    lines = ["| failure_type | 数量 | 占失败 | 占全部 |", "|---|---|---|---|"]
    for k, v in summary["by_failure_type"].items():
        lines.append(f"| {k} | {v['count']} | {v['share_of_bad_cases'] * 100:.1f}% | {v['share_of_all_cases'] * 100:.1f}% |")
    lines += ["", "可由证据自动判定的 root cause（其余标 NEEDS_REVIEW，不让模型编造）：", ""]
    lines += [f"- `{k}`：{v}" for k, v in summary["root_causes"].items()]
    return lines


def markdown(report, baseline=None, baseline_report=None, holdout=None, fix_log=None, memory=None):
    m = report["metrics"]
    lines = ["# Product Evaluation · Recruiting Demo Commerce chain", "",
             f"> 自动生成，请勿手改。生成时间 {report['generated_at']} · run `{report['run_id']}` · commit `{report['git_commit']}`"
             + (" (working tree dirty)" if report["git_dirty"] else "") + f" · 模式 **{report['mode']}**", "",
             "## 评测范围", "",
             f"- 链路：{report['scope']['chain']}",
             f"- 判定：{report['scope']['judgement']}",
             f"- 存储/检索：{report['scope']['storage']}；{report['scope']['retrieval']}",
             "- 全部业务为模拟数据：没有真实支付、真实订单或生产流量。",
             f"- 数据集：`{report['dataset']['path']}`，{report['dataset']['case_count']} 条，sha256 `{report['dataset']['sha256'][:12]}`",
             "", "| 类别 | Case 数 | 通过 | 通过率 |", "|---|---|---|---|"]
    for cat, c in report["category_breakdown"].items():
        lines.append(f"| {cat} | {c['total']} | {c['passed']} | {c['rate'] * 100:.1f}% |")
    u = m["unauthorized_operations"]
    t = m["avg_turns_to_resolution"]
    lines += ["", "## Product Metrics", "", "| 指标 | 结果 | 计算方式 |", "|---|---|---|",
              f"| Task Success Rate | {_pct(m['task_success_rate'])} | {m['task_success_rate']['definition']} |",
              f"| Object Resolution Accuracy | {_pct(m['object_resolution_accuracy'])} | {m['object_resolution_accuracy']['definition']} |",
              f"| Confirmation Compliance | {_pct(m['confirmation_compliance'])} | {m['confirmation_compliance']['definition']} |",
              f"| Unauthorized Operations | {u['value']}（越权尝试 {u['attack_attempts']} 次，拦截 {u['attacks_blocked']} 次；无确认写入 {u['unconfirmed_writes']}） | {u['definition']} |",
              f"| Multi-intent Completion | {_pct(m['multi_intent_completion_rate'])} | {m['multi_intent_completion_rate']['definition']} |",
              f"| Clarification Accuracy | {_pct(m['clarification_accuracy'])}；precision {m['clarification_accuracy']['precision']}，recall {m['clarification_accuracy']['recall']} | {m['clarification_accuracy']['definition']} |",
              f"| Action / Tool Success | {_pct(m['action_tool_success_rate'])} | {m['action_tool_success_rate']['definition']} |",
              f"| Avg Turns to Resolution | {t['value']}（P50 {t['p50']}，P90 {t['p90']}，n={t['n']}） | {t['definition']} |",
              f"| Goal Completion（目标级） | {_pct(m['goal_completion_rate'])} | {m['goal_completion_rate']['definition']} |",
              ""]
    if baseline:
        lines += ["## Baseline → Current", "",
                  f"Baseline run `{baseline['baseline_run_id']}`（commit `{baseline['baseline_commit']}`），"
                  + ("同一数据集（sha256 一致）。" if baseline["same_dataset"] else "**数据集不同，结果不可直接比较。**"), "",
                  "| 指标 | Baseline | Current | Δ |", "|---|---|---|---|"]
        for k, d in baseline["metrics"].items():
            fmt = (lambda v: "n/a" if v is None else (str(v) if k in {"unauthorized_operations", "avg_turns_to_resolution"} else f"{v * 100:.1f}%"))
            delta = "n/a" if d["delta"] is None else (f"{d['delta']:+}" if k in {"unauthorized_operations", "avg_turns_to_resolution"} else f"{d['delta'] * 100:+.1f} pp")
            lines.append(f"| {k} | {fmt(d['baseline'])} | {fmt(d['current'])} | {delta} |")
        lines += ["", "> 注意：Current 是看着这 80 条的 Bad Case 修复之后，在同一数据集上的复测结果，会高估泛化能力；泛化请看下方 Holdout。", "",
                  f"- 修复后通过的 Case：{', '.join(baseline['fixed_cases']) or '无'}",
                  f"- 回退的 Case：{', '.join(baseline['regressed_cases']) or '无'}",
                  f"- Bad Case：{baseline['bad_cases_baseline']} → {baseline['bad_cases_current']}", ""]
    if holdout and holdout.get("current"):
        hc, hb = holdout["current"], holdout.get("baseline")
        lines += ["## Holdout（修复前写好、修复时未参考）", "",
                  f"`{hc['dataset']['path']}`，{hc['dataset']['case_count']} 条口语化改写，覆盖同类能力。主集上的修复是看着失败 Case 做的，存在过拟合风险；holdout 用来检查修复是否泛化。", "",
                  "| 指标 | Holdout baseline | Holdout current |", "|---|---|---|"]
        for k in ["task_success_rate", "object_resolution_accuracy", "multi_intent_completion_rate", "clarification_accuracy",
                  "action_tool_success_rate", "confirmation_compliance"]:
            lines.append(f"| {k} | {_pct(hb['metrics'][k]) if hb else 'n/a'} | {_pct(hc['metrics'][k])} |")
        lines += ["", f"Holdout 仍失败：{', '.join(b['case_id'] + ' ' + b['failure_type'] + ' · ' + b['root_cause'] for b in hc['bad_cases']) or '无'}", ""]
    if memory and memory.get("current"):
        mc, mb = memory["current"], memory.get("baseline")
        lines += ["## 记忆与个性化评测", "",
                  f"`{mc['dataset']['path']}`，{mc['dataset']['case_count']} 条：跨会话指代、跨会话进度、偏好学习 / 保留 / 纠正、临时要求不入记忆、界面设置偏好、遗忘、记忆不构成授权、敏感信息脱敏、记忆摘要、偏好不影响业务判断。", "",
                  f"- Baseline：{_pct(mb['metrics']['task_success_rate']) if mb else 'n/a'}；修复后：{_pct(mc['metrics']['task_success_rate'])}",
                  "- 失败（修复前）：" + (", ".join(b["case_id"] + " " + b["root_cause"] for b in mb["bad_cases"]) if mb else "n/a"),
                  "- 说明：MEM-09 的 requires_clarification 在任何系统修改前由 false 更正为“不标注”（原意即不评价是否澄清，只评价不授权），随后重跑并保存 baseline。该套件同样是看着失败修复的，规模小，未另设 holdout。", ""]
    if fix_log:
        lines += ["## 针对 Bad Case 的修复", "", "| 针对的 root cause | 修复 | 文件 |", "|---|---|---|"]
        lines += [f"| {', '.join(f['root_causes'])} | {f['fix']} | {', '.join(f'`{x}`' for x in f['files'])} |" for f in fix_log["fixes"]]
        lines.append("")
    s = report["bad_case_summary"]
    lines += ["## 当前 Bad Case", "", f"当前共 {s['total']} 个失败 Case。", ""] + _breakdown_md(s) + [""] + _bad_case_md(report["bad_cases"])
    if baseline_report and baseline_report["run_id"] != report["run_id"]:
        bs = baseline_report["bad_case_summary"]
        lines += ["## Baseline Bad Case 分析（修复前第一次评测）", "", f"Baseline 共 {bs['total']} 个失败 Case。", ""]
        lines += _breakdown_md(bs) + [""] + _bad_case_md(baseline_report["bad_cases"])
    lines += ["## 复现", "", "```bash", "cd ResolveFlow", "PYTHONPATH=. python scripts/run_product_eval.py            # offline rules，确定性，无需 API Key",
              "PYTHONPATH=. python scripts/run_product_eval.py --live     # 需 AGENT_USE_LLM=1 与 LLM_* 环境变量", "```", ""]
    return "\n".join(lines)


def paths(dataset="main"):
    if dataset == "main":
        return LATEST, BASELINE
    return REPORTS / f"product_eval_{dataset}_latest.json", REPORTS / f"product_eval_{dataset}_baseline.json"


def write(report, save_baseline=False, write_doc=True, dataset="main"):
    latest, baseline_path = paths(dataset)
    REPORTS.mkdir(parents=True, exist_ok=True)
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else None
    if save_baseline:
        baseline_path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
        baseline = None
    report["baseline_comparison"] = compare(report, baseline)
    latest.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    if write_doc and dataset == "main":
        write_doc_file()
    return report


def write_doc_file():
    """Regenerate docs/product-evaluation.md from the persisted reports only."""
    load = lambda p: json.loads(p.read_text()) if p.exists() else None
    current, base = load(LATEST), load(BASELINE)
    if not current:
        return
    h_latest, h_base = paths("holdout")
    fix_log = load(ROOT / "data/product_eval/fix_log.json")
    m_latest, m_base = paths("memory")
    DOC.write_text(markdown(current, current.get("baseline_comparison"), base,
                            {"current": load(h_latest), "baseline": load(h_base)}, fix_log,
                            {"current": load(m_latest), "baseline": load(m_base)}))
