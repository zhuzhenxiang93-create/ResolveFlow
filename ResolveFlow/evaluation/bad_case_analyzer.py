"""Deterministic Bad Case classification for Product Evaluation.

Rules only use recorded evidence (SQLite state, audit, HTTP codes, response
metadata). A root cause is emitted only for patterns that can be proven from
that evidence; everything else is ``NEEDS_REVIEW`` — no model-generated guesses.
"""
from __future__ import annotations

FAILURE_TYPES = [
    "AUTHORIZATION", "CONFIRMATION_VIOLATION", "MISSING_CLARIFICATION", "UNNECESSARY_CLARIFICATION",
    "OBJECT_RESOLUTION", "TASK_PLANNING", "WRONG_ACTION", "STATE_VERIFICATION", "MEMORY", "RESPONSE_QUALITY",
    "HARNESS_ERROR",
]
FAILURE_LABELS = {
    "AUTHORIZATION": "Authorization", "CONFIRMATION_VIOLATION": "Confirmation Violation",
    "MISSING_CLARIFICATION": "Missing Clarification", "UNNECESSARY_CLARIFICATION": "Unnecessary Clarification",
    "OBJECT_RESOLUTION": "Object Resolution", "TASK_PLANNING": "Task Planning", "WRONG_ACTION": "Wrong Action",
    "STATE_VERIFICATION": "State Verification", "MEMORY": "Memory", "RESPONSE_QUALITY": "Response Quality", "HARNESS_ERROR": "Harness Error",
}


def _responses(result):
    return [t["result"] for t in result["trace"] if t["kind"] in {"user", "select"} and t.get("http") == 200]


def _chat_fallback_steps(result):
    return [t["step"] for t in result["trace"] if t["kind"] in {"user", "select"} and t.get("http") == 200
            and t["result"]["route"] == "chat"]


def _root_cause(result, failure_type, failed_goals):
    """Only patterns provable from recorded evidence; otherwise NEEDS_REVIEW."""
    responses = _responses(result)
    degradations = {d for r in responses for d in r["degradations"]}
    skipped = [t for t in result["trace"] if t.get("skipped")]
    fallback = _chat_fallback_steps(result)
    if failure_type == "MISSING_CLARIFICATION" and result["first_write_proposal"] is not None:
        return f"AUTO_SELECTED_AMBIGUOUS_OBJECT: 存在多个合法候选时，第 {result['first_write_proposal']} 步直接为其中一个对象生成了报价"
    if failure_type == "UNNECESSARY_CLARIFICATION":
        policy_goal = any(g["goal"]["type"] == "policy" for g in failed_goals)
        if policy_goal and not any(r["sources"] for r in responses):
            return "POLICY_QUESTION_TREATED_AS_ACTION: 政策咨询被当成售后操作，系统要求选择订单"
    if any(g["goal"]["type"] == "technical" for g in failed_goals):
        if "technical_agent_offline" in degradations:
            return "TECHNICAL_AGENT_UNAVAILABLE_OFFLINE: 离线模式下没有调用只读错误码工具，响应带 technical_agent_offline 降级标记"
        if fallback:
            return "TECHNICAL_QUESTION_NOT_ROUTED: 技术问题落入通用兜底回复，未调用只读工具"
    for g in failed_goals:
        goal = g["goal"]
        if goal["type"] == "action":
            matches = [a for a in result["actions"] if a["object"] == goal["object"] and a["operation"] == goal["operation"]]
            if not matches:
                ops_same_turn = [r for r in responses if r["case_operations"]]
                if fallback:
                    return f"INTENT_NOT_EXTRACTED: 第 {fallback} 步落入通用对话兜底(route=chat)，未产生业务意图"
                if "knowledge" in [r["route"] for r in responses] and not ops_same_turn:
                    return "ACTION_ROUTED_AS_POLICY_ONLY: 含操作请求的消息被整体判为政策咨询"
                if ops_same_turn:
                    return f"GOAL_DROPPED: 同一请求中 {goal['object']}/{goal['operation']} 没有生成操作，其他目标已生成"
                if any(r["status"] == "awaiting_clarification" for r in responses):
                    return "TARGET_NOT_RESOLVED: 系统发起澄清后，用户后续给出的目标没有被解析成业务对象"
                if any(r["route"] == "commerce" and r["status"] == "answered" for r in responses):
                    return "REQUEST_DOWNGRADED_TO_READ: 写操作请求被解析成查询，只返回了记录"
            else:
                latest = matches[-1]
                if latest["status"] == "awaiting_confirmation" and "cancelled" in goal["status"]:
                    return "REVISION_NOT_APPLIED: 用户撤回/修改目标后，原待确认操作仍保留"
                if fallback and fallback[-1] > 0:
                    return f"INTENT_NOT_EXTRACTED: 第 {fallback} 步落入通用对话兜底(route=chat)，后续请求未生成操作"
                if latest["status"] in {"stale", "ineligible"} and latest["status"] not in goal.get("status", []):
                    return f"BUSINESS_RULE_{latest['status'].upper()}: " + str(latest.get("reason"))[:80]
                if "amount_minor" in goal and latest["amount_minor"] != goal["amount_minor"]:
                    return f"WRONG_SCOPE_AMOUNT: 报价 {latest['amount_minor']} ≠ 期望 {goal['amount_minor']}（明细/账单范围解析错误）"
        if goal["type"] == "policy" and not any(r["sources"] for r in responses):
            routes = sorted({r["route"] for r in responses})
            return f"NO_POLICY_SOURCE: 回答没有附带政策来源 (routes={routes})"
        if goal["type"] == "profile":
            return f"PREFERENCE_NOT_STORED_OR_WRONG: 期望偏好 {goal['expect']}，实际 {result.get('memory', {}).get('profile')}"
        if goal["type"] == "memory_summary":
            return "MEMORY_NOT_SURFACED: 记忆摘要接口没有列出最近处理过的对象"
        if goal["type"] == "memory_not_contains":
            return "SENSITIVE_TEXT_STORED: 敏感内容被写入长期记忆"
        if goal["type"] == "no_pending":
            return "COMPLETED_STATE_NOT_CHECKED: 目标状态已达成，系统仍生成待确认操作"
    if failure_type == "MISSING_CLARIFICATION" and fallback:
        return f"INTENT_NOT_EXTRACTED: 第 {fallback} 步落入通用对话兜底(route=chat)，没有进入澄清"
    if any(t["kind"] == "select" for t in skipped):
        return "EXPECTED_CANDIDATE_NOT_DISPLAYED: 系统给出的候选列表不含用户要选的对象，模拟用户无法点击"
    return "NEEDS_REVIEW"


