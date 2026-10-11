"""Server-to-server Dify Workflow adapter. Credentials never reach the frontend."""
import json
import os
import httpx
from business.conversation import Proposal

class DifyWorkflow:
    def __init__(self, *, transport=None):
        self.url = os.environ["DIFY_API_BASE"].rstrip("/")
        self.key = os.environ["DIFY_API_KEY"]
        self.transport = transport

    async def run(self, owner, message, catalog, history):
        async with httpx.AsyncClient(timeout=240, transport=self.transport) as client:
            response = await client.post(self.url + "/workflows/run",
                headers={"Authorization": "Bearer " + self.key}, json={
                    "inputs": {"message": message, "catalog": json.dumps(catalog, ensure_ascii=False),
                               "history": json.dumps(history, ensure_ascii=False)},
                    "response_mode": "blocking", "user": owner})
            response.raise_for_status()
        data = response.json().get("data", {})
        if data.get("status") != "succeeded":
            raise ValueError("Dify workflow did not succeed")
        out = data.get("outputs", {})
        proposal = Proposal.model_validate_json(out["proposal"])
        for field in ("policy_answer", "support_answer"):
            if not isinstance(out.get(field), str):
                raise ValueError("Invalid Dify output: " + field)
        sources = json.loads(out.get("sources", "[]"))
        traces = json.loads(out.get("tool_traces", "[]"))
        if not isinstance(sources, list) or not isinstance(traces, list):
            raise ValueError("Invalid Dify evidence")
        plan = json.loads(out.get("agent_plan", "[]"))
        results = json.loads(out.get("agent_results", "[]"))
        allowed = {"commerce", "general", "technical", "billing", "escalation"}
        if not isinstance(plan, list) or any(role not in allowed for role in plan):
            raise ValueError("Invalid agent plan")
        if not isinstance(results, list) or any(not isinstance(a, dict) or
                a.get("agent_type") not in plan or not isinstance(a.get("answer"), str) or
                a.get("status") not in {"completed", "failed"} for a in results):
            raise ValueError("Invalid agent results")
        is_policy = bool(proposal.policy_only or proposal.policy_question)
        for agent in results:
            agent["answer_kind"] = "policy" if agent["agent_type"] == "general" and is_policy else "support"
        if out["policy_answer"] and not is_policy:
            out["support_answer"] = (out["policy_answer"] + "\n" + out["support_answer"]).strip()
            out["policy_answer"] = ""
        if plan and ([a["agent_type"] for a in results] != plan or
                any(a["agent_type"] == "commerce" and a["status"] == "failed" for a in results)):
            raise ValueError("Incomplete agent results")
        return {**out, "proposal": proposal, "sources": sources, "tool_traces": traces,
                "agent_plan": plan, "agent_results": results}
