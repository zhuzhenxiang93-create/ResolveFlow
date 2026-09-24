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
            # Both goals' confirmations can be proposed at once now; work
            # through whichever is still pending each round.
            pending = next(c for c in task["confirmations"].values() if c["status"] == "pending")
            print(json.dumps({"stage": "user_confirmation_required", "confirmation": pending}, ensure_ascii=False))
            runtime.confirm(task["id"], "demo", pending["id"], True)
            task = await runtime.advance(task["id"], "demo")
        assert task["status"] == "awaiting_approval"
        print(json.dumps({"stage": "approval_required", "task": task}, ensure_ascii=False))
        restarted = ActionRuntime(directory + "/state.sqlite3")
        task = restarted.approve(task["id"], "demo", task["approvals"]["request_refund"]["id"], True, "demo-reviewer")
        assert task["status"] == "completed" and not task["unresolved"]
        print(json.dumps({"stage": "verified", "task": task}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
