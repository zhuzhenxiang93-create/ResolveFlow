"""Separate offline/live reports; live requests require explicit configured budgets."""
import argparse
import asyncio
import hashlib
import json
import math
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from evaluation.agent_evaluator import ROOT, evaluate


class BudgetClient:
    def __init__(self, client, max_calls, max_usd, input_price, output_price):
        if any(not math.isfinite(v) or v <= 0 for v in (max_calls, max_usd, input_price, output_price)):
            raise ValueError("Positive call, cost and price limits are required")
        self.client, self.max_calls, self.max_usd = client, max_calls, max_usd
        self.input_price, self.output_price = input_price, output_price
        self.calls, self.reserved = 0, 0.0

    async def create_tool_turn(self, **kwargs):
        # Conservative byte-count reservation plus protocol overhead. No SDK retries.
        # This is an application estimate, not a provider-side billing hard limit.
        input_bound = len(json.dumps(kwargs).encode("utf-8")) + 4096
        if input_bound > 100000:
            raise ValueError("Input budget exhausted")
        reserve = (input_bound * self.input_price + kwargs["max_tokens"] * self.output_price) / 1_000_000
        if self.calls >= self.max_calls or self.reserved + reserve > self.max_usd:
            raise ValueError("Live call/cost budget exhausted")
        self.calls += 1
        self.reserved += reserve  # Failed/unknown requests retain reservation.
        return await self.client.create_tool_turn(**kwargs)


def code_version():
    files = sorted(p for p in ROOT.rglob("*.py") if not any(part in {".venv", "__pycache__"} for part in p.parts))
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return {"git_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()),
            "backend_python_sha256": digest.hexdigest()}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/eval/reports/agent")
    args = parser.parse_args()
    client, live_unavailable = None, None
    if args.live:
        required = ["LLM_API_KEY", "LLM_MODEL", "AGENT_EVAL_MAX_USD", "AGENT_EVAL_INPUT_USD_PER_M", "AGENT_EVAL_OUTPUT_USD_PER_M"]
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            live_unavailable = "Live validation not executed; missing configuration: " + ", ".join(missing)
        else:
            from core.llm_client import LLMClient
            client = BudgetClient(LLMClient(api_key=os.environ["LLM_API_KEY"], model=os.environ["LLM_MODEL"],
                                           base_url=os.getenv("LLM_BASE_URL"), max_retries=0),
                                  int(os.getenv("AGENT_EVAL_MAX_CALLS", "32")), float(os.environ["AGENT_EVAL_MAX_USD"]),
                                  float(os.environ["AGENT_EVAL_INPUT_USD_PER_M"]), float(os.environ["AGENT_EVAL_OUTPUT_USD_PER_M"]))
    report = await evaluate(client)
    report.update(code_version=code_version(), run_id=str(uuid.uuid4()), timestamp=datetime.now(timezone.utc).isoformat(),
                  requested_mode="live_llm" if args.live else "offline_deterministic", live_unavailable=live_unavailable,
                  model_configuration={"model": os.getenv("LLM_MODEL"), "provider": os.getenv("LLM_PROVIDER", "anthropic")} if client else None)
    if client:
        report["metrics"]["model_call_attempts"] = report["metrics"]["model_calls"]
        report["metrics"]["model_calls"] = client.calls
        report["live_budget"] = {"actual_requests": client.calls, "reserved_usd_estimate": client.reserved,
                                 "max_calls": client.max_calls, "max_usd": client.max_usd,
                                 "note": "Caller-supplied prices and conservative reservation; not an invoice or provider-enforced cap"}
        m = report["metrics"]
        if m["input_tokens"] is not None:
            report["estimated_cost_usd"] = (m["input_tokens"] * client.input_price + m["output_tokens"] * client.output_price) / 1_000_000
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = "latest_live" if args.live else "latest_offline"
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    (args.output_dir / (report["run_id"] + ".json")).write_text(output)
    (args.output_dir / (stem + ".json")).write_text(output)
    m = report["metrics"]
    lines = ["# Agent validation", "", "Mode: " + report["mode"], "", live_unavailable or "", "",
             "Internal developer-visible regression set. Not an official benchmark or production measurement.", "",
             "| Metric | Value |", "| --- | --- |"]
    lines.extend("| " + name + " | " + json.dumps(value, ensure_ascii=False) + " |" for name, value in m.items())
    lines += ["", "## Resume-safe bullets", "",
              f"- 为订阅客服 Agent 建立 {report['sample_count']} 个内部模拟回归场景，预期行为通过 {m['expected_outcome_rate']['numerator']}/{report['sample_count']}；覆盖查询、审批、部分完成和状态恢复。模式：{report['mode']}。",
              "- 实现原生工具协议适配、受约束计划、审批绑定和独立业务状态核验；离线结果不代表真实模型解决率。", "",
              "## Limitations", *["- " + s for s in report["limitations"]]]
    (args.output_dir / (stem + ".md")).write_text("\n".join(lines) + "\n")
    print(json.dumps({"report": str(args.output_dir / (stem + ".json")), "mode": report["mode"],
                      "sample_count": report["sample_count"], "metrics": m, "live_unavailable": live_unavailable}, ensure_ascii=False, indent=2))
    if not all(r["passed"] for r in report["rows"]):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
