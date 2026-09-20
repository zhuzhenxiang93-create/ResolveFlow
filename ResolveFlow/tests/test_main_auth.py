"""Auth coverage for api/main.py's non-Action routes, added when the product
was unified onto a single JWT identity model: `/chat` now has exactly one
code path (the old mode=chat/mode=action branches are gone), and /search,
/knowledge/*, /skills, /monitor, /eval/run moved from fully open to a tiered
model — any authenticated role for reads, admin for writes/ops. /health and
/metrics stay open (Docker healthcheck and Prometheus scraping need that).

These tests exercise the auth *gate* on each route without spinning up the
full RAG/monitor stack (TestClient(app) without entering the lifespan context
leaves _tool_manager/_monitor/_skill_manager as None, same pattern already
used by test_native_tools.py and test_unified_conversation.py). A FastAPI
Depends() runs before the handler body, so a 401/403 proves the auth check
fired; a non-401/403 response (503 "服务未就绪" here, since the underlying
service is None in-process) proves the request got *past* the auth gate."""
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import main
from tests.action_test_support import mint_token


class MainAuthTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict("os.environ", {"AGENT_JWT_SECRET": "test-signing-secret"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.user = {"Authorization": "Bearer " + mint_token("user", "alice")}
        self.admin = {"Authorization": "Bearer " + mint_token("admin", "root")}

    def test_health_and_metrics_stay_open(self):
        # /health reports 503 with no token because _orchestrator is None in
        # this bare TestClient (lifespan never ran) — that's the readiness
        # check, not an auth rejection; the point is it is NOT 401.
        self.assertNotEqual(self.client.get("/health").status_code, 401)
        self.assertEqual(self.client.get("/metrics").status_code, 200)

    def test_chat_has_no_mode_field_and_always_requires_a_token(self):
        payload = {"message": "你好"}
        self.assertEqual(self.client.post("/chat", json=payload).status_code, 401)
        # A client that still sends the retired `mode` field is not rejected
        # for it (backward compatible) — the field is simply ignored, and the
        # single remaining code path still requires auth same as any other call.
        legacy_payload = {"message": "你好", "mode": "action"}
        self.assertNotEqual(self.client.post("/chat", json=legacy_payload).status_code, 400)
        self.assertEqual(self.client.post("/chat", json=legacy_payload).status_code, 401)

    def test_search_requires_any_authenticated_role(self):
        self.assertEqual(self.client.post("/search?query=x").status_code, 401)
        self.assertNotEqual(self.client.post("/search?query=x", headers=self.user).status_code, 401)

    def test_knowledge_stats_requires_any_role_not_admin_specifically(self):
        self.assertEqual(self.client.get("/knowledge/stats").status_code, 401)
        self.assertNotEqual(self.client.get("/knowledge/stats", headers=self.user).status_code, 401)

    def test_knowledge_add_requires_admin_not_just_any_role(self):
        # _require_admin follows the same convention as action_routes.py's
        # reviewer(): any auth failure (missing token or wrong role) is 403,
        # not a 401-then-403 split — consistent with the existing RBAC helpers.
        body = {"documents": [{"title": "t", "content": "c"}]}
        self.assertEqual(self.client.post("/knowledge/add", json=body).status_code, 403)
        self.assertEqual(self.client.post("/knowledge/add", json=body, headers=self.user).status_code, 403)
        self.assertNotEqual(self.client.post("/knowledge/add", json=body, headers=self.admin).status_code, 403)

    def test_knowledge_upload_requires_admin(self):
        files = {"file": ("doc.txt", b"hello", "text/plain")}
        self.assertEqual(self.client.post("/knowledge/upload", files=files).status_code, 403)
        self.assertEqual(self.client.post("/knowledge/upload", files=files, headers=self.user).status_code, 403)

    def test_monitor_requires_admin_not_just_any_role(self):
        self.assertEqual(self.client.get("/monitor").status_code, 403)
        self.assertEqual(self.client.get("/monitor", headers=self.user).status_code, 403)
        self.assertNotEqual(self.client.get("/monitor", headers=self.admin).status_code, 403)

    def test_skills_read_is_any_role_reload_is_admin(self):
        self.assertEqual(self.client.get("/skills").status_code, 401)
        self.assertNotEqual(self.client.get("/skills", headers=self.user).status_code, 401)
        self.assertEqual(self.client.post("/skills/reload").status_code, 403)
        self.assertEqual(self.client.post("/skills/reload", headers=self.user).status_code, 403)

    def test_eval_run_requires_admin(self):
        self.assertEqual(self.client.post("/eval/run", json={}).status_code, 403)
        self.assertEqual(self.client.post("/eval/run", json={}, headers=self.user).status_code, 403)


if __name__ == "__main__":
    unittest.main()
