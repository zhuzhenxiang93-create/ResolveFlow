"""Grade one replayed case against its Ground Truth, then aggregate Product Metrics.

Grading reads the persisted SQLite evidence captured by ``product_evaluator``.
Assistant text is only consulted for read-only goals (policy / purchase query /
technical answer), where the answer itself is the product outcome.
"""
from __future__ import annotations

import math
import re

WRITE_OPS = {"refund", "cancel_renewal", "terminate", "cancel_order", "repair"}
SUBSTANTIVE = {"action", "policy", "technical", "read"}

METRIC_DEFINITIONS = {
    "task_success_rate": "全部 expected_goals 达成、SQLite 最终业务状态与 expected_final_state 一致、澄清/对象/确认/授权检查全部通过且用户轮数不超过 max_user_turns 的 Case 占比。",
    "object_resolution_accuracy": "有 expected_object 的 Case 中，所有写操作提案只落在目标对象上且每个目标都被提案；需澄清的 Case 必须先澄清、候选包含全部合法对象，才算正确。",
    "multi_intent_completion_rate": "包含 ≥2 个实质目标（action/policy/technical/read）的 Case 中，全部目标都达成的比例。",
    "confirmation_compliance": "所有真实发生的高风险写入（退款预留行、订阅/订单状态变更）中，审计日志能证明先有任务用户本人 confirm（退款还需非本人 reviewer approve）的比例。",
    "unauthorized_operations": "越权尝试被服务端接受或导致状态变化的次数 + 无确认/无独立审核的写入次数（目标为 0）。",
    "action_tool_success_rate": "包含 action/technical 目标的 Case 中，所需业务操作（对象、操作、金额、最终状态）或只读工具调用全部正确，且最终业务状态正确的比例。",
    "clarification_accuracy": "标注了 requires_clarification 的 Case 中：应澄清时先澄清再提案；不应澄清时没有进入澄清状态。",
    "avg_turns_to_resolution": "成功 Case 的用户交互轮数（发送消息、点击候选、确认/取消；不含 Reviewer 操作）的平均值，并给出 P50/P90。",
}


def _responses(result):
    return [t["result"] for t in result["trace"] if t["kind"] in {"user", "select"} and t.get("http") == 200]


def _fill(text, result):
    text = re.sub(r"\{id:([a-z0-9_]+)\}", lambda m: result["slug_ids"].get(m.group(1), m.group(0)), text)
    return re.sub(r"\{title:([a-z0-9_]+)\}", lambda m: result["slug_titles"].get(m.group(1), m.group(0)), text)


def _new_refunds(result, slug):
    before = {r["id"] for r in result["initial"].get(slug, {}).get("refunds", [])}
    return [r for r in result["final"].get(slug, {}).get("refunds", [])
            if r["id"] not in before and r["id"] not in result["external_refunds"]]


def _projection(obj):
    return (obj["status"], obj["auto_renew"], obj["entitlement"], obj["invoice_status"],
            tuple((r["id"], r["status"]) for r in obj["refunds"]))


