"""Offline end-to-end demonstration using an isolated temporary database."""
import asyncio
import json
import tempfile

from agents.action_runtime import ActionRuntime


async def main():
    with tempfile.TemporaryDirectory() as directory:
        runtime = ActionRuntime(directory + "/state.sqlite3")
        order = runtime.seed("demo")
        task = runtime.create("demo", "请修复升级后的权益，并申请重复扣款退款")
        print(json.dumps({"stage": "clarification", "task": task}, ensure_ascii=False))
        task = await runtime.advance(task["id"], "demo", order["id"])
        for _ in range(2):
            assert task["status"] == "awaiting_confirmation"
            print(json.dumps({"stage": "user_confirmation_required", "confirmation": task["confirmation"]}, ensure_ascii=False))
            runtime.confirm(task["id"], "demo", task["confirmation"]["id"], True)
            task = await runtime.advance(task["id"], "demo")
        assert task["status"] == "awaiting_approval"
        print(json.dumps({"stage": "approval_required", "task": task}, ensure_ascii=False))
        restarted = ActionRuntime(directory + "/state.sqlite3")
        task = restarted.approve(task["id"], "demo", task["approval"]["id"], True, "demo-reviewer")
        assert task["status"] == "completed" and not task["unresolved"]
        print(json.dumps({"stage": "verified", "task": task}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
