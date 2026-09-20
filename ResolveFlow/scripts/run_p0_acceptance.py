"""Frozen P0 acceptance with a campaign-wide paid-call ceiling."""
import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import platform
import tempfile
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values
from agents.action_runtime import ActionRuntime
from agents.agent_orchestrator import AgentOrchestrator
from agents.goal_interpreter import GoalInterpreter
from core.llm_client import LLMClient

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/eval/p0_acceptance_v1.json"
FROZEN_SHA = "e451f3b678d23d489ce8938fe0a9a5dfa90584204ef0771ef6a547fc31977e1d"


class BudgetClient:
    def __init__(self, client, ledger, limit):
        self.client, self.ledger, self.limit = client, ledger, limit
        self.calls, self.usage, self.denied = 0, [], 0

    async def create_tool_turn(self, **kwargs):
        if len(json.dumps(kwargs)) > 100000:
            raise ValueError("Acceptance input size exceeded")
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0)
            content = stream.read()
            state = json.loads(content) if content else {"limit": self.limit, "reserved_calls": 0}
            if state["limit"] != self.limit or state["reserved_calls"] >= state["limit"]:
                self.denied += 1
                raise RuntimeError("Campaign call budget exhausted or changed")
            state["reserved_calls"] += 1
            stream.seek(0)
            stream.truncate()
            stream.write(json.dumps(state))
            stream.flush()
            os.fsync(stream.fileno())
        self.calls += 1
        result = await self.client.create_tool_turn(**{**kwargs, "max_tokens": min(kwargs.get("max_tokens", 512), 512)})
        if result.get("_usage"):
            self.usage.append(result["_usage"])
        return result


def goals_match(proposal, case):
    if proposal is None:
        return False
    return bool(proposal.get("unsupported_requests")) if case.get("unsupported") else proposal["goals"] == case["goals"] and not proposal.get("unsupported_requests")


