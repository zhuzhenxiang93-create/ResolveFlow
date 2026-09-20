"""Budget-capped real-model validation for GoalInterpreter's general_remainder field
and tightened unsupported_requests boundary (added for full N-way compound-request
support — see wiki/product_positioning.md). Mirrors run_p0_acceptance.py's campaign-
wide call-ceiling pattern (BudgetClient + a ledger file), but targets the separate,
non-frozen data/eval/compound_acceptance_v1.json instead of the frozen p0 dataset —
this script never touches or re-validates that frozen file; use run_p0_acceptance.py
--only <ids> for regression checks against it instead."""
import argparse
import asyncio
import fcntl
import json
import os
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values
from agents.goal_interpreter import GoalInterpreter
from core.llm_client import LLMClient

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/eval/compound_acceptance_v1.json"


class BudgetClient:
    def __init__(self, client, ledger, limit):
        self.client, self.ledger, self.limit = client, ledger, limit
        self.calls, self.usage = 0, []

    async def create_tool_turn(self, **kwargs):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0)
            content = stream.read()
            state = json.loads(content) if content else {"limit": self.limit, "reserved_calls": 0}
            if state["limit"] != self.limit or state["reserved_calls"] >= state["limit"]:
                raise RuntimeError("Campaign call budget exhausted or changed")
            state["reserved_calls"] += 1
            stream.seek(0)
            stream.truncate()
            stream.write(json.dumps(state))
            stream.flush()
            os.fsync(stream.fileno())
        self.calls += 1
        result = await self.client.create_tool_turn(**kwargs)
        if result.get("_usage"):
            self.usage.append(result["_usage"])
        return result


def check_case(case, proposal):
    checks = {}
    goals = (proposal or {}).get("goals", {})
    if "expect_domains" in case:
        checks["domains_exact"] = set(goals.keys()) == set(case["expect_domains"])
    if "expect_domains_any" in case:
        checks["domains_any"] = any(domain in goals for domain in case["expect_domains_any"])
    checks["unsupported"] = bool((proposal or {}).get("unsupported_requests")) == bool(case.get("expect_unsupported"))
    remainder = (proposal or {}).get("general_remainder")
    if "expect_remainder" in case:
        checks["remainder_presence"] = bool(remainder) == bool(case["expect_remainder"])
    if case.get("expect_remainder_contains"):
        checks["remainder_contains_all"] = bool(remainder) and all(kw in remainder for kw in case["expect_remainder_contains"])
    if case.get("expect_remainder_contains_any"):
        checks["remainder_contains_any"] = bool(remainder) and any(kw in remainder for kw in case["expect_remainder_contains_any"])
    return checks, all(checks.values())


async def run_case(case, client):
    context = {"goals": {}, "order_id": None, "unresolved": [], "status": "awaiting_clarification",
               "response": "", "messages": [], "interpreted": False}
    proposal, record = await GoalInterpreter(client, timeout=30).interpret(case["message"], context)
    parsed = proposal.model_dump() if proposal else None
    checks, passed = check_case(case, parsed)
    return {"passed": passed, "checks": checks, "proposal": parsed, "record": record}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--campaign", default="compound-" + datetime.now(timezone.utc).strftime("%Y%m%d"))
    parser.add_argument("--max-calls", type=int, default=10)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    if not 1 <= args.max_calls <= 20 or Path(args.campaign).name != args.campaign:
        raise SystemExit("Invalid campaign or call limit")
    dataset = json.loads(DATA.read_text())
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
        sdk = LLMClient(api_key=cfg["LLM_API_KEY"], model=cfg["LLM_MODEL"], base_url=cfg.get("LLM_BASE_URL"),
                         provider=cfg.get("LLM_PROVIDER", "openai"), max_retries=0)
        client = BudgetClient(sdk, output / (args.campaign + "-budget.json"), args.max_calls)
    results = []
    try:
        for case in cases:
            if client and client.ledger.exists() and json.loads(client.ledger.read_text())["reserved_calls"] >= args.max_calls:
                result = {"passed": False, "not_run": "budget_exhausted"}
            else:
                try:
                    result = await run_case(case, client)
                except Exception as ex:
                    result = {"passed": False, "error_type": type(ex).__name__}
            results.append({"id": case["id"], "category": case["category"], **result})
            print(case["id"], "PASS" if result["passed"] else "FAIL",
                  result.get("checks") or result.get("not_run") or result.get("error_type") or "", flush=True)
    finally:
        if sdk:
            await sdk._client.close()
    report = {"run_id": str(uuid.uuid4()), "timestamp": datetime.now(timezone.utc).isoformat(),
              "mode": "live" if client else "offline_rules", "model": sdk.model if sdk else None,
              "campaign": args.campaign, "calls": client.calls if client else 0,
              "categories": dict(Counter(r["category"] for r in results)),
              "expected_outcomes": {"numerator": sum(r["passed"] for r in results), "denominator": len(results)},
              "results": results}
    path = output / ("compound-" + report["mode"] + "-" + report["run_id"] + ".json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(path), "expected_outcomes": report["expected_outcomes"], "calls": report["calls"]}, ensure_ascii=False))
    if not all(r["passed"] for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
