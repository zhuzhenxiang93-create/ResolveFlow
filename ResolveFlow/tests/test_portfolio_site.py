"""Public single-container host: static build + /api/python demo API + opt-in abuse limits."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import commerce_routes


class PortfolioSiteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        dist = Path(self.tmp.name) / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html>ResolveFlow</html>")
        self.env = patch.dict(os.environ, {
            "RESOLVEFLOW_DEMO_MODE": "true", "RESOLVEFLOW_DEMO_DIR": self.tmp.name + "/demo", "AGENT_USE_LLM": "0",
            "RESOLVEFLOW_FRONTEND_DIST": str(dist), "RESOLVEFLOW_PUBLIC": "true",
            "RESOLVEFLOW_SESSIONS_PER_HOUR": "3", "RESOLVEFLOW_MAX_SESSIONS": "2"})
        self.env.start()
        self.saved = commerce_routes.runtime, commerce_routes.conversation_service
        from api.portfolio_site import create_site
        self.client = TestClient(create_site())

    def tearDown(self):
        self.client.close()
        commerce_routes.runtime, commerce_routes.conversation_service = self.saved
        self.env.stop()
        self.tmp.cleanup()

    def test_static_and_prefixed_api(self):
        self.assertIn("ResolveFlow", self.client.get("/").text)
        health = self.client.get("/api/python/health").json()
        self.assertTrue(health["demo"] and health["public"])
        self.assertEqual(health["mode"], "offline-rules")
        self.assertEqual(self.client.post("/api/python/chat", json={"message": "hi"}).status_code, 401)

    def test_full_refund_path_through_prefix(self):
        s = self.client.post("/api/python/demo/session").json()
        user = {"Authorization": "Bearer " + s["userToken"]}
        r = self.client.post("/api/python/chat", headers=user, json={"message": "帮我把无线耳机退掉"}).json()
        self.assertEqual(r["commerce_case"]["operations"][0]["quote"]["amount_minor"], 25900)

    def test_session_rate_limit_and_budget(self):
        codes = [self.client.post("/api/python/demo/session").status_code for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])
        files = list(Path(self.tmp.name, "demo").glob("*.sqlite3"))
        self.assertLessEqual(len(files), 2)
