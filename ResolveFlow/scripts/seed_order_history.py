"""种子脚本：生成带时间跨度的模拟订单/退款历史（agents/action_runtime.py 的
order_events 表），供用户信任分工具开发/演示使用。

数据是合成的，不是任何外部数据集原始行的导入；三种行为原型（normal /
occasional_returner / serial_returner）的退货率区间参考了行业报告里报告的
真实退货率量级（正常电商整体退货率约 8%-12%，恶意/惯犯退货远高于此），仅用于
本地开发和演示，不代表任何真实用户。
"""
import random
from datetime import datetime, timedelta
from pathlib import Path

from agents.action_runtime import ActionRuntime

REFERENCE_DATE = datetime(2026, 9, 1)
SPAN_DAYS = 365
SEED = 20260901

ARCHETYPES = {
    "normal": dict(count=4, orders=(3, 8), refund_rate=(0.0, 0.15), refund_gap_days=(10, 60)),
    "occasional_returner": dict(count=4, orders=(5, 10), refund_rate=(0.15, 0.35), refund_gap_days=(5, 20)),
    "serial_returner": dict(count=4, orders=(8, 15), refund_rate=(0.45, 0.70), refund_gap_days=(0, 3)),
}


def _generate_owner_events(rng, owner, spec):
    n_orders = rng.randint(*spec["orders"])
    refund_rate = rng.uniform(*spec["refund_rate"])
    order_days = sorted(rng.sample(range(SPAN_DAYS), n_orders))
    events = []
    for i, day_offset in enumerate(order_days):
        order_id = f"{owner}-order-{i + 1}"
        purchased_at = REFERENCE_DATE - timedelta(days=SPAN_DAYS - day_offset)
        amount = round(rng.uniform(29, 399), 2)
        events.append(dict(order_id=order_id, event_type="purchase", amount=amount,
                            occurred_at=purchased_at.isoformat()))
        if rng.random() < refund_rate:
            gap = rng.randint(*spec["refund_gap_days"])
            refunded_at = purchased_at + timedelta(days=gap)
            if refunded_at <= REFERENCE_DATE:
                events.append(dict(order_id=order_id, event_type="refund", amount=amount,
                                    occurred_at=refunded_at.isoformat()))
    return events


def main():
    db_path = Path(__file__).resolve().parent.parent / "data" / "agent" / "state.sqlite3"
    runtime = ActionRuntime(db_path)
    rng = random.Random(SEED)
    total = 0
    for archetype, spec in ARCHETYPES.items():
        for i in range(spec["count"]):
            owner = f"{archetype}-{i + 1}"
            events = _generate_owner_events(rng, owner, spec)
            for event in events:
                runtime.record_order_event(owner, **event)
            total += len(events)
            print(f"seeded {owner}: {len(events)} events ({archetype})")
    print(f"done: {total} events -> {db_path}")


if __name__ == "__main__":
    main()