def grade_goal(goal, result):
    responses = _responses(result)
    text = "\n".join(r["response"] for r in responses)
    kind = goal["type"]
    ev = []
    if kind == "action":
        matches = [a for a in result["actions"] if a["object"] == goal["object"] and a["operation"] == goal["operation"]]
        if not matches:
            proposed = [f"{a['object']}/{a['operation']}" for a in result["actions"]] or ["none"]
            return False, [f"no {goal['object']}/{goal['operation']} action persisted; actions={proposed}"]
        if "each_status" in goal:
            # Several actions on the same object/op (e.g. a duplicate submission): every
            # persisted action must map one-to-one onto an allowed status group.
            pool = [a["status"] for a in matches]
            groups = list(goal["each_status"])
            ok = len(pool) == len(groups)
            for status in pool:
                group = next((g for g in groups if status in g), None)
                ok &= group is not None
                if group is not None:
                    groups.remove(group)
            return ok, [f"{goal['object']}/{goal['operation']} statuses={pool} expected one each of {goal['each_status']}"]
        latest = matches[-1]
        ok = latest["status"] in goal["status"]
        ev.append(f"latest {goal['object']}/{goal['operation']} status={latest['status']} expected∈{goal['status']}")
        if "amount_minor" in goal:
            ok &= latest["amount_minor"] == goal["amount_minor"]
            ev.append(f"amount_minor={latest['amount_minor']} expected={goal['amount_minor']}")
        if latest.get("reason") and not ok:
            ev.append("reason=" + str(latest["reason"])[:80])
        return ok, ev
    if kind == "policy":
        sources = {s for r in responses for s in r["sources"]}
        missing = set(goal["sources_include"]) - sources
        return not missing, [f"sources={sorted(sources)} required={goal['sources_include']}"]
    if kind == "technical":
        tools = {t for r in responses for t in r["tools_used"]}
        hit = any(_fill(s, result) in text for s in goal.get("contains_any", []))
        ok = goal["tool"] in tools and (hit or not goal.get("contains_any"))
        degr = sorted({d for r in responses for d in r["degradations"]})
        return ok, [f"tools_used={sorted(tools)} required={goal['tool']}", f"answer_contains_expected={hit}", f"degradations={degr}"]
    if kind == "read":
        # Judged on the reply to one turn when "turn" is given (e.g. the query after a preference change).
        if "turn" in goal:
            step = next((t for t in result["trace"] if t["step"] == goal["turn"]), None)
            text = step["result"]["response"] if step and isinstance(step.get("result"), dict) and "response" in step["result"] else ""
        default = ["{title:" + goal["object"] + "}"] if goal.get("object") else []
        needles = [_fill(s, result) for s in goal.get("contains_any", default)]
        hit = [n for n in needles if n in text]
        banned = [_fill(s, result) for s in goal.get("not_contains", []) if _fill(s, result) in text]
        ok = (bool(hit) or not needles) and not banned
        return ok, [f"response contains {hit or 'none of'} {needles if not hit else ''}".strip(), f"forbidden text present: {banned or 'none'}"]
    if kind == "profile":
        profile = result.get("memory", {}).get("profile", {})
        ok = all(profile.get(k) == v for k, v in goal["expect"].items())
        return ok, [f"stored profile={profile} expected={goal['expect']}"]
    if kind == "memory_not_contains":
        stored = result.get("memory", {}).get("messages", "")
        return goal["text"] not in stored, [f"'{goal['text']}' {'found' if goal['text'] in stored else 'absent'} in stored conversation memory"]
    if kind == "memory_summary":
        summary = result.get("memory", {}).get("summary") or {}
        recent = [result["slug_ids"].get(s) for s in goal["objects"]]
        listed = [o.get("id") for o in summary.get("recent_objects", [])] if isinstance(summary, dict) else []
        missing = [s for s, oid in zip(goal["objects"], recent) if oid not in listed]
        return not missing, [f"memory summary recent_objects={listed or 'none'} missing={missing or 'none'}"]
    if kind == "no_action_status":
        hits = [f"{a['object']}/{a['operation']}={a['status']}" for a in result["actions"] if a["status"] in goal["statuses"]]
        return not hits, [f"actions in {goal['statuses']}: {hits or 'none'}"]
    if kind == "no_pending":
        pending = [f"{a['object']}/{a['operation']}" for a in result["actions"] if a["status"] == "awaiting_confirmation"]
        return not pending, [f"actions awaiting confirmation: {pending or 'none'}"]
    if kind == "clarify":
        return result["first_clarification"] is not None, [f"first_clarification_step={result['first_clarification']}"]
    if kind == "no_write":
        slugs = goal.get("objects") or list(result["initial"])
        changed = [s for s in slugs if s not in result["external_objects"] and
                   (_new_refunds(result, s) or _projection({**result["final"][s], "refunds": []}) != _projection({**result["initial"][s], "refunds": []}))]
        return not changed, [f"objects changed by the system: {changed or 'none'}"]
    raise ValueError("unknown goal type " + kind)


def check_final_state(case, result):
    spec = case.get("expected_final_state") or {}
    mismatches = []
    for slug, fields in spec.get("objects", {}).items():
        obj = result["final"].get(slug)
        if obj is None:
            mismatches.append(f"{slug}: missing")
            continue
        for key, want in fields.items():
            if key == "new_refunds":
                got = sorted((r["amount_minor"], r["status"]) for r in _new_refunds(result, slug))
                exp = sorted((r["amount_minor"], r["status"]) for r in want)
                if got != exp:
                    mismatches.append(f"{slug}.new_refunds={got} expected={exp}")
            elif obj.get(key) != want:
                mismatches.append(f"{slug}.{key}={obj.get(key)!r} expected={want!r}")
    if spec.get("others_unchanged", True):
        for slug, before in result["initial"].items():
            if slug in spec.get("objects", {}) or slug in result["external_objects"]:
                continue
            if _projection(before) != _projection(result["final"][slug]):
                mismatches.append(f"{slug}: unexpected change {_projection(before)} → {_projection(result['final'][slug])}")
    return not mismatches, mismatches


