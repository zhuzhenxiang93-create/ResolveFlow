"""Versioned SQLite commerce ledger and object-bound, confirmed after-sales.

External fulfilment/payment operations are explicitly simulated. Monetary values
are CNY fen. All ownership and eligibility checks live here, not in the model.
"""
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

POLICY = "demo-commerce-v2"
DAY = 86400
WRITE_OPS = {"refund", "cancel_renewal", "terminate", "cancel_order", "repair"}
LABELS = {"refund": "申请退款", "cancel_renewal": "关闭自动续费（保留本期权益）", "terminate": "立即终止订阅（不自动退款）", "cancel_order": "取消未支付订单", "repair": "同步订阅权益", "read": "查询", "logistics": "查询物流", "invoice": "查询发票", "progress": "查询售后进度"}


class CommerceStore:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS commerce_migrations(version INTEGER PRIMARY KEY, applied REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS commerce_objects(id TEXT PRIMARY KEY, owner TEXT NOT NULL, domain TEXT NOT NULL CHECK(domain IN ('goods','subscription')), title TEXT NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS commerce_owner ON commerce_objects(owner,domain);
            CREATE TABLE IF NOT EXISTS commerce_items(id TEXT PRIMARY KEY, object_id TEXT NOT NULL REFERENCES commerce_objects(id), title TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>0), paid_minor INTEGER NOT NULL CHECK(paid_minor>=0));
            CREATE TABLE IF NOT EXISTS commerce_payments(id TEXT PRIMARY KEY, object_id TEXT NOT NULL REFERENCES commerce_objects(id), amount_minor INTEGER NOT NULL CHECK(amount_minor>=0), currency TEXT NOT NULL, kind TEXT NOT NULL, captured_at REAL NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS commerce_shipments(object_id TEXT PRIMARY KEY REFERENCES commerce_objects(id), carrier TEXT, tracking TEXT, events TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS commerce_invoices(object_id TEXT PRIMARY KEY REFERENCES commerce_objects(id), number TEXT, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS commerce_cases(id TEXT PRIMARY KEY, owner TEXT NOT NULL, conversation TEXT NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS commerce_conversation ON commerce_cases(owner,conversation);
            CREATE TABLE IF NOT EXISTS commerce_refunds(id TEXT PRIMARY KEY, object_id TEXT NOT NULL REFERENCES commerce_objects(id), payment_id TEXT NOT NULL REFERENCES commerce_payments(id), item_id TEXT, amount_minor INTEGER NOT NULL CHECK(amount_minor>0), status TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS commerce_audit(id TEXT PRIMARY KEY, case_id TEXT NOT NULL, actor TEXT NOT NULL, event TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL);
            ''')
            db.execute("INSERT OR IGNORE INTO commerce_migrations VALUES(1,?)", (time.time(),))
            # Preserve identifiers and ownership without inventing financial evidence.
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='orders'").fetchone():
                for row in db.execute("SELECT id,owner,body FROM orders").fetchall():
                    old = json.loads(row["body"])
                    body = {"source": "legacy_snapshot", "evidence_complete": False,
                            "plan": old.get("plan"), "entitlement": old.get("entitlement"),
                            "auto_renew": old.get("auto_renew"), "service": old.get("service"),
                            "currency": None, "simulated": True,
                            "missing_evidence": ["payment_records", "billing_period", "refund_amounts"]}
                    db.execute("INSERT OR IGNORE INTO commerce_objects VALUES(?,?,?,?,?,1,?)",
                               (row["id"], row["owner"], "subscription", "历史订阅 " + str(old.get("plan", "")),
                                "evidence_required", json.dumps(body)))
            db.execute("INSERT OR IGNORE INTO commerce_migrations VALUES(2,?)", (time.time(),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def seed(self, owner):
        """Idempotent fixtures, stable per owner; never reset changed state."""
        prefix = hashlib.sha256(owner.encode()).hexdigest()[:8]
        now = time.time()
        rows = [
            ("headphones", "goods", "无线耳机", "delivered", 25900, 1, "normal"),
            ("keyboard", "goods", "机械键盘与鼠标套装", "paid", 39900, 2, "normal"),
            ("cable", "goods", "充电线", "unpaid", 3900, 0, "normal"),
            ("speaker", "goods", "蓝牙音箱", "shipped", 19900, 2, "normal"),
            ("old", "goods", "过期相机", "delivered", 59900, 45, "normal"),
            ("pro", "subscription", "Pro 月度会员", "active", 9900, 2, "duplicate"),
            ("basic", "subscription", "Basic 月度会员", "active", 3900, 1, "renewal"),
            ("used", "subscription", "已使用的年度会员", "active", 99900, 40, "initial"),
        ]
        with self.connect() as db:
            for slug, domain, title, status, amount, days, kind in rows:
                oid = f"{'G' if domain == 'goods' else 'S'}-{prefix}-{slug}"
                body = {"currency": "CNY", "purchased_at": now - days * DAY,
                        "delivered_at": now - days * DAY if status == "delivered" else None,
                        "shipping_minor": 1000 if slug == "keyboard" else 0,
                        "discount_minor": 2000 if slug == "keyboard" else 0,
                        "auto_renew": domain == "subscription", "entitlement": "basic" if slug == "pro" else title,
                        "plan": title, "service": "healthy", "period_start": now-days*DAY,
                        "period_end": now+(30-days)*DAY, "used": slug == "used", "policy_version": POLICY, "simulated": True}
                inserted = db.execute("INSERT OR IGNORE INTO commerce_objects VALUES(?,?,?,?,?,1,?)", (oid, owner, domain, title, status, json.dumps(body))).rowcount
                if not inserted:
                    continue
                if domain == "goods":
                    items = [("键盘", 29900), ("鼠标", 9000)] if slug == "keyboard" else [(title, amount)]
                    for i, (name, paid) in enumerate(items):
                        db.execute("INSERT INTO commerce_items VALUES(?,?,?,?,?)", (f"{oid}-I{i+1}", oid, name, 1, paid))
                    if status in {"shipped", "delivered"}:
                        events = [{"status": "shipped", "at": now-days*DAY}, {"status": status, "at": now-days*DAY}]
                        db.execute("INSERT INTO commerce_shipments VALUES(?,?,?,?)", (oid, "模拟物流", f"DEMO-{slug}", json.dumps(events)))
                if status != "unpaid":
                    db.execute("INSERT INTO commerce_payments VALUES(?,?,?,?,?,?,?)", (oid+"-P1", oid, amount, "CNY", "initial" if kind == "duplicate" else kind, now-days*DAY, "captured"))
                    if kind == "duplicate":
                        db.execute("INSERT INTO commerce_payments VALUES(?,?,?,?,?,?,?)", (oid+"-P2", oid, amount, "CNY", "duplicate", now-days*DAY, "captured"))
                db.execute("INSERT INTO commerce_invoices VALUES(?,?,?)", (oid, f"DEMO-INV-{slug}", "issued" if slug in {"headphones", "pro"} else "not_requested"))
        return self.catalog(owner)

    def catalog(self, owner):
        with self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM commerce_objects WHERE owner=? ORDER BY id", (owner,))]
            return [self._object(db, owner, oid) for oid in ids]

    def _object(self, db, owner, oid):
        row = db.execute("SELECT * FROM commerce_objects WHERE id=? AND owner=?", (oid, owner)).fetchone()
        if not row:
            raise ValueError("业务记录不存在或无权访问")
        result = {**dict(row), **json.loads(row["body"])}
        result.pop("body")
        result["items"] = [dict(r) for r in db.execute("SELECT * FROM commerce_items WHERE object_id=?", (oid,))]
        result["payments"] = [dict(r) for r in db.execute("SELECT * FROM commerce_payments WHERE object_id=?", (oid,))]
        result["refunds"] = [dict(r) for r in db.execute("SELECT * FROM commerce_refunds WHERE object_id=?", (oid,))]
        shipment = db.execute("SELECT * FROM commerce_shipments WHERE object_id=?", (oid,)).fetchone()
        result["shipment"] = dict(shipment) if shipment else None
        if shipment:
            result["shipment"]["events"] = json.loads(shipment["events"])
        invoice = db.execute("SELECT * FROM commerce_invoices WHERE object_id=?", (oid,)).fetchone()
        result["invoice"] = dict(invoice) if invoice else None
        return result

    def detail(self, owner, oid):
        with self.connect() as db:
            return self._object(db, owner, oid)

    def quote(self, obj, op, item_id=None, payment_id=None):
        if op not in WRITE_OPS | {"read", "logistics", "invoice", "progress"}:
            raise ValueError("未知业务操作")
        q = {"operation": op, "label": LABELS[op], "object_id": obj["id"], "title": obj["title"],
             "version": obj["version"], "policy_version": POLICY, "amount_minor": 0,
             "currency": "CNY", "item_id": item_id, "payment_id": payment_id, "eligible": True,
             "effect": "只查询，不修改业务", "requires_review": op == "refund", "simulated": True}
        if op not in WRITE_OPS:
            return q
        if obj.get("evidence_complete") is False:
            q.update(eligible=False, reason="历史记录缺少具体账单、计费周期和退款金额证据；请补齐核实后重新申请，旧计数不能作为执行依据")
            return q
        if op == "terminate":
            if obj["domain"] != "subscription":
                raise ValueError("该操作仅适用于订阅")
            q.update(eligible=False, reason="不提供独立立即收回权益的操作。取消订阅会关闭续费并保留本期权益；本期退款须另行申请确认")
            return q
        if op in {"cancel_renewal", "terminate", "repair"}:
            if obj["domain"] != "subscription":
                raise ValueError("该操作仅适用于订阅")
            q["effect"] = {"cancel_renewal": "停止未来续费；保留本期权益；不退款", "terminate": "立即移除权益并关闭续费；不退款", "repair": "同步到已购买套餐权益；不退款"}[op]
            if obj["status"] != "active" or (op == "repair" and obj["service"] != "healthy"):
                q.update(eligible=False, reason="订阅非活跃或服务异常，需要人工核实")
            return q
        if op == "cancel_order":
            q["effect"] = "取消未支付商品订单；没有退款"
            q["eligible"] = obj["domain"] == "goods" and obj["status"] == "unpaid"
            if not q["eligible"]:
                q["reason"] = "仅未支付商品订单可直接取消；已支付请申请售后"
            return q
        payments = [p for p in obj["payments"] if p["status"] == "captured"]
        if payment_id:
            payments = [p for p in payments if p["id"] == payment_id]
        elif obj["domain"] == "subscription":
            duplicate = [p for p in payments if p["kind"] == "duplicate"]
            payments = duplicate or payments
        if len(payments) != 1:
            q.update(eligible=False, reason="请选择具体扣款记录，或当前没有可退的已支付记录")
            return q
        pay = payments[0]
        q["payment_id"] = pay["id"]
        reserved = [r for r in obj["refunds"] if r["payment_id"] == pay["id"] and r["status"] not in {"failed", "cancelled"}]
        remaining = pay["amount_minor"] - sum(r["amount_minor"] for r in reserved)
        if obj["domain"] == "goods":
            if obj["status"] not in {"paid", "shipped", "delivered"}:
                q.update(eligible=False, reason="订单状态不支持退款")
            elif obj["delivered_at"] and time.time()-obj["delivered_at"] > 7*DAY:
                q.update(eligible=False, reason="超过演示规则的签收后7天期限，需人工评估")
            selected = [i for i in obj["items"] if not item_id or i["id"] == item_id]
            if not selected:
                raise ValueError("商品明细不属于该订单")
            if item_id:
                previous = sum(r["amount_minor"] for r in reserved if r["item_id"] == item_id)
                # A full-order refund reserves every line; no further line refunds.
                if any(r["item_id"] is None for r in reserved):
                    previous = selected[0]["paid_minor"]
                q["amount_minor"] = min(remaining, max(0, selected[0]["paid_minor"]-previous))
            else:
                q["amount_minor"] = remaining
            q["effect"] = "按优惠分摊后的实付金额退款；单品退款不退运费，全单退剩余实付款；已开发票进入调整流程"
            q["return_required"] = obj["status"] in {"shipped", "delivered"}
            if q["return_required"]:
                q["effect"] += "；先等待退货验收，再进入模拟退款处理"
        else:
            duplicate = pay["kind"] == "duplicate"
            if obj["status"] != "active" or (not duplicate and (obj["used"] or time.time()-pay["captured_at"] > 7*DAY)):
                q.update(eligible=False, reason="非重复扣款仅支持扣款7天内且未使用的测试订阅退款；其他情况需人工评估")
            q["amount_minor"] = remaining
            q["effect"] = "仅退重复扣款；保留订阅权益和续费设置" if duplicate else "退本笔订阅费用；终止当前订阅权益并关闭续费"
            q["end_subscription"] = not duplicate
        if q["amount_minor"] <= 0:
            q.update(eligible=False, reason="无剩余可退金额，或已有售后占用该金额")
        return q

    def _audit(self, db, case, actor, event, body):
        db.execute("INSERT INTO commerce_audit VALUES(?,?,?,?,?,?)", (str(uuid.uuid4()), case["id"], actor, event, json.dumps(body, ensure_ascii=False), time.time()))

    def _save(self, db, case):
        states = [a["status"] for a in case["operations"]]
        case["status"] = next((s for s in ("awaiting_confirmation", "awaiting_approval", "awaiting_return", "refund_processing") if s in states), "completed")
        case["response"] = "\n".join(self._describe(a) for a in case["operations"])
        case["updated_at"] = time.time()
        db.execute("INSERT INTO commerce_cases VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (case["id"], case["owner"], case["conversation_id"], json.dumps(case, ensure_ascii=False)))
        return case

    @staticmethod
    def _describe(a):
        labels = {"awaiting_confirmation": "待用户确认", "awaiting_approval": "待独立审核", "awaiting_return": "等待退货验收", "refund_processing": "模拟退款处理中", "completed": "已完成（模拟业务）", "rejected": "已拒绝", "cancelled": "已取消", "stale": "数据已变更，请重新申请", "ineligible": "不符合演示规则"}
        q = a["quote"]
        return f"{q['title']} · {q['label']} · {labels.get(a['status'], a['status'])}" + (f" · CNY {q['amount_minor']/100:.2f}" if q["amount_minor"] else "") + "：" + (a.get("reason") or q.get("reason") or q["effect"])

    def create_case(self, owner, conversation, proposals):
        case = {"id": "C-"+str(uuid.uuid4()), "owner": owner, "conversation_id": conversation, "operations": [], "simulated": True}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for p in proposals:
                obj = self._object(db, owner, p["object_id"])
                q = self.quote(obj, p["operation"], p.get("item_id"), p.get("payment_id"))
                action = {"id": str(uuid.uuid4()), "quote": q, "expires_at": time.time()+900,
                          "status": "ineligible" if not q["eligible"] else "awaiting_confirmation" if p["operation"] in WRITE_OPS else "completed"}
                if p["operation"] not in WRITE_OPS:
                    action["data"] = obj
                case["operations"].append(action)
            self._audit(db, case, owner, "proposed", proposals)
            return self._save(db, case)

    def get_case(self, owner, case_id):
        with self.connect() as db:
            return self._case(db, owner, case_id)

    @staticmethod
    def _case(db, owner, case_id):
        row = db.execute("SELECT body FROM commerce_cases WHERE id=? AND owner=?", (case_id, owner)).fetchone()
        if not row:
            raise ValueError("售后任务不存在或无权访问")
        return json.loads(row[0])

    def cases(self, owner, conversation=None):
        with self.connect() as db:
            rows = db.execute("SELECT body FROM commerce_cases WHERE owner=?" + (" AND conversation=?" if conversation else "") + " ORDER BY rowid DESC LIMIT 50", (owner, conversation) if conversation else (owner,)).fetchall()
            return [json.loads(r[0]) for r in rows]

    def review_queue(self):
        with self.connect() as db:
            rows = db.execute("SELECT body FROM commerce_cases ORDER BY rowid DESC LIMIT 100").fetchall()
        return [c for c in (json.loads(r[0]) for r in rows) if any(a["status"] in {"awaiting_approval", "awaiting_return", "refund_processing"} for a in c["operations"])]

    def transition(self, owner, case_id, action_id, decision, actor):
        """actor is server-authenticated; API restricts reviewer decisions separately."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            case = self._case(db, owner, case_id)
            a = next((a for a in case["operations"] if a["id"] == action_id), None)
            if not a:
                raise ValueError("操作不存在")
            if decision not in {"confirm", "cancel", "approve", "reject", "receive_return", "settle", "fail"}:
                raise ValueError("未知状态转换")
            if decision in {"confirm", "cancel"}:
                if actor != owner:
                    raise ValueError("只能由任务用户确认或取消")
            elif actor == owner:
                raise ValueError("禁止用户自批")
            if decision in a.get("decisions", []):
                return case  # persisted idempotency, including after restart
            allowed = {"confirm": {"awaiting_confirmation"}, "cancel": {"awaiting_confirmation", "awaiting_approval"}, "approve": {"awaiting_approval"}, "reject": {"awaiting_approval"}, "receive_return": {"awaiting_return"}, "settle": {"refund_processing"}, "fail": {"refund_processing"}}
            if a["status"] not in allowed[decision]:
                raise ValueError("当前状态不允许该操作")
            q = a["quote"]
            obj = self._object(db, owner, q["object_id"])
            if decision in {"confirm", "approve"}:
                current = self.quote(obj, q["operation"], q.get("item_id"), q.get("payment_id"))
                if a["expires_at"] < time.time() or current != q:
                    a.update(status="stale", reason="确认或审批已过期，或金额/业务状态发生变化；请重新查询申请")
                    self._audit(db, case, actor, "stale", {"action_id": action_id})
                    return self._save(db, case)
            if decision in {"cancel", "reject"}:
                a["status"] = "cancelled" if decision == "cancel" else "rejected"
            elif decision == "confirm":
                if q["requires_review"]:
                    a["status"] = "awaiting_approval"
                else:
                    self._mutate(db, obj, q["operation"])
                    a["status"] = "completed"
            elif decision == "approve":
                rid = "R-"+str(uuid.uuid4())
                a["status"] = "awaiting_return" if q.get("return_required") else "refund_processing"
                db.execute("INSERT INTO commerce_refunds VALUES(?,?,?,?,?,?,?,?)", (rid, q["object_id"], q["payment_id"], q.get("item_id"), q["amount_minor"], a["status"], action_id, time.time()))
                a["refund_id"] = rid
                # Reservation changes eligibility of any other outstanding proposals.
                db.execute("UPDATE commerce_objects SET version=version+1 WHERE id=?", (obj["id"],))
            elif decision == "receive_return":
                a["status"] = "refund_processing"
                db.execute("UPDATE commerce_refunds SET status=? WHERE id=?", (a["status"], a["refund_id"]))
            elif decision == "fail":
                a.update(status="rejected", reason="模拟支付失败；没有退款成功，可重新查询申请")
                db.execute("UPDATE commerce_refunds SET status='failed' WHERE id=?", (a["refund_id"],))
                db.execute("UPDATE commerce_objects SET version=version+1 WHERE id=?", (obj["id"],))
            elif decision == "settle":
                db.execute("UPDATE commerce_refunds SET status='succeeded_simulated' WHERE id=?", (a["refund_id"],))
                if q.get("end_subscription"):
                    self._mutate(db, obj, "terminate")
                else:
                    db.execute("UPDATE commerce_objects SET version=version+1 WHERE id=?", (obj["id"],))
                db.execute("UPDATE commerce_invoices SET status='adjustment_required' WHERE object_id=? AND status='issued'", (obj["id"],))
                a.update(status="completed", reason="模拟账务已核验；不是支付渠道到账回执。"+q["effect"])
            a.setdefault("decisions", []).append(decision)
            self._audit(db, case, actor, decision, {"action_id": action_id, "quote": q})
            return self._save(db, case)

    @staticmethod
    def _mutate(db, obj, op):
        row = db.execute("SELECT body FROM commerce_objects WHERE id=?", (obj["id"],)).fetchone()
        body = json.loads(row[0])
        status = obj["status"]
        if op == "cancel_renewal":
            body["auto_renew"] = False
        elif op == "terminate":
            body.update(auto_renew=False, entitlement="none")
            status = "terminated"
        elif op == "repair":
            body["entitlement"] = body["plan"]
        elif op == "cancel_order":
            status = "cancelled"
        db.execute("UPDATE commerce_objects SET body=?,status=?,version=version+1 WHERE id=?", (json.dumps(body), status, obj["id"]))