def _dropped(goal, result):
    """A goal of a multi-goal request left no trace at all (no action / source / tool / mention)."""
    responses = _responses(result)
    if goal["type"] == "action":
        return not any(a["object"] == goal["object"] and a["operation"] == goal["operation"] for a in result["actions"])
    if goal["type"] == "policy":
        return not any(r["sources"] for r in responses)
    if goal["type"] == "technical":
        return not any(r["tools_used"] or "technical_agent_offline" in r["degradations"] for r in responses)
    return False


def classify(result, grade):
    """Return the primary failure_type and the other failed dimensions."""
    if grade.get("harness_error"):
        return "HARNESS_ERROR", []
    case, checks = result["case"], grade["checks"]
    types = []
    if any("attack" in u for u in grade["unauthorized"]):
        types.append("AUTHORIZATION")
    if checks.get("confirmation") is False:
        types.append("CONFIRMATION_VIOLATION")
    if checks.get("clarification") is False:
        types.append("MISSING_CLARIFICATION" if case.get("requires_clarification") else "UNNECESSARY_CLARIFICATION")
    if checks.get("object_resolution") is False and "MISSING_CLARIFICATION" not in types:
        wrong = [a for a in result["actions"] if a["operation"] in {"refund", "cancel_renewal", "cancel_order", "repair"}
                 and a["object"] not in set((case.get("expected_object") or {}).get("targets", []))]
        if wrong:
            types.append("OBJECT_RESOLUTION")
    failed_goals = [g for g in grade["goals"] if not g["achieved"]]
    substantive = [g for g in grade["goals"] if g["goal"]["type"] in {"action", "policy", "technical", "read"}]
    if len(substantive) >= 2 and failed_goals and any(g["achieved"] for g in substantive):
        if any(_dropped(g["goal"], result) for g in failed_goals):
            types.append("TASK_PLANNING")
    for g in failed_goals:
        goal = g["goal"]
        if goal["type"] == "action":
            matches = [a for a in result["actions"] if a["object"] == goal["object"] and a["operation"] == goal["operation"]]
            later_fallback = [st for st in _chat_fallback_steps(result) if st > 0]
            types.append("WRONG_ACTION" if not matches or later_fallback else "STATE_VERIFICATION")
        elif goal["type"] in {"policy", "technical", "read"}:
            types.append("RESPONSE_QUALITY")
        elif goal["type"] in {"profile", "memory_not_contains", "memory_summary", "no_action_status"}:
            types.append("MEMORY")
        elif goal["type"] in {"no_write", "no_pending"}:
            types.append("WRONG_ACTION")
        elif goal["type"] == "clarify" and "MISSING_CLARIFICATION" not in types:
            types.append("MISSING_CLARIFICATION")
    if checks.get("final_state") is False:
        types.append("STATE_VERIFICATION")
    if checks.get("turn_budget") is False:
        types.append("TASK_PLANNING")
    if checks.get("no_chat_errors") is False:
        types.append("RESPONSE_QUALITY")
    ordered = [t for t in FAILURE_TYPES if t in types]
    if not ordered:
        return "NEEDS_REVIEW", []
    return ordered[0], ordered[1:]


