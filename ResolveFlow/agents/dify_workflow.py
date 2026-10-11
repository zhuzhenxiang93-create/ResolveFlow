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
        async with httpx.AsyncClient(timeout=90, transport=self.transport) as client:
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
        return {**out, "proposal": proposal, "sources": sources, "tool_traces": traces}
