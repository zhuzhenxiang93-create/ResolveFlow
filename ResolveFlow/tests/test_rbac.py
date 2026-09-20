"""Local JWT RBAC (user/reviewer/admin): mint/verify contract, and that the
FastAPI wiring in api/action_routes.py actually enforces it end to end —
including the self-approval guard, which the old two-hardcoded-strings
identity model could never actually exercise (owner() always returned
"demo-user" and reviewer() always returned "demo-reviewer", so
`reviewer == owner` inside ActionRuntime.approve() was structurally
unreachable)."""
import tempfile
import unittest
from unittest.mock import patch

import jwt as pyjwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import AuthError, decode_token, mint_token, subject_with_role


class TokenContractTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict("os.environ", {"AGENT_JWT_SECRET": "unit-test-secret"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_round_trip_preserves_subject_and_role(self):
        token = mint_token("alice", "user")
        payload = decode_token(token)
        self.assertEqual(payload["sub"], "alice")
        self.assertEqual(payload["role"], "user")

    def test_unknown_role_rejected_at_mint_time(self):
        with self.assertRaises(AuthError):
            mint_token("alice", "superadmin")

    def test_expired_token_rejected(self):
        # PyJWT checks exp against its own internal clock, not core.auth's
        # imported time module, so build an already-expired token directly
        # rather than trying to mock time.time() out from under it.
        expired = pyjwt.encode({"sub": "alice", "role": "user", "iat": 0, "exp": 1}, "unit-test-secret", algorithm="HS256")
        with self.assertRaises(AuthError):
            decode_token(expired)

    def test_tampered_signature_rejected(self):
        token = mint_token("alice", "user")
        forged = pyjwt.encode({"sub": "alice", "role": "admin", "iat": 0, "exp": 9999999999}, "wrong-secret", algorithm="HS256")
        self.assertNotEqual(token, forged)
        with self.assertRaises(AuthError):
            decode_token(forged)

    def test_missing_secret_is_a_configuration_error_not_a_free_pass(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(AuthError):
                mint_token("alice", "user")

    def test_subject_with_role_rejects_wrong_role(self):
        token = mint_token("alice", "user")
        with self.assertRaises(AuthError):
            subject_with_role("Bearer " + token, "reviewer")

    def test_subject_with_role_accepts_matching_role(self):
        token = mint_token("alice", "reviewer")
        self.assertEqual(subject_with_role("Bearer " + token, "reviewer"), "alice")

    def test_missing_bearer_prefix_rejected(self):
        token = mint_token("alice", "user")
        with self.assertRaises(AuthError):
            subject_with_role(token, "user")


class RouteEnforcementTests(unittest.TestCase):
    def setUp(self):
        from api.action_routes import router, runtime
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict("os.environ", {
            "AGENT_STATE_PATH": self.temp.name + "/db", "AGENT_JWT_SECRET": "unit-test-secret",
            "AGENT_ADMIN_SECRET": "unit-test-admin-secret", "AGENT_USE_LLM": "0",
        })
        env.start()
        self.addCleanup(env.stop)
        runtime.cache_clear()
        self.addCleanup(runtime.cache_clear)
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def bearer(self, subject, role, ttl_seconds=3600):
        return {"Authorization": "Bearer " + mint_token(subject, role, ttl_seconds)}

    def test_auth_token_endpoint_requires_admin_secret(self):
        body = {"subject": "alice", "role": "user"}
        self.assertEqual(self.client.post("/agent/auth/token", json=body).status_code, 403)
        self.assertEqual(
            self.client.post("/agent/auth/token", json=body, headers={"X-Admin-Secret": "wrong"}).status_code, 403
        )
        response = self.client.post("/agent/auth/token", json=body, headers={"X-Admin-Secret": "unit-test-admin-secret"})
        self.assertEqual(response.status_code, 200)
        minted = response.json()["access_token"]
        self.assertEqual(subject_with_role("Bearer " + minted, "user"), "alice")

    def test_auth_token_endpoint_rejects_unknown_role(self):
        response = self.client.post(
            "/agent/auth/token", json={"subject": "alice", "role": "superadmin"},
            headers={"X-Admin-Secret": "unit-test-admin-secret"},
        )
        self.assertEqual(response.status_code, 400)

    def test_expired_token_rejected_by_owner_route(self):
        # mint_token enforces a positive ttl at mint time; build an
        # already-expired token directly to exercise the decode path.
        expired = pyjwt.encode({"sub": "alice", "role": "user", "iat": 0, "exp": 1}, "unit-test-secret", algorithm="HS256")
        response = self.client.post("/agent/demo/orders", headers={"Authorization": "Bearer " + expired})
        self.assertEqual(response.status_code, 401)

    def test_user_role_cannot_hit_reviewer_routes_and_vice_versa(self):
        user = self.bearer("alice", "user")
        reviewer = self.bearer("bob", "reviewer")
        self.assertEqual(self.client.get("/agent/review/tasks", headers=user).status_code, 403)
        self.assertEqual(self.client.post("/agent/demo/orders", headers=reviewer).status_code, 401)

    def test_two_distinct_users_cannot_see_each_others_tasks(self):
        alice, bob = self.bearer("alice", "user"), self.bearer("bob", "user")
        order = self.client.post("/agent/demo/orders", headers=alice).json()
        task = self.client.post("/agent/tasks", headers=alice, json={"message": "查询账单"}).json()
        self.assertEqual(self.client.get("/agent/tasks/" + task["id"], headers=alice).status_code, 200)
        self.assertEqual(self.client.get("/agent/tasks/" + task["id"], headers=bob).status_code, 404)
        self.assertEqual(self.client.post("/agent/demo/orders", headers=bob).json()["id"] != order["id"], True)

    def test_reviewer_cannot_approve_their_own_task(self):
        # Mint a token for the SAME subject under both roles — this is exactly
        # the case the old hardcoded "demo-user"/"demo-reviewer" strings could
        # never produce, so the reviewer==owner guard was previously dead code.
        same_subject = "carol"
        user_headers = self.bearer(same_subject, "user")
        reviewer_headers = self.bearer(same_subject, "reviewer")
        order = self.client.post("/agent/demo/orders", headers=user_headers).json()
        task = self.client.post("/agent/tasks", headers=user_headers, json={"message": "请申请退款，重复扣款"}).json()
        base = "/agent/tasks/" + task["id"]
        task = self.client.post(base + "/continue", headers=user_headers, json={"order_id": order["id"]}).json()
        task = self.client.post(base + "/confirmation", headers=user_headers,
                                json={"confirmation_id": task["confirmation"]["id"], "accepted": True}).json()
        body = {"approval_id": task["approval"]["id"], "approved": True}
        response = self.client.post(base + "/approval", headers=reviewer_headers, json=body)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Independent", response.json()["detail"])
        # The task itself is untouched — no silent partial state change.
        unchanged = self.client.get(base, headers=user_headers).json()
        self.assertEqual(unchanged["status"], "awaiting_approval")


if __name__ == "__main__":
    unittest.main()
