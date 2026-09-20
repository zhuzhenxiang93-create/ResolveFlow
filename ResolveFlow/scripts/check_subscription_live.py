"""Opt-in, at most six real requests; synthetic read-only subscription checks."""
import argparse
import asyncio
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

from agents.action_runtime import ActionRuntime
from agents.agent_orchestrator import AgentOrchestrator
from core.llm_client import LLMClient

ROOT = Path(__file__).resolve().parents[1]


class ReadOnlyBudget:
    def __init__(self, client, limit=6):
        self.client, self.calls, self.limit = client, 0, limit

    async def create_tool_turn(self, **kwargs):
        if self.calls >= self.limit:
            raise RuntimeError("Six-call live acceptance budget exhausted")
        self.calls += 1
        return await self.client.create_tool_turn(**kwargs)


class ReadOnlyRuntime(ActionRuntime):
    def _execute(self, db, task, name, fallback=False):
        if name not in {"query_subscription", "query_billing", "query_renewal", "check_service"}:
            raise PermissionError("Acceptance run allows read tools only")
        return super()._execute(db, task, name, fallback)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--consultation-only", action="store_true", help="One consultation case, maximum two calls")
    args = parser.parse_args()
    cfg = {**dotenv_values(ROOT / ".env.agent.local"), **os.environ}
    if not cfg.get("LLM_API_KEY") or not cfg.get("LLM_MODEL"):
        raise SystemExit("Missing local model credentials")
    client = LLMClient(api_key=cfg["LLM_API_KEY"], model=cfg["LLM_MODEL"],
                       base_url=cfg.get("LLM_BASE_URL"), provider=cfg.get("LLM_PROVIDER", "openai"), max_retries=0)
    budget = ReadOnlyBudget(client, 2 if args.consultation_only else 6)
    cases = []
    try:
        with tempfile.TemporaryDirectory() as directory:
            runtime = ReadOnlyRuntime(Path(directory) / "db", client=budget, allow_fallback=False, model_timeout=30)
            order = runtime.seed("acceptance")
            for message, topic in [("怎么退订？关闭自动续费后会员权益会立即消失吗？", "renewal"),
                                   ("订阅退款需要什么条件，多久能到账？", "refund"),
                                   ("只查询订单 " + order["id"] + " 的自动续费状态，不要进行修改。", None)]:
                if args.consultation_only and cases:
                    break
                start = time.monotonic()
                task = await AgentOrchestrator.execute_action(runtime, owner="acceptance", message=message)
                expected = {"policy": "read"} if topic else {"renewal": "read"}
                content_ok = (any(d["topic"] == topic for d in task.get("knowledge_result", {}).get("sources", []))
                              if topic else any(e["tool"] == "query_renewal" and e["success"] and
                                                e["data"].get("auto_renew") is True for e in task["evidence"]))
                cases.append({"message": message, "passed": task["status"] == "completed" and task["goals"] == expected and
                              content_ok and not task["actions"] and not task["fallback_count"],
                              "status": task["status"], "goals": task["goals"], "response": task["response"],
                              "latency_ms": round((time.monotonic() - start) * 1000), "usage": task["usage"],
                              "model_calls": task["model_calls"], "fallback_count": task["fallback_count"],
                              "interpretations": task["interpretations"],
                              "actions": task["actions"]})
    finally:
        await client._client.close()
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "mode": "real_llm_internal_acceptance",
              "model": client.model, "source": "developer-authored synthetic read-only requests; optional consultation subset",
              "sample_count": len(cases), "passed": sum(c["passed"] for c in cases), "actual_calls": budget.calls,
              "max_calls": budget.limit, "cost_usd": None, "scope": "Not a benchmark or production validation; simulated business state",
              "cases": cases}
    path = ROOT / ("data/eval/reports/agent/subscription_consultation_retry.json" if args.consultation_only else "data/eval/reports/agent/latest_subscription_live.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["passed"] != len(cases):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
