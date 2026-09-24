import json
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from core.llm_client import LLMClient


class NativeProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_and_anthropic_roundtrip(self):
        for provider in ("openai", "anthropic"):
            with self.subTest(provider=provider):
                client = LLMClient.__new__(LLMClient)
                client.provider, client.model = provider, "test-model"
                call = {"id": "call_1", "type": "function", "function": {
                    "name": "query_billing", "arguments": '{"order_id":"o1"}'}}
                transcript = [{"role": "user", "content": "Check my bill"},
                              {"role": "assistant", "content": "", "tool_calls": [call]},
                              {"role": "tool", "tool_call_id": "call_1", "content": '{"charges":2}'}]
                if provider == "openai":
                    create = AsyncMock(return_value=NS(choices=[NS(message=NS(content=None, tool_calls=[
                        NS(id="call_2", function=NS(name="request_refund", arguments='{"order_id":"o1"}'))]))]))
                    client._client = NS(chat=NS(completions=NS(create=create)))
                else:
                    create = AsyncMock(return_value=NS(content=[NS(type="tool_use", id="call_2", name="request_refund", input={"order_id": "o1"})]))
                    client._client = NS(messages=NS(create=create))
                result = await client.create_tool_turn(system="System", messages=transcript, tools=[{
                    "name": "request_refund", "description": "Request approval", "parameters": {"type": "object"}}], required_tool="request_refund")
                self.assertEqual(result["tool_calls"][0]["id"], "call_2")
                kwargs = create.call_args.kwargs
                if provider == "openai":
                    self.assertEqual(kwargs["tool_choice"], {"type": "function", "function": {"name": "request_refund"}})
                    self.assertFalse(kwargs["parallel_tool_calls"])
                    self.assertEqual(kwargs["messages"][-1]["tool_call_id"], "call_1")
                else:
                    self.assertEqual(kwargs["tool_choice"]["type"], "tool")
                    self.assertEqual(kwargs["tool_choice"]["name"], "request_refund")
                    self.assertEqual(kwargs["messages"][-1]["content"][0]["tool_use_id"], "call_1")
                    self.assertIn("input_schema", kwargs["tools"][0])

    async def test_bad_arguments_are_returned_as_tool_error(self):
        from action_test_support import ActionRuntime
        with tempfile.TemporaryDirectory() as directory:
            client = NS(create_tool_turn=AsyncMock(return_value={"role": "assistant", "content": "", "tool_calls": [{
                "id": "bad", "function": {"name": "request_refund", "arguments": '{"order_id":"foreign"}'}, "type": "function"}]}))
            runtime = ActionRuntime(directory + "/db", client=client, max_steps=1)
            order = runtime.seed("user")
            task = runtime.create("user", "重复扣款")
            result = await runtime.advance(task["id"], "user", order["id"])
            self.assertFalse(result["approvals"])
            self.assertFalse(json.loads(result["tool_messages"][-1]["content"])["success"])
            self.assertEqual(result["tool_messages"][-1]["tool_call_id"], "bad")

    async def test_anthropic_multiple_results_stay_associated_after_restart(self):
        client = LLMClient.__new__(LLMClient)
        client.provider, client.model = "anthropic", "mock"
        create = AsyncMock(return_value=NS(content=[], usage=NS(input_tokens=20, output_tokens=4)))
        client._client = NS(messages=NS(create=create))
        calls = [{"id": str(i), "type": "function", "function": {"name": "query_billing", "arguments": "{}"}} for i in range(2)]
        transcript = [{"role": "user", "content": "query"}, {"role": "assistant", "tool_calls": calls}]
        transcript += [{"role": "tool", "tool_call_id": str(i), "content": json.dumps({"success": bool(i)})} for i in range(2)]
        result = await client.create_tool_turn(system="system", messages=json.loads(json.dumps(transcript)),
                                                tools=[{"name": "query_billing", "description": "Read", "parameters": {"type": "object"}}])
        self.assertEqual([b["tool_use_id"] for b in create.call_args.kwargs["messages"][-1]["content"]], ["0", "1"])
        self.assertEqual(result["_usage"], {"input_tokens": 20, "output_tokens": 4})


class UnifiedAPITests(unittest.TestCase):
    def test_chat_task_api_and_approval_share_state(self):
        from fastapi.testclient import TestClient
        from api.main import app
        from api.action_routes import runtime
        from tests.action_test_support import mint_token
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
            "AGENT_STATE_PATH": directory + "/db", "AGENT_JWT_SECRET": "test-signing-secret",
            "AGENT_USE_LLM": "0",
        }):
            runtime.cache_clear()
            self.addCleanup(runtime.cache_clear)
            # Do not start unrelated Redis/Chroma services in this API contract test.
            client = TestClient(app)
            self.addCleanup(client.close)
            headers = {"Authorization": "Bearer " + mint_token("user", "alice")}
            payload = {"message": "请修复升级后的权益，并申请重复扣款退款", "user_id": "forged"}
            self.assertEqual(client.post("/chat", json=payload).status_code, 401)
            rows = client.post("/agent/demo/orders", headers=headers).json()["objects"]
            oid = next(o["id"] for o in rows if o["id"].endswith("pro"))
            payload["message"] = "申请重复扣款退款 " + oid
            case = client.post("/chat", headers=headers, json=payload).json()["commerce_case"]
            self.assertEqual(case["owner"], "alice")
            aid=case["operations"][0]["id"]
            result=client.post("/commerce/cases/"+case["id"]+"/decision",headers=headers,json={"action_id":aid,"decision":"confirm"}).json()
            self.assertEqual(result["status"],"awaiting_approval")
            review={"Authorization":"Bearer "+mint_token("reviewer","bob")}
            for decision in ("approve","settle"):
                result=client.post("/commerce/review/"+case["id"],headers=review,json={"action_id":aid,"decision":decision}).json()
            self.assertEqual(result["status"],"completed")
            self.assertEqual(client.get("/agent/tasks/"+case["id"],headers=headers).json()["status"],"completed")
