import tempfile
import unittest

from agents.action_runtime import ActionRuntime
from scripts.agent_cli import Session


class CLITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.runtime = ActionRuntime(self.directory.name + "/state.sqlite3", allow_fallback=False)
        self.session = Session(self.runtime)
        self.order = self.runtime.seed("demo-user")

    def tearDown(self):
        self.directory.cleanup()

    async def pending(self):
        task = await self.session.handle("请修复升级后未生效的权益")
        self.assertEqual(task["status"], "awaiting_clarification")
        return await self.session.handle(self.order["id"])

    async def test_confirm_and_restart(self):
        task = await self.pending()
        self.assertEqual(task["status"], "awaiting_confirmation")
        task = await self.session.handle("好的")
        self.assertEqual(task["actions"], [])
        restarted = Session(ActionRuntime(self.runtime.path, allow_fallback=False))
        loaded = await restarted.handle("/resume " + task["id"])
        self.assertEqual(loaded["confirmation"]["id"], task["confirmation"]["id"])
        result = await restarted.handle("/confirm " + task["confirmation"]["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["actions"]), 1)
        with self.assertRaises(ValueError):
            await restarted.handle("再修一次")

    async def test_reject_and_invalid_confirmation(self):
        task = await self.pending()
        with self.assertRaises(ValueError):
            await self.session.handle("/confirm wrong-id")
        task = await self.session.handle("/reject " + task["confirmation"]["id"])
        self.assertEqual(task["status"], "cancelled")
        self.assertEqual(task["actions"], [])

    async def test_readonly_and_no_self_approval(self):
        await self.session.handle("查询订阅状态")
        task = await self.session.handle(self.order["id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["actions"], [])
        with self.assertRaises(ValueError):
            await self.session.handle("/approve yes")

    async def test_resume_checks_owner_and_revise(self):
        task = await self.pending()
        with self.assertRaises(ValueError):
            await Session(self.runtime, owner="other-user").handle("/resume " + task["id"])
        revised = await self.session.handle("/revise 查询账单")
        self.assertNotEqual(revised["id"], task["id"])
        self.assertEqual(self.runtime.get(task["id"], "demo-user")["status"], "superseded")
