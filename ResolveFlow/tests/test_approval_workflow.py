import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from api.action_demo import app
from api.action_routes import runtime
from tests.action_test_support import mint_token


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict("os.environ", {"AGENT_STATE_PATH": self.temp.name + "/db", "AGENT_USE_LLM": "0",
                                       "AGENT_JWT_SECRET": "test-signing-secret"})
        env.start()
        self.addCleanup(env.stop)
        runtime.cache_clear()
        self.addCleanup(runtime.cache_clear)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.user = {"Authorization": "Bearer " + mint_token("user", "alice")}
        self.review = {"Authorization": "Bearer " + mint_token("reviewer", "bob")}

    def pending(self):
        order = self.client.post("/agent/demo/orders", headers=self.user).json()
        task = self.client.post("/agent/tasks", headers=self.user, json={"message": "请申请重复扣款退款"}).json()
        base = "/agent/tasks/" + task["id"]
        task = self.client.post(base + "/continue", headers=self.user, json={"order_id": order["id"]}).json()
        task = self.client.post(base + "/confirmation", headers=self.user,
                                json={"confirmation_id": task["confirmation"]["id"], "accepted": True}).json()
        self.assertEqual(task["status"], "awaiting_approval")
        return task, base

    def test_independent_review_approve_replay_and_shared_state(self):
        task, base = self.pending()
        self.assertEqual(self.client.get("/agent/review/tasks", headers=self.user).status_code, 403)
        self.assertEqual(self.client.get("/agent/review/tasks").status_code, 403)
        queue = self.client.get("/agent/review/tasks", headers=self.review).json()
        self.assertIn(task["id"], [t["id"] for t in queue["tasks"]])
        detail = self.client.get("/agent/review/tasks/" + task["id"], headers=self.review).json()
        self.assertEqual(detail["current_order"]["refunds"], 0)
        body = {"approval_id": task["approval"]["id"], "approved": True}
        self.assertEqual(self.client.post(base + "/approval", headers=self.user, json=body).status_code, 403)
        result = self.client.post(base + "/approval", headers=self.review, json=body).json()
        self.assertEqual(result["status"], "completed")
        replay = self.client.post(base + "/approval", headers=self.review, json=body).json()
        self.assertEqual(len(replay["actions"]), 1)
        runtime.cache_clear()
        self.assertEqual(self.client.get(base, headers=self.user).json()["status"], "completed")

    def test_rejection_does_not_refund(self):
        task, base = self.pending()
        result = self.client.post(base + "/approval", headers=self.review,
                                 json={"approval_id": task["approval"]["id"], "approved": False}).json()
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.client.get("/agent/review/tasks/" + task["id"], headers=self.review).json()["current_order"]["refunds"], 0)

    def test_invalidated_release_requires_new_consent(self):
        task, base = self.pending()
        old = task["approval"]["id"]
        self.client.post(base + "/continue", headers=self.user, json={"message": "修改本次需求"})
        self.assertEqual(self.client.post(base + "/release", headers=self.user).status_code, 403)
        self.assertEqual(self.client.post(base + "/approval", headers=self.review,
                         json={"approval_id": old, "approved": True}).json()["status"], "needs_human")
        released = self.client.post(base + "/release", headers=self.review).json()
        self.assertEqual(released["status"], "awaiting_confirmation")
        self.assertNotEqual(released["confirmation"]["id"], task["confirmation"]["id"])
        self.assertEqual(released["approval_history"][-1]["status"], "invalidated")
        new = self.client.post(base + "/confirmation", headers=self.user,
                              json={"confirmation_id": released["confirmation"]["id"], "accepted": True}).json()
        self.assertNotEqual(new["approval"]["id"], old)
        self.assertEqual(self.client.post(base + "/approval", headers=self.review,
                         json={"approval_id": old, "approved": True}).status_code, 400)
        done = self.client.post(base + "/approval", headers=self.review,
                               json={"approval_id": new["approval"]["id"], "approved": True}).json()
        self.assertEqual(done["status"], "completed")

    def test_expired_approval_cannot_execute(self):
        task, base = self.pending()
        with patch("agents.action_runtime.time.time", return_value=task["approval"]["expires_at"] + 1):
            self.assertTrue(self.client.get("/agent/review/tasks/" + task["id"], headers=self.review).json()["approval_expired"])
            result = self.client.post(base + "/approval", headers=self.review,
                                     json={"approval_id": task["approval"]["id"], "approved": True}).json()
        self.assertEqual(result["status"], "needs_human")
        self.assertEqual(result["actions"], [])