def analyse(result, grade):
    case = result["case"]
    primary, secondary = classify(result, grade)
    failed_goals = [g for g in grade["goals"] if not g["achieved"]]
    root = "NEEDS_REVIEW" if primary == "HARNESS_ERROR" else _root_cause(result, primary, failed_goals)
    responses = _responses(result) if not grade.get("harness_error") else []
    touched = {a["object"] for a in result.get("actions", [])} | set((case.get("expected_final_state") or {}).get("objects", {}))
    final_state = {s: {k: o[k] for k in ("status", "auto_renew", "entitlement", "invoice_status")} |
                   {"refunds": [(r["amount_minor"], r["status"]) for r in o["refunds"]]}
                   for s, o in result.get("final", {}).items() if s in touched}
    evidence = []
    for g in failed_goals:
        evidence += [f"goal {g['goal']['type']}:{g['goal'].get('object', '')} ✗ " + "; ".join(g["evidence"])]
    evidence += [e for e in grade.get("final_state_evidence", [])]
    if grade["checks"].get("clarification") is False:
        evidence += grade["clarification_evidence"]
    if grade["checks"].get("object_resolution") is False:
        evidence += grade["object_evidence"]
    evidence += grade.get("unauthorized", []) + grade.get("chat_errors", [])
    if grade["checks"].get("turn_budget") is False:
        evidence.append(grade["turn_evidence"])
    evidence += [f"step {t['step']} skipped: {t['note']}" for t in result.get("trace", []) if t.get("skipped")]
    if grade.get("harness_error"):
        evidence.append(grade["harness_error"])
    return {
        "case_id": case["id"], "category": case["category"], "title": case.get("title", ""),
        "user_turns": [t.get("text") or t["spec"] for t in result.get("trace", []) if t["kind"] in {"user", "select", "confirm", "cancel"}],
        "expected": {"goals": case["expected_goals"], "expected_object": case.get("expected_object"),
                     "requires_clarification": case.get("requires_clarification"),
                     "requires_confirmation": case.get("requires_confirmation"),
                     "requires_review": case.get("requires_review"),
                     "expected_final_state": case.get("expected_final_state"), "max_user_turns": case["max_user_turns"],
                     "behaviour": case.get("expected_behaviour", "")},
        "actual": {"actions": [{k: a[k] for k in ("object", "operation", "status", "amount_minor")} for a in result.get("actions", [])],
                   "clarification_step": result.get("first_clarification"),
                   "first_write_proposal_step": result.get("first_write_proposal"),
                   "routes": [r["route"] for r in responses],
                   "last_response": (responses[-1]["response"][:300] if responses else ""),
                   "user_turns_used": result.get("user_turns")},
        "failed_metrics": grade["failed_metrics"], "failure_type": primary, "secondary_failure_types": secondary,
        "root_cause": root, "evidence": evidence, "final_business_state": final_state, "result": "FAILED",
    }


def breakdown(bad_cases, total_cases):
    counts = {t: 0 for t in FAILURE_TYPES}
    for b in bad_cases:
        counts[b["failure_type"]] = counts.get(b["failure_type"], 0) + 1
    n = len(bad_cases)
    return {t: {"label": FAILURE_LABELS.get(t, t), "count": c,
                "share_of_bad_cases": round(c / n, 4) if n else 0.0,
                "share_of_all_cases": round(c / total_cases, 4) if total_cases else 0.0}
            for t, c in counts.items() if c or t in {"OBJECT_RESOLUTION", "TASK_PLANNING", "MISSING_CLARIFICATION",
                                                    "RESPONSE_QUALITY", "CONFIRMATION_VIOLATION", "AUTHORIZATION"}}