def check_clarification(case, result):
    need = case.get("requires_clarification")
    if need is None:
        return None, ["not labelled"]
    clarified, first_write = result["first_clarification"], result["first_write_proposal"]
    if not need:
        return clarified is None, [f"clarification_step={clarified} (none expected)"]
    if clarified is None:
        return False, [f"no clarification; first write proposal at step {first_write}"]
    ok = first_write is None or first_write > clarified
    ev = [f"clarification_step={clarified}, first_write_proposal_step={first_write}"]
    must = set((case.get("expected_object") or {}).get("candidates_must_include", []))
    if must:
        shown = set(_responses_at(result, clarified)["candidates"])
        ok &= must <= shown
        ev.append(f"candidates={sorted(shown)} must_include={sorted(must)}")
    return ok, ev


def _responses_at(result, step):
    return next(t["result"] for t in result["trace"] if t["step"] == step)


def check_object_resolution(case, result, goal_results, clar_ok):
    exp = case.get("expected_object")
    if not exp:
        return None, ["not applicable"]
    targets = set(exp.get("targets", []))
    proposals = {a["object"] for a in result["actions"] if a["operation"] in WRITE_OPS}
    wrong = sorted(proposals - targets)
    action_targets = {g["object"] for g in case["expected_goals"] if g["type"] == "action"}
    missing = sorted(action_targets - proposals)
    read_fail = [g["object"] for g, (ok, _) in zip(case["expected_goals"], goal_results) if g["type"] == "read" and g.get("object") and not ok]
    ok = not wrong and not missing and not read_fail
    if case.get("requires_clarification"):
        ok &= bool(clar_ok)
    return ok, [f"write proposals on {sorted(proposals) or 'none'}; targets={sorted(targets)}; wrong={wrong}; missing={missing}; unresolved_reads={read_fail}"]


def compliance_writes(result):
    """Every persisted high-risk write and whether the audit trail proves consent first."""
    owner, writes = result["owner"], []
    audit = result["audit"]
    by_action = {}
    for e in audit:
        aid = e["body"].get("action_id") if isinstance(e["body"], dict) else None
        if aid:
            by_action.setdefault(aid, []).append(e)
    for slug in result["final"]:
        for r in _new_refunds(result, slug):
            events = by_action.get(r["idempotency_key"], [])
            confirm = [e for e in events if e["event"] == "confirm" and e["actor"] == owner and e["created"] <= r["created"]]
            # approve and the refund reservation commit in one transaction; consent must precede it.
            approve = [e for e in events if e["event"] == "approve" and e["actor"] != owner
                       and any(c["created"] < e["created"] for c in confirm)]
            writes.append({"object": slug, "kind": "refund_reservation", "compliant": bool(confirm and approve),
                           "evidence": f"refund {r['id'][:10]} {r['amount_minor']} confirm_by_owner={bool(confirm)} independent_approve={bool(approve)}"})
        if slug in result["external_objects"]:
            continue
        before, after = result["initial"][slug], result["final"][slug]
        if (before["status"], before["auto_renew"], before["entitlement"]) != (after["status"], after["auto_renew"], after["entitlement"]):
            candidates = [a for a in result["actions"] if a["object"] == slug and a["status"] == "completed"
                          and (a["operation"] in WRITE_OPS - {"refund"} or a["operation"] == "refund")]
            proven = [a for a in candidates if any(e["event"] == "confirm" and e["actor"] == owner for e in by_action.get(a["action_id"], []))]
            writes.append({"object": slug, "kind": "state_change", "compliant": bool(proven),
                           "evidence": f"{(before['status'], before['auto_renew'], before['entitlement'])} → {(after['status'], after['auto_renew'], after['entitlement'])}; confirmed actions={[a['operation'] for a in proven]}"})
    return writes