async def run_case(case, client, directory):
    r = ActionRuntime(Path(directory) / (case["id"] + ".sqlite3"), client=client, allow_fallback=False, model_timeout=30)
    start = time.monotonic()
    if case["kind"] == "semantic":
        context = {"goals": {}, "order_id": None, "unresolved": [], "status": "awaiting_clarification", "response": "", "messages": []}
        proposal, record = await GoalInterpreter(client, timeout=30).interpret(case["message"], context)
        parsed = proposal.model_dump() if proposal else None
        return {"passed": goals_match(parsed, case), "raw_model_correct": goals_match(record.get("original_proposal", parsed), case) if client else None,
                "record": record, "proposal": parsed, "latency_ms": (time.monotonic() - start) * 1000}
    owner = "isolated-user"
    order = r.seed(owner)
    steps = []
    t = await AgentOrchestrator.execute_action(r, owner=owner, message=case["message"])
    clarification_ok = t["status"] == "awaiting_clarification" and not t["actions"]
    t = await AgentOrchestrator.execute_action(r, owner=owner, task_id=t["id"], order_id=order["id"])
    recovered, access_denied, stale_denied = False, False, False
    for _ in range(8):
        steps.append({"status": t["status"], "response": t["response"], "verification": t["verification"],
                      "actions": len(t["actions"]), "confirmation_id": (t.get("confirmation") or {}).get("id")})
        if t["status"] == "awaiting_confirmation":
            if not access_denied:
                try:
                    r.confirm(t["id"], "wrong-user", t["confirmation"]["id"], True)
                except ValueError:
                    access_denied = True
            t = r.confirm(t["id"], owner, t["confirmation"]["id"], case["operation"] != "decline")
            if t["status"] == "running":
                t = await AgentOrchestrator.execute_action(r, owner=owner, task_id=t["id"])
        elif t["status"] == "awaiting_approval":
            aid = t["approval"]["id"]
            if case["operation"] == "recover" and not recovered:
                await r.advance(t["id"], owner, message="稍等，我需要人工重新核查本次任务")
                # Inject elapsed evidence age in the isolated store, not in the prompt.
                with r.connect() as db:
                    expired = r._load(db, t["id"], owner)
                    for evidence in expired["evidence"]:
                        evidence["timestamp"] -= r.evidence_ttl + 1
                    r._save(db, expired)
                r = ActionRuntime(r.path, client=client, allow_fallback=False, model_timeout=30)
                t = r.approve(t["id"], owner, aid, True, "isolated-reviewer")
                stale_denied = t["status"] == "needs_human" and not any(a["tool"] == "refund" for a in t["actions"])
                r.release_handoff(t["id"], owner, "isolated-reviewer")
                t = await AgentOrchestrator.execute_action(r, owner=owner, task_id=t["id"])
                recovered = True
            else:
                t = r.approve(t["id"], owner, aid, True, "isolated-reviewer")
                t = r.approve(t["id"], owner, aid, True, "isolated-reviewer")
                if t["status"] == "running":
                    t = await AgentOrchestrator.execute_action(r, owner=owner, task_id=t["id"])
        else:
            break
    with r.connect() as db:
        state = r._order(db, t)
    signatures = [a["idempotency_key"] for a in t["actions"]]
    consent_ids = {c["id"] for c in t.get("confirmation_history", []) + ([t["confirmation"]] if t.get("confirmation") else []) if c.get("accepted_at")}
    unauthorized = sum(a.get("confirmation_id") not in consent_ids for a in t["actions"])
    duplicates = len(signatures) - len(set(signatures))
    state_ok = (state["auto_renew"] is True and state["refunds"] == 0 and not t["actions"] if case["operation"] == "decline" else
                state["refunds"] == 1 and (case["operation"] != "approve" or state["entitlement"] == "pro"))
    bill_queries = sum(e["tool"] == "query_billing" and e["success"] for e in t["evidence"])
    recovery_ok = case["operation"] != "recover" or (recovered and stale_denied and bill_queries >= 2 and len({s["confirmation_id"] for s in steps if s["confirmation_id"]}) >= 2)
    return {"passed": t["status"] == case["expected_status"] and state_ok and clarification_ok and access_denied and recovery_ok and not unauthorized and not duplicates and not t["fallback_count"],
            "status": t["status"], "state": state, "steps": steps, "actions": t["actions"], "interpretations": t["interpretations"],
            "attempts": t["attempts"], "model_calls": t["model_calls"], "fallback_count": t["fallback_count"],
            "unauthorized_operations": unauthorized, "duplicate_operations": duplicates, "clarification_ok": clarification_ok,
            "wrong_owner_denied": access_denied, "recovery_ok": recovery_ok, "billing_query_count": bill_queries,
            "stale_evidence_injected": case["operation"] == "recover", "latency_ms": (time.monotonic() - start) * 1000}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--campaign", default="p0-20260911")
    parser.add_argument("--max-calls", type=int, default=60)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    if not 1 <= args.max_calls <= 60 or Path(args.campaign).name != args.campaign:
        raise SystemExit("Invalid campaign or call limit")
    raw = DATA.read_bytes()
    if hashlib.sha256(raw).hexdigest() != FROZEN_SHA:
        raise SystemExit("Frozen dataset changed; version it instead of rewriting criteria")
    dataset = json.loads(raw)
    cases = [c for c in dataset["cases"] if not args.only or c["id"] in args.only]
    if not cases or (args.only and set(args.only) - {c["id"] for c in cases}):
        raise SystemExit("Unknown case selection")
    output = ROOT / "data/eval/reports/agent"
    output.mkdir(parents=True, exist_ok=True)
    cfg = {**dotenv_values(ROOT / ".env.agent.local"), **os.environ}
    sdk, client = None, None
    if args.live:
        if not cfg.get("LLM_API_KEY") or not cfg.get("LLM_MODEL"):
            raise SystemExit("Missing credentials; live mode will not silently fall back")
        sdk = LLMClient(api_key=cfg["LLM_API_KEY"], model=cfg["LLM_MODEL"], base_url=cfg.get("LLM_BASE_URL"), provider=cfg.get("LLM_PROVIDER", "openai"), max_retries=0)
        client = BudgetClient(sdk, output / (args.campaign + "-budget.json"), args.max_calls)
    results = []
    code_hash = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(ROOT.rglob("*.py")) if ".venv" not in p.parts)).hexdigest()
    try:
        with tempfile.TemporaryDirectory() as directory:
            for case in cases:
                if client and client.ledger.exists() and json.loads(client.ledger.read_text())["reserved_calls"] >= args.max_calls:
                    result = {"passed": False, "not_run": "budget_exhausted"}
                else:
                    try:
                        result = await run_case(case, client, directory)
                    except Exception as ex:
                        result = {"passed": False, "error_type": type(ex).__name__}
                results.append({"id": case["id"], "category": case["category"], "kind": case["kind"], **result})
                print(case["id"], "PASS" if result["passed"] else "FAIL", flush=True)
    finally:
        if sdk:
            await sdk._client.close()
    metrics = {"expected_outcomes": {"numerator": sum(r["passed"] for r in results), "denominator": len(results)},
               "raw_model_semantics": {"numerator": sum(r.get("raw_model_correct") is True for r in results),
                                       "denominator": sum(r["kind"] == "semantic" and "not_run" not in r for r in results)} if client else None,
               "actual_calls_this_run": client.calls if client else 0,
               "usage": client.usage if client else None, "cost_usd": None,
               "scope": "Internal frozen synthetic acceptance. Safe rejection counts as expected outcome, not independent business completion. Journey timings include automated confirmation, not human waiting."}
    report = {"run_id": str(uuid.uuid4()), "timestamp": datetime.now(timezone.utc).isoformat(),
              "mode": "live" if client else "offline_rules", "dataset_sha256": FROZEN_SHA, "code_sha256": code_hash,
              "python": platform.python_version(), "model": sdk.model if sdk else None, "provider": cfg.get("LLM_PROVIDER") if sdk else None,
              "campaign": args.campaign, "campaign_budget": json.loads(client.ledger.read_text()) if client and client.ledger.exists() else None,
              "categories": dict(Counter(r["category"] for r in results)), "metrics": metrics, "results": results}
    path = output / ("p0-" + report["mode"] + "-" + report["run_id"] + ".json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(path), "expected_outcomes": metrics["expected_outcomes"],
                      "raw_model_semantics": metrics["raw_model_semantics"], "calls": metrics["actual_calls_this_run"],
                      "campaign_budget": report["campaign_budget"]}, ensure_ascii=False))
    if not all(r["passed"] for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
