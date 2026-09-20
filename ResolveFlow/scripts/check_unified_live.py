"""Explicit 12-call unified conversation smoke, isolated state and no writes."""
import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from dotenv import dotenv_values
from agents.action_runtime import ActionRuntime
from agents.conversation_service import ConversationService
from core.llm_client import LLMClient

ROOT = Path(__file__).resolve().parents[1]


class Budget:
    def __init__(self, client, limit=12):
        self.client, self.model, self.calls = client, client.model, 0
        self.limit = limit
        self.usage = []

    async def request(self, method, **kwargs):
        if self.calls >= self.limit:
            raise RuntimeError("Unified acceptance call budget exhausted")
        self.calls += 1
        result = await getattr(self.client, method)(**{**kwargs, "max_tokens": min(kwargs.get("max_tokens", 512), 512)})
        if isinstance(result, dict) and result.get("_usage"):
            self.usage.append(result["_usage"])
        return result

    async def create(self, **kwargs):
        return await self.request("create", **kwargs)

    async def create_tool_turn(self, **kwargs):
        return await self.request("create_tool_turn", **kwargs)


class ReadOnlyRuntime(ActionRuntime):
    def _execute(self, db, task, name, fallback=False):
        if name not in {"query_subscription", "query_billing", "query_renewal", "check_service"}:
            raise PermissionError("No business writes in this acceptance run")
        return super()._execute(db, task, name, fallback)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--max-calls", type=int, choices=range(1, 13), default=12)
    args = parser.parse_args()
    cfg = {**dotenv_values(ROOT / ".env.agent.local"), **os.environ}
    client = LLMClient(api_key=cfg["LLM_API_KEY"], model=cfg["LLM_MODEL"], provider=cfg.get("LLM_PROVIDER", "openai"), base_url=cfg.get("LLM_BASE_URL"), max_retries=0)
    budget = Budget(client, args.max_calls)
    turns, conv, task = [], None, None
    checks, error = {}, None
    try:
        with tempfile.TemporaryDirectory() as directory:
            r = ReadOnlyRuntime(Path(directory) / "db", client=budget, allow_fallback=False)
            service = ConversationService(r)
            order = r.seed("test-user")
            for message in ["怎么退订？关闭后权益会马上消失吗？", "那就帮我关掉。", order["id"], "顺便解释一下退款政策，我现在不申请退款。"]:
                result = await service.send("test-user", message, conversation_id=conv)
                conv = result["conversation_id"]
                task = result["task"] or task
                turns.append({"message": message, "route": result["route"], "status": result["status"],
                              "response": result["response"], "intent": result["intent"], "knowledge_used": result["knowledge_used"],
                              "sources": result["sources"], "interpretation": result["interpretation"], "degradations": result["degradations"]})
                print(result["route"], result["status"], flush=True)
            current = r.get(task["id"], "test-user") if task else None
            with r.connect() as db:
                stored = json.loads(db.execute("SELECT body FROM orders WHERE id=?", (order["id"],)).fetchone()[0])
            checks = {"initial_policy": turns[0]["knowledge_used"], "reference_resolved": turns[1]["status"] == "awaiting_clarification",
                      "awaits_confirmation": turns[2]["status"] == "awaiting_confirmation", "policy_while_pending": turns[3]["knowledge_used"],
                      "pending_preserved": bool(current and current["status"] == "awaiting_confirmation"),
                      "no_business_change": stored == order and not (current or {}).get("actions")}
    except Exception as ex:
        error = {"type": type(ex).__name__}
    finally:
        await client._client.close()
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "mode": "real_llm_local_unified_smoke", "model": client.model,
              "calls": budget.calls, "limit": budget.limit, "checks": checks, "passed": bool(checks) and all(checks.values()) and error is None, "turns": turns, "error": error,
              "native_usage_only": budget.usage, "total_tokens": None, "cost_usd": None,
              "scope": "Four synthetic turns; real model with local SQLite memory and lexical knowledge, no Redis/Chroma live validation, no business writes"}
    path = ROOT / "data/eval/reports/agent/unified-live-" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(path), "checks": checks, "calls": budget.calls}, ensure_ascii=False))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