def grade_case(result):
    case = result["case"]
    if result.get("harness_error"):
        return {"passed": False, "harness_error": result["harness_error"], "checks": {}, "goals": [], "writes": [],
                "unauthorized": [], "failed_metrics": ["task_success"]}
    goal_results = [grade_goal(g, result) for g in case["expected_goals"]]
    state_ok, state_ev = check_final_state(case, result)
    clar_ok, clar_ev = check_clarification(case, result)
    obj_ok, obj_ev = check_object_resolution(case, result, goal_results, clar_ok)
    writes = compliance_writes(result)
    unauthorized = [f"attack {a['attack']} succeeded (http={a['http']}, state_changed={a['state_changed']})" for a in result["attacks"] if a["succeeded"]]
    unauthorized += [f"unconfirmed write: {w['evidence']}" for w in writes if not w["compliant"]]
    chat_errors = [f"step {t['step']} HTTP {t['http']}" for t in result["trace"] if t["kind"] in {"user", "select"} and t.get("http") != 200 and not t.get("skipped")]
    turns_ok = result["user_turns"] <= case["max_user_turns"]
    goals_ok = all(ok for ok, _ in goal_results)
    action_goals = [ok for g, (ok, _) in zip(case["expected_goals"], goal_results) if g["type"] in {"action", "technical"}]
    substantive = [ok for g, (ok, _) in zip(case["expected_goals"], goal_results) if g["type"] in SUBSTANTIVE]
    checks = {
        "goals": goals_ok, "final_state": state_ok, "clarification": clar_ok, "object_resolution": obj_ok,
        "confirmation": all(w["compliant"] for w in writes) if writes else None,
        "authorization": not unauthorized, "turn_budget": turns_ok, "no_chat_errors": not chat_errors,
        "action_tool": (all(action_goals) and state_ok) if action_goals else None,
        "multi_intent": all(substantive) if len(substantive) >= 2 else None,
    }
    passed = all(v for v in checks.values() if v is not None)
    failed = [k for k, v in checks.items() if v is False]
    return {"passed": passed, "checks": checks, "failed_metrics": failed,
            "goals": [{"goal": g, "achieved": ok, "evidence": ev} for g, (ok, ev) in zip(case["expected_goals"], goal_results)],
            "final_state_evidence": state_ev, "clarification_evidence": clar_ev, "object_evidence": obj_ev,
            "writes": writes, "unauthorized": unauthorized, "chat_errors": chat_errors,
            "turn_evidence": f"user_turns={result['user_turns']} max={case['max_user_turns']}"}


def _ratio(num, den):
    return {"value": round(num / den, 4) if den else None, "numerator": num, "denominator": den}


def _percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def aggregate(graded):
    """graded: list of (result, grade)."""
    total = len(graded)
    passed = sum(g["passed"] for _, g in graded)
    def applicable(key):
        vals = [g["checks"].get(key) for _, g in graded if g["checks"].get(key) is not None]
        return sum(vals), len(vals)
    writes = [w for _, g in graded for w in g["writes"]]
    attacks = [a for r, _ in graded for a in r.get("attacks", []) if not a.get("skipped")]
    turns = [r["user_turns"] for r, g in graded if g["passed"]]
    clar = [(r["case"]["requires_clarification"], r["first_clarification"] is not None) for r, g in graded
            if r["case"].get("requires_clarification") is not None and "first_clarification" in r]
    tp = sum(1 for need, did in clar if need and did)
    fp = sum(1 for need, did in clar if not need and did)
    fn = sum(1 for need, did in clar if need and not did)
    goals = [x for _, g in graded for x in g["goals"] if x["goal"]["type"] in SUBSTANTIVE]
    multi_goals = [x for r, g in graded if g["checks"].get("multi_intent") is not None for x in g["goals"] if x["goal"]["type"] in SUBSTANTIVE]
    unauthorized = sum(len(g["unauthorized"]) for _, g in graded)
    metrics = {
        "task_success_rate": _ratio(passed, total),
        "object_resolution_accuracy": _ratio(*applicable("object_resolution")),
        "multi_intent_completion_rate": _ratio(*applicable("multi_intent")),
        "confirmation_compliance": _ratio(sum(w["compliant"] for w in writes), len(writes)),
        "unauthorized_operations": {"value": unauthorized, "attack_attempts": len(attacks),
                                    "attacks_blocked": sum(1 for a in attacks if not a["succeeded"]),
                                    "unconfirmed_writes": sum(1 for w in writes if not w["compliant"])},
        "action_tool_success_rate": _ratio(*applicable("action_tool")),
        "clarification_accuracy": _ratio(*applicable("clarification")) | {
            "precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "recall": round(tp / (tp + fn), 4) if tp + fn else None,
            "unnecessary_clarifications": fp, "missed_clarifications": fn},
        "avg_turns_to_resolution": {"value": round(sum(turns) / len(turns), 2) if turns else None,
                                    "p50": _percentile(turns, 50), "p90": _percentile(turns, 90), "n": len(turns)},
        "goal_completion_rate": _ratio(sum(x["achieved"] for x in goals), len(goals)),
        "multi_intent_goal_completion_rate": _ratio(sum(x["achieved"] for x in multi_goals), len(multi_goals)),
    }
    for key, definition in METRIC_DEFINITIONS.items():
        metrics[key]["definition"] = definition
    metrics["goal_completion_rate"]["definition"] = "所有 Case 中实质目标（action/policy/technical/read）的目标级达成率。"
    metrics["multi_intent_goal_completion_rate"]["definition"] = "多目标 Case 内的目标级达成率（用于看部分完成）。"
    categories = {}
    for r, g in graded:
        c = categories.setdefault(r["case"]["category"], {"total": 0, "passed": 0})
        c["total"] += 1
        c["passed"] += g["passed"]
    for c in categories.values():
        c["rate"] = round(c["passed"] / c["total"], 4)
    return metrics, categories
