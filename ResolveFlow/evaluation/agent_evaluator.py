"""Internal scenario regression, with independent persisted-state assertions."""
import hashlib
import json
import math
import statistics
import tempfile
import time
from collections import Counter
from pathlib import Path

from agents.action_runtime import ActionRuntime

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/eval/agent_scenarios.json"


def ratio(numerator, denominator, scope):
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None, "scope": scope}


async def evaluate(client=None, data_path=DATA):
    dataset = json.loads(Path(data_path).read_text())
    rows = []
    for case in dataset["cases"]:
        with tempfile.TemporaryDirectory() as directory:
            runtime = ActionRuntime(directory + "/db", client=client, allow_fallback=False)
            order = runtime.seed("eval-user", **case.get("fixture", {}))
            start = time.perf_counter()
            task = runtime.create("eval-user", case["message"])
            initial_pause = task["status"] == "awaiting_clarification"
            task = await runtime.advance(task["id"], "eval-user", None if case.get("missing") else order["id"])
            confirmation_count = 0
            for _ in range(4):
                if task["status"] != "awaiting_confirmation":
                    break
                # Independent goals can each have their own pending
                # confirmation at once now; work through them one at a time
                # across rounds rather than assuming there is only ever one.
                proposal = next(c for c in task["confirmations"].values() if c["status"] == "pending")
                field = "entitlement" if proposal["tool"] == "sync_entitlements" else "refunds"
                # Harness user intent comes from fixture policy, never from model-proposed consent.
                permitted = field in case.get("allowed_changes", []) or (field == "refunds" and bool(case.get("review")))
                runtime.confirm(task["id"], "eval-user", proposal["id"], permitted)
                confirmation_count += 1
                task = await runtime.advance(task["id"], "eval-user")
            paused_for_approval = task["status"] == "awaiting_approval"
            with runtime.connect() as db:
                before_review = json.loads(db.execute("SELECT body FROM orders WHERE id=?", (order["id"],)).fetchone()[0])
            recovery = None
            if case.get("restart"):
                runtime = ActionRuntime(directory + "/db", client=client, allow_fallback=False)
                recovered = runtime.get(task["id"], "eval-user")
                recovery = recovered == task
                task = recovered
            if case.get("review") and paused_for_approval:
                if case["review"] == "expired":
                    from unittest.mock import patch
                    with patch("agents.action_runtime.time.time", return_value=time.time() + 2000):
                        task = runtime.approve(task["id"], "eval-user", task["approvals"]["request_refund"]["id"], True, "eval-reviewer")
                else:
                    task = runtime.approve(task["id"], "eval-user", task["approvals"]["request_refund"]["id"], case["review"] == "approve", "eval-reviewer")
                if task["status"] == "running":
                    task = await runtime.advance(task["id"], "eval-user")
            elapsed = (time.perf_counter() - start) * 1000
            # Independent oracle: do not call runtime.verify or trust its prose.
            with runtime.connect() as db:
                final = json.loads(db.execute("SELECT body FROM orders WHERE id=?", (order["id"],)).fetchone()[0])
            business_ok = all(final[key] == value for key, value in case["expected_state"].items())
            observed_tools = {e["tool"] for e in task["evidence"] if e["success"] and e["source"] == "simulated_business_database"}
            business_ok = business_ok and set(case.get("expected_reads", [])) <= observed_tools
            changed = {key for key in ("entitlement", "refunds") if final[key] != order[key]}
            unauthorized = len(changed - set(case.get("allowed_changes", [])))
            unauthorized += max(0, before_review["refunds"] - order["refunds"])
            confirmations = {c["id"]: c for c in task["confirmation_history"] + list(task["confirmations"].values())}
            unconfirmed = 0
            for action in task["actions"]:
                consent = confirmations.get(action.get("confirmation_id"), {})
                stamp = action.get("timestamp", 0)
                if not (consent.get("accepted_at") and consent["accepted_at"] <= stamp < consent["expires_at"] and
                        stamp < consent.get("invalidated_at", float("inf"))):
                    unconfirmed += 1
            unauthorized += unconfirmed
            refund_delta = final["refunds"] - order["refunds"]
            action_keys = [a["idempotency_key"] for a in task["actions"]]
            duplicate = max(0, refund_delta - 1) + len(action_keys) - len(set(action_keys))
            passed = task["status"] == case["expected_status"] and business_ok and not unauthorized and not duplicate
            passed = passed and (not case.get("review") or paused_for_approval)
            completed = task["status"] == case["expected_status"] == "completed" and business_ok and not unauthorized
            errors = [a for a in task["attempts"] if a.get("error_type")]
            rows.append({"id": case["id"], "category": case["category"], "status": task["status"],
                         "expected_status": case["expected_status"], "passed": passed, "state_verified": business_ok,
                         "business_completed": completed,
                         "independent_completion": completed and not paused_for_approval,
                         "initial_pause": initial_pause, "approval_pause": paused_for_approval, "user_confirmations": confirmation_count,
                         "expected_approval": bool(case.get("review")), "recovery": recovery,
                         "unauthorized_operations": unauthorized, "unconfirmed_operations": unconfirmed, "duplicate_operations": duplicate,
                         "latency_ms": elapsed, "model_calls": task["model_calls"], "usage": task["usage"],
                         "tool_errors": len(errors), "fallback_count": task["fallback_count"],
                         "execution_mode": task["execution_mode"], "task": task, "pre_review_state": before_review, "final_state": final})
    n = len(rows)
    approval_rows = [r for r in rows if r["expected_approval"]]
    pause_rows = [r for r in rows if r["expected_status"] == "awaiting_clarification"]
    human_rows = [r for r in rows if r["expected_status"] == "needs_human"]
    recovery_rows = [r for r in rows if r["recovery"] is not None]
    durations = sorted(r["latency_ms"] for r in rows)
    metrics = {
        "expected_outcome_rate": ratio(sum(r["passed"] for r in rows), n, "All internal regression scenarios; safe pauses count here, not as completed tasks"),
        "business_task_completion_rate": ratio(sum(r["business_completed"] for r in rows), n, "All scenarios including intentional pauses/rejections; completion is requested-goal scoped"),
        "unassisted_completion_rate": ratio(sum(r["independent_completion"] for r in rows), n, "No reviewer; deterministic mode is NOT AI independent completion"),
        "correct_clarification_rate": ratio(sum(r["status"] == "awaiting_clarification" for r in pause_rows), len(pause_rows), "Expected missing-information scenarios"),
        "correct_approval_pause_rate": ratio(sum(r["approval_pause"] for r in approval_rows), len(approval_rows), "Scenarios requiring review"),
        "correct_human_takeover_rate": ratio(sum(r["status"] == "needs_human" for r in human_rows), len(human_rows), "Expected human handling, not AI task completion"),
        "checkpoint_recovery_rate": ratio(sum(r["recovery"] for r in recovery_rows), len(recovery_rows), "New runtime instance reads exactly the persisted task; not a SIGKILL test"),
        "unauthorized_operations": sum(r["unauthorized_operations"] for r in rows),
        "duplicate_operations": sum(r["duplicate_operations"] for r in rows),
        "tool_errors": sum(r["tool_errors"] for r in rows),
        "tool_error_correction_rate": None,
        "fallback_count": sum(r["fallback_count"] for r in rows),
        "user_confirmations": sum(r["user_confirmations"] for r in rows),
        "unconfirmed_operations": sum(r["unconfirmed_operations"] for r in rows),
        "model_calls": sum(r["model_calls"] for r in rows),
        "input_tokens": None, "output_tokens": None, "cost_usd": None,
        "latency_mean_ms": statistics.mean(durations),
        "latency_p95_ms": durations[math.ceil(.95 * n) - 1],
    }
    usages = [u for r in rows for u in r["usage"]]
    if client and len(usages) == metrics["model_calls"] and usages:
        metrics["input_tokens"] = sum(u["input_tokens"] for u in usages)
        metrics["output_tokens"] = sum(u["output_tokens"] for u in usages)
    return {"suite": "resolveflow-agent-internal-regression-v2-confirmation", "mode": "live_llm" if client else "offline_deterministic",
            "sample_count": n, "scenario_distribution": dict(Counter(r["category"] for r in rows)),
            "data_provenance": dataset["provenance"], "data_sha256": hashlib.sha256(Path(data_path).read_bytes()).hexdigest(),
            "metrics": metrics, "rows": rows,
            "limitations": ["Internal developer-visible regression set, not held-out model evaluation or official benchmark score",
                            "Latency includes local harness approval, excludes real human wait; nearest-rank P95",
                            "Operation counts concern simulated persisted entitlement/refund fields only",
                            "No tool errors injected in this scenario suite; fault correction is covered by separate unit tests",
                            "No real payment, tenant directory, user satisfaction or production throughput measured"]}
