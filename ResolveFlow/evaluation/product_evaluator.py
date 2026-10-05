"""Product Evaluation for the recruiting-demo Commerce chain.

Every case is replayed through the *real* isolated Demo host
(``api.portfolio_demo.create_app``) over HTTP with the same JWT identities the
browser uses:

    POST /chat → ConversationService → CommerceConversation → CommerceStore

User consent, reviewer decisions and attacks use the public Commerce routes.
Success is decided from the persisted SQLite state (objects, refunds, cases,
audit) of the session database — never from the assistant's wording, except for
read-only goals (policy answers, purchase queries, technical tool answers) where
the response/sources/tool trace *is* the product outcome.

This module only executes cases and records evidence. Metric arithmetic lives in
``product_metrics`` and failure classification in ``bad_case_analyzer``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data/product_eval/product_eval_cases.jsonl"
HOLDOUT = ROOT / "data/product_eval/product_eval_holdout.jsonl"
WRITE_OPS = {"refund", "cancel_renewal", "terminate", "cancel_order", "repair"}
USER_STEPS = {"user", "select", "confirm", "cancel"}
DAY = 86400


def load_cases(path=DATASET):
    cases = []
    for line_no, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            case = json.loads(line)
            case.setdefault("_line", line_no)
            cases.append(case)
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate product-eval case id")
    return cases


def dataset_digest(path=DATASET):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Session database helpers (read-only except for declared fixtures/system steps)
# ---------------------------------------------------------------------------

@contextmanager
def _db(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


class Session:
    """One demo identity (user + reviewer tokens) and its SQLite file."""

    def __init__(self, client, root, label):
        boot = client.post("/demo/session")
        boot.raise_for_status()
        data = boot.json()
        self.label = label
        self.owner = data["userId"]
        self.session = self.owner.split(":")[0]
        self.user = {"Authorization": "Bearer " + data["userToken"]}
        self.reviewer = {"Authorization": "Bearer " + data["reviewerToken"]}
        self.path = str(Path(root) / f"{self.session}.sqlite3")
        self.prefix = hashlib.sha256(self.owner.encode()).hexdigest()[:8]
        self.conversation = None

    def oid(self, slug, domain=None):
        if domain is None:
            with _db(self.path) as db:
                for d in ("G", "S"):
                    if db.execute("SELECT 1 FROM commerce_objects WHERE id=?", (f"{d}-{self.prefix}-{slug}",)).fetchone():
                        return f"{d}-{self.prefix}-{slug}"
            return f"G-{self.prefix}-{slug}"
        return f"{'G' if domain == 'goods' else 'S'}-{self.prefix}-{slug}"

    def slug(self, object_id):
        if not object_id:
            return None
        marker = f"-{self.prefix}-"
        return object_id.split(marker, 1)[1] if marker in object_id else "FOREIGN:" + object_id

    # -- persisted state ----------------------------------------------------
    def snapshot(self):
        with _db(self.path) as db:
            objects = {}
            for row in db.execute("SELECT * FROM commerce_objects WHERE owner=? ORDER BY id", (self.owner,)):
                body = json.loads(row["body"])
                inv = db.execute("SELECT status FROM commerce_invoices WHERE object_id=?", (row["id"],)).fetchone()
                refunds = [dict(r) for r in db.execute(
                    "SELECT id,amount_minor,status,item_id,payment_id,idempotency_key,created FROM commerce_refunds WHERE object_id=? ORDER BY created",
                    (row["id"],))]
                objects[self.slug(row["id"])] = {
                    "id": row["id"], "title": row["title"], "domain": row["domain"], "status": row["status"],
                    "version": row["version"], "auto_renew": body.get("auto_renew"),
                    "entitlement": body.get("entitlement"), "invoice_status": inv[0] if inv else None,
                    "refunds": refunds}
            return objects

    def cases(self):
        with _db(self.path) as db:
            return [json.loads(r[0]) for r in db.execute(
                "SELECT body FROM commerce_cases WHERE owner=? ORDER BY rowid", (self.owner,))]

    def audit(self):
        with _db(self.path) as db:
            return [dict(r) | {"body": json.loads(r["body"])} for r in db.execute(
                "SELECT case_id,actor,event,body,created FROM commerce_audit ORDER BY created, rowid")]

    def actions(self):
        """All persisted actions, oldest case first, with object slugs."""
        out = []
        for case in self.cases():
            for a in case["operations"]:
                q = a["quote"]
                out.append({"case_id": case["id"], "action_id": a["id"], "object": self.slug(q["object_id"]),
                            "operation": q["operation"], "status": a["status"], "amount_minor": q["amount_minor"],
                            "eligible": q["eligible"], "reason": a.get("reason") or q.get("reason"),
                            "decisions": a.get("decisions", [])})
        return out


# ---------------------------------------------------------------------------
# Fixtures: declared per case, applied before the first turn
# ---------------------------------------------------------------------------

def apply_fixture(sess, fixture):
    now = time.time()
    with _db(sess.path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        for spec in fixture.get("extra_objects", []):
            domain = spec.get("domain", "goods")
            oid = sess.oid(spec["slug"], domain)
            days, status, amount = spec.get("days", 1), spec.get("status", "delivered"), spec["amount_minor"]
            body = {"currency": "CNY", "purchased_at": now - days * DAY,
                    "delivered_at": now - days * DAY if status == "delivered" else None,
                    "shipping_minor": 0, "discount_minor": 0, "auto_renew": domain == "subscription",
                    "entitlement": spec["title"], "plan": spec["title"], "service": "healthy",
                    "period_start": now - days * DAY, "period_end": now + (30 - days) * DAY,
                    "used": spec.get("used", False), "policy_version": "demo-commerce-v2", "simulated": True}
            db.execute("INSERT INTO commerce_objects VALUES(?,?,?,?,?,1,?)",
                       (oid, sess.owner, domain, spec["title"], status, json.dumps(body)))
            if domain == "goods":
                db.execute("INSERT INTO commerce_items VALUES(?,?,?,?,?)", (oid + "-I1", oid, spec["title"], 1, amount))
                if status in {"shipped", "delivered"}:
                    events = [{"status": "shipped", "at": now - days * DAY}, {"status": status, "at": now - days * DAY}]
                    db.execute("INSERT INTO commerce_shipments VALUES(?,?,?,?)",
                               (oid, "模拟物流", "DEMO-" + spec["slug"], json.dumps(events)))
            if status != "unpaid":
                db.execute("INSERT INTO commerce_payments VALUES(?,?,?,?,?,?,?)",
                           (oid + "-P1", oid, amount, "CNY", spec.get("kind", "initial"), now - days * DAY, "captured"))
            db.execute("INSERT INTO commerce_invoices VALUES(?,?,?)", (oid, "DEMO-INV-" + spec["slug"], "not_requested"))
        for m in fixture.get("mutations", []):
            _mutate(db, sess, m)


def _mutate(db, sess, m):
    """Declared fixture / external-system change. Bumps version like the store."""
    oid = sess.oid(m["object"])
    row = db.execute("SELECT body,status FROM commerce_objects WHERE id=?", (oid,)).fetchone()
    body, status = json.loads(row[0]), row[1]
    body.update(m.get("body", {}))
    status = m.get("status", status)
    db.execute("UPDATE commerce_objects SET body=?,status=?,version=version+1 WHERE id=?", (json.dumps(body), status, oid))
    for r in m.get("add_refunds", []):
        payment = oid + "-" + r.get("payment", "P1")
        db.execute("INSERT INTO commerce_refunds VALUES(?,?,?,?,?,?,?,?)",
                   ("R-ext-" + str(uuid.uuid4()), oid, payment, r.get("item_id"), r["amount_minor"],
                    r.get("status", "succeeded_simulated"), "external-" + str(uuid.uuid4()), time.time()))


# ---------------------------------------------------------------------------
# Step execution
# ---------------------------------------------------------------------------

def _summarise(sess, response):
    case = response.get("commerce_case")
    return {"status": response.get("status"), "route": response.get("route"),
            "interpretation": (response.get("interpretation") or {}).get("mode"),
            "candidates": [sess.slug(c["id"]) for c in response.get("candidates", [])],
            "case_operations": [{"object": sess.slug(a["quote"]["object_id"]), "operation": a["quote"]["operation"],
                                 "status": a["status"], "amount_minor": a["quote"]["amount_minor"]}
                                for a in (case or {}).get("operations", [])],
            "sources": [s.get("document_id") for s in response.get("sources", [])],
            "tools_used": list((response.get("support_result") or {}).get("tools_used", [])),
            "degradations": response.get("degradations", []),
            "response": response.get("response", "")}


def _find_action(sess, spec, statuses):
    slug, op = spec.get("object"), spec.get("operation")
    for a in reversed(sess.actions()):
        if (slug is None or a["object"] == slug) and (op is None or a["operation"] == op) and a["status"] in statuses:
            return a
    return None


class Runner:
    def __init__(self, client, root):
        self.client, self.root = client, root

    def run_case(self, case):
        a = Session(self.client, self.root, "A")
        b = None
        apply_fixture(a, case.get("fixture", {}))
        initial = a.snapshot()
        trace, attacks, external_refunds, external_objects = [], [], set(), set()
        user_turns, chat_index = 0, 0
        first_clarification, first_write_proposal = None, None
        started = time.perf_counter()

        def chat(sess, text, order_id=None):
            payload = {"message": text, "conv_id": sess.conversation}
            if order_id:
                payload["order_id"] = order_id
            r = self.client.post("/chat", headers=sess.user, json=payload)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200:
                sess.conversation = body.get("conversation_id", sess.conversation)
            return r.status_code, body

        for index, step in enumerate(case["turns"]):
            kind = next(k for k in ("user", "select", "confirm", "cancel", "reviewer", "attack", "system") if k in step)
            record = {"step": index, "kind": kind, "spec": step}
            if kind in USER_STEPS:
                user_turns += 1
            if kind == "user":
                text = re.sub(r"\{id:([a-z0-9_]+)\}", lambda m: a.oid(m.group(1)), step["user"])
                status, body = chat(a, text)
                record.update(http=status, text=text, result=_summarise(a, body) if status == 200 else body)
            elif kind == "select":
                target = step["select"]
                last = next((t for t in reversed(trace) if t["kind"] in {"user", "select"} and t.get("http") == 200), None)
                shown = (last or {}).get("result", {}).get("candidates", [])
                if isinstance(target, str) and target.startswith("@candidate["):
                    i = int(target[len("@candidate["):-1])
                    target = shown[i] if i < len(shown) else None
                if target is None or target not in shown:
                    # The simulated user can only click a candidate the UI actually displayed.
                    record.update(skipped=True, note=f"candidate {step['select']} not displayed; shown={shown}")
                    user_turns -= 1
                else:
                    oid = a.oid(target)
                    status, body = chat(a, oid, order_id=oid)
                    record.update(http=status, text=oid, result=_summarise(a, body) if status == 200 else body)
            elif kind in {"confirm", "cancel"}:
                spec = step[kind]
                states = {"awaiting_confirmation"} if kind == "confirm" else {"awaiting_confirmation", "awaiting_approval"}
                action = _find_action(a, spec, states)
                if not action:
                    record.update(skipped=True, note=f"no {'/'.join(sorted(states))} action for {spec}")
                    user_turns -= 1
                else:
                    results = []
                    for _ in range(spec.get("repeat", 1)):
                        r = self.client.post(f"/commerce/cases/{action['case_id']}/decision", headers=a.user,
                                             json={"action_id": action["action_id"], "decision": kind})
                        results.append(r.status_code)
                    record.update(http=results, action=action)
            elif kind == "reviewer":
                decision = step["reviewer"]
                allowed = {"approve": {"awaiting_approval"}, "reject": {"awaiting_approval"},
                           "receive_return": {"awaiting_return"}, "settle": {"refund_processing"},
                           "fail": {"refund_processing"}}[decision]
                action = _find_action(a, step, allowed)
                if not action:
                    record.update(skipped=True, note=f"no action in {sorted(allowed)} for {step.get('object')}/{step.get('operation')}")
                else:
                    results = []
                    for _ in range(step.get("repeat", 1)):
                        r = self.client.post(f"/commerce/review/{action['case_id']}", headers=a.reviewer,
                                             json={"action_id": action["action_id"], "decision": decision})
                        results.append(r.status_code)
                    record.update(http=results, action=action)
            elif kind == "attack":
                if step["attack"].startswith("cross_user") and b is None:
                    b = Session(self.client, self.root, "B")
                outcome = self._attack(step, a, b, chat)
                record.update(attack=outcome)
                attacks.append(outcome)
            elif kind == "system":
                if step["system"] == "expire_quotes":
                    with _db(a.path) as db:
                        for row in db.execute("SELECT id,body FROM commerce_cases WHERE owner=?", (a.owner,)).fetchall():
                            body = json.loads(row[1])
                            for op in body["operations"]:
                                if op["status"] == "awaiting_confirmation":
                                    op["expires_at"] = time.time() - 1
                            db.execute("UPDATE commerce_cases SET body=? WHERE id=?", (json.dumps(body, ensure_ascii=False), row[0]))
                elif step["system"] == "mutate":
                    with _db(a.path) as db:
                        before = {r[0] for r in db.execute("SELECT id FROM commerce_refunds")}
                        _mutate(db, a, step)
                        external_refunds |= {r[0] for r in db.execute("SELECT id FROM commerce_refunds")} - before
                    external_objects.add(step["object"])
                record.update(applied=True)
            if kind in {"user", "select"} and record.get("http") == 200:
                res = record["result"]
                if res["status"] == "awaiting_clarification" and first_clarification is None:
                    first_clarification = index
                if first_write_proposal is None and any(o["operation"] in WRITE_OPS for o in res["case_operations"]):
                    first_write_proposal = index
            trace.append(record)

        final = a.snapshot()
        return {
            "case": case, "trace": trace, "attacks": attacks, "user_turns": user_turns,
            "initial": initial, "final": final, "actions": a.actions(), "audit": a.audit(), "owner": a.owner,
            "external_refunds": sorted(external_refunds), "external_objects": sorted(external_objects),
            "first_clarification": first_clarification, "first_write_proposal": first_write_proposal,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "slug_titles": {k: v["title"] for k, v in final.items()},
            "slug_ids": {k: v["id"] for k, v in final.items()},
        }

    def _attack(self, step, a, b, chat):
        """Each attack records whether the server blocked it AND whether state moved."""
        kind = step["attack"]
        before = a.snapshot()
        out = {"attack": kind, "http": None, "blocked": None}
        target = _find_action(a, step, {"awaiting_confirmation", "awaiting_approval", "awaiting_return",
                                       "refund_processing", "rejected", "cancelled", "completed", "stale"})
        needs_target = kind not in {"no_token", "cross_user_read", "cross_user_chat"}
        if needs_target and target is None and not (kind == "user_approves"):
            # The system never produced the action under attack; nothing to attempt.
            return out | {"skipped": True, "state_changed": False, "succeeded": False,
                          "note": f"no {step.get('object')}/{step.get('operation')} action to attack"}
        if kind == "user_approves":
            q = self.client.get("/commerce/review", headers=a.user).status_code
            r = self.client.post(f"/commerce/review/{target['case_id']}", headers=a.user,
                                 json={"action_id": target["action_id"], "decision": "approve"}) if target else None
            out["http"] = [q, r.status_code if r is not None else None]
            out["blocked"] = q in {401, 403} and (r is None or r.status_code in {401, 403})
        elif kind == "reviewer_confirms":
            r = self.client.post(f"/commerce/cases/{target['case_id']}/decision", headers=a.reviewer,
                                 json={"action_id": target["action_id"], "decision": "confirm"})
            out["http"] = r.status_code
            out["blocked"] = r.status_code in {401, 403}
        elif kind in {"reviewer_out_of_order", "approve_before_confirm", "approve_after_reject", "settle_before_return",
                      "approve_after_cancel"}:
            decision = step.get("decision", "approve")
            r = self.client.post(f"/commerce/review/{target['case_id']}", headers=a.reviewer,
                                 json={"action_id": target["action_id"], "decision": decision})
            out["http"] = r.status_code
            out["blocked"] = r.status_code >= 400
        elif kind == "no_token":
            r1 = self.client.post("/chat", json={"message": step.get("say", "帮我把无线耳机退掉")})
            r2 = self.client.post("/chat", headers={"Authorization": "Bearer forged.token.value"},
                                  json={"message": step.get("say", "帮我把无线耳机退掉")})
            out["http"] = [r1.status_code, r2.status_code]
            out["blocked"] = r1.status_code == 401 and r2.status_code == 401
        elif kind == "decision_field_injection":
            r = self.client.post(f"/commerce/cases/{target['case_id']}/decision", headers=a.user,
                                 json={"action_id": target["action_id"], "decision": "confirm", "amount_minor": 1})
            out["http"] = r.status_code
            out["blocked"] = r.status_code == 422
        elif kind == "cross_user_read":
            r = self.client.get("/commerce/objects/" + a.oid(step["object"]), headers=b.user)
            c = self.client.get("/commerce/cases", headers=b.user).json().get("cases", [])
            out["http"] = r.status_code
            out["blocked"] = r.status_code == 404 and not c
        elif kind == "cross_user_chat":
            status, body = chat(b, step["say"].replace("{id:" + step["object"] + "}", a.oid(step["object"])))
            case = body.get("commerce_case") or {}
            touched = [op["quote"]["object_id"] for op in case.get("operations", [])]
            out["http"] = status
            out["blocked"] = a.oid(step["object"]) not in touched
            out["b_case_objects"] = touched
        elif kind == "cross_user_decide":
            r = self.client.post(f"/commerce/cases/{target['case_id']}/decision", headers=b.user,
                                 json={"action_id": target["action_id"], "decision": "confirm"})
            out["http"] = r.status_code
            out["blocked"] = r.status_code >= 400
        else:
            raise ValueError("Unknown attack " + kind)
        after = a.snapshot()
        out["state_changed"] = _state_projection(before) != _state_projection(after)
        out["succeeded"] = (out["blocked"] is False) or out["state_changed"]
        return out


def _state_projection(snapshot):
    return {k: (v["status"], v["auto_renew"], v["entitlement"], tuple((r["id"], r["status"]) for r in v["refunds"]))
            for k, v in snapshot.items()}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(cases=None, live=False, data_path=DATASET):
    """Replay cases through the isolated Demo host; returns raw per-case results."""
    from fastapi.testclient import TestClient
    cases = cases if cases is not None else load_cases(data_path)
    results = []
    with tempfile.TemporaryDirectory(prefix="rf-product-eval-") as root:
        env = {"RESOLVEFLOW_DEMO_MODE": "true", "RESOLVEFLOW_DEMO_DIR": root}
        if not live:
            env["AGENT_USE_LLM"] = "0"
        with patch.dict(os.environ, env):
            from api import commerce_routes
            saved = commerce_routes.runtime, commerce_routes.conversation_service
            from api.portfolio_demo import create_app
            try:
                with TestClient(create_app()) as client:
                    mode = client.get("/health").json()["mode"]
                    runner = Runner(client, root)
                    for case in cases:
                        try:
                            results.append(runner.run_case(case))
                        except Exception as ex:  # harness error is recorded, never hidden
                            results.append({"case": case, "harness_error": f"{type(ex).__name__}: {ex}",
                                            "trace": [], "attacks": [], "user_turns": 0})
            finally:
                commerce_routes.runtime, commerce_routes.conversation_service = saved
    return mode, results
