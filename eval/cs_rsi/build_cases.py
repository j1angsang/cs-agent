# -*- coding: utf-8 -*-
"""生成客服策略校准评测集（离线，不依赖 DB / LLM）。

标签来自一版“目标策略配置”，用来模拟业务专家给出的边界：
- 退款单笔上限 3000，日累计 6000，日次数 2
- 补偿券上限 600，延迟 2 天
基线代码默认值是 2198 / 4396 / 1 / 439.60 / 3，因此会在边界样本上失败。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from policies.action_rules import decide_address_change, decide_agent_note, decide_coupon, decide_ticket, decide_urge
from policies.decision import RuleConfig
from policies.refund_policy import decide_refund

BASE = Path(__file__).resolve().parent
NOW = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
TARGET = RuleConfig(
    refund_amount_limit=Decimal("3000"),
    refund_daily_max_count=2,
    refund_daily_max_total=Decimal("6000"),
    coupon_amount_limit=Decimal("600"),
    coupon_delay_days=2,
    coupon_expire_days=30,
)


def user(uid: int = 1):
    return SimpleNamespace(id=uid)


def order(oid: int, *, uid: int = 1, status: str = "pending", amount: float = 1000,
          shipped_days: int | None = None):
    return SimpleNamespace(
        id=oid, user_id=uid, status=status, total_price=Decimal(str(amount)),
        shipped_at=(NOW - timedelta(days=shipped_days)) if shipped_days is not None else None,
    )


def session(sid: int = 1, uid: int = 1):
    return SimpleNamespace(id=sid, user_id=uid)


def refund_decision(o, recent_amounts: list[float], active: bool = False):
    recent = [SimpleNamespace(amount=Decimal(str(x))) for x in recent_amounts]
    return decide_refund(o, user(o.user_id), recent, TARGET, active_order_refund=active)


def add(rows: list[dict], cid: str, category: str, action: str, description: str,
        expected: str, payload: dict, expected_missed: list[str] | None = None):
    rows.append({
        "id": cid,
        "split": "dev" if len(rows) % 2 == 0 else "test",
        "category": category,
        "action": action,
        "description": description,
        "expected_level": expected,
        "expected_missed": expected_missed or [],
        "input": payload,
    })


def main() -> None:
    rows: list[dict] = []

    refund_specs = [
        ("待支付 1000，无历史退款", 1000, [], False, "pending"),
        ("待支付 2000，无历史退款", 2000, [], False, "pending"),
        ("待支付 2198，无历史退款", 2198, [], False, "pending"),
        ("待支付 2201，超过旧上限但未超过目标上限", 2201, [], False, "pending"),
        ("待支付 2500，目标内旧上限外", 2500, [], False, "pending"),
        ("待支付 2999，目标内旧上限外", 2999, [], False, "pending"),
        ("待支付 3000，目标边界", 3000, [], False, "pending"),
        ("待支付 3001，超过目标上限", 3001, [], False, "pending"),
        ("待支付 3500，超过目标上限", 3500, [], False, "pending"),
        ("待支付 1000，已有 1 笔 500 退款", 1000, [500], False, "pending"),
        ("待支付 2000，已有 1 笔 1000 退款", 2000, [1000], False, "pending"),
        ("待支付 2500，已有 1 笔 3000 退款", 2500, [3000], False, "pending"),
        ("待支付 3500，已有 1 笔 3000 退款", 3500, [3000], False, "pending"),
        ("待支付 1000，已有 2 笔 500 退款", 1000, [500, 500], False, "pending"),
        ("已签收 1000，退款需人工审核", 1000, [], False, "delivered"),
        ("已发货 1000，退款拒绝", 1000, [], False, "shipped"),
        ("已退款 1000，重复退款拒绝", 1000, [], False, "refunded"),
        ("退款中 1000，重复退款拒绝", 1000, [], False, "refund_pending"),
        ("待支付 1000，已有进行中退款行", 1000, [], True, "pending"),
        ("非本人订单 1000，拒绝", 1000, [], False, "pending"),
    ]
    for i, (desc, amount, recent, active, status) in enumerate(refund_specs, 1):
        uid = 1 if i != 20 else 2
        o = order(1000 + i, uid=uid, status=status, amount=amount)
        d = refund_decision(o, recent, active)
        add(
            rows, f"rf-{i:02d}", "refund", "refund", desc, d.level,
            {
                "user_id": uid, "order": {
                    "id": o.id, "user_id": o.user_id, "status": o.status,
                    "total_price": float(o.total_price),
                },
                "recent_amounts": recent, "active_order_refund": active,
            },
            d.rules_missed,
        )

    coupon_specs = [
        ("已发货 300 元，延迟 1 天", 300, 1, False),
        ("已发货 300 元，延迟 2 天", 300, 2, False),
        ("已发货 300 元，延迟 3 天", 300, 3, False),
        ("已发货 300 元，延迟 4 天", 300, 4, False),
        ("已发货 500 元，延迟 5 天", 500, 5, False),
        ("已发货 600 元，延迟 5 天", 600, 5, False),
        ("已发货 601 元，延迟 5 天", 601, 5, False),
        ("已发货 300 元，已发过券", 300, 5, True),
        ("已发货 300 元，发货时间缺失", 300, None, False),
        ("待支付 300 元，不满足状态", 300, 5, False),
        ("非本人订单 300 元，拒绝", 300, 5, False),
        ("已发货 300 元，延迟 10 天", 300, 10, False),
        ("已发货 100 元，延迟 4 天", 100, 4, False),
        ("已发货 700 元，延迟 4 天", 700, 4, False),
        ("已发货 439.6 元，延迟 4 天", 439.6, 4, False),
    ]
    for i, (desc, amount, delay, exists) in enumerate(coupon_specs, 1):
        uid = 1 if i != 11 else 2
        status = "pending" if i == 10 else "shipped"
        o = order(2000 + i, uid=uid, status=status, amount=amount,
                  shipped_days=delay)
        d = decide_coupon(o, user(uid), NOW, TARGET, coupon_exists=exists)
        add(
            rows, f"cp-{i:02d}", "coupon", "coupon", desc, d.level,
            {
                "user_id": uid, "order": {
                    "id": o.id, "user_id": o.user_id, "status": o.status,
                    "total_price": float(o.total_price),
                    "shipped_at": o.shipped_at.isoformat() if o.shipped_at else None,
                },
                "coupon_exists": exists, "now": NOW.isoformat(),
            },
            d.rules_missed,
        )

    address_specs = [
        ("待支付本人订单，允许改地址", 1, "pending", True),
        ("已发货本人订单，拒绝", 1, "shipped", True),
        ("已签收本人订单，拒绝", 1, "delivered", True),
        ("待支付非本人订单，拒绝", 2, "pending", True),
        ("待支付本人订单，重复确认仍允许", 1, "pending", True),
        ("已发货非本人订单，拒绝", 2, "shipped", True),
        ("已签收非本人订单，拒绝", 2, "delivered", True),
        ("待支付本人订单，金额 5000 仍允许", 1, "pending", True),
    ]
    for i, (desc, uid, status, _) in enumerate(address_specs, 1):
        o = order(3000 + i, uid=uid, status=status, amount=5000 if i == 8 else 1000)
        d = decide_address_change(o, user(uid), TARGET)
        add(rows, f"ad-{i:02d}", "address", "address", desc, d.level,
            {"user_id": uid, "order": {"id": o.id, "user_id": o.user_id, "status": o.status, "total_price": float(o.total_price)}},
            d.rules_missed)

    urge_specs = [
        ("已发货本人订单，允许催单", 1, "shipped"),
        ("待支付本人订单，拒绝", 1, "pending"),
        ("已签收本人订单，拒绝", 1, "delivered"),
        ("已发货非本人订单，拒绝", 2, "shipped"),
        ("已签收非本人订单，拒绝", 2, "delivered"),
        ("已发货本人订单，重复催单仍允许", 1, "shipped"),
    ]
    for i, (desc, uid, status) in enumerate(urge_specs, 1):
        o = order(4000 + i, uid=uid, status=status)
        d = decide_urge(o, user(uid), TARGET)
        add(rows, f"ur-{i:02d}", "urge", "urge", desc, d.level,
            {"user_id": uid, "order": {"id": o.id, "user_id": o.user_id, "status": o.status, "total_price": float(o.total_price)}},
            d.rules_missed)

    ticket_specs = [
        ("本人会话，无关联订单，允许建单", 1, 1, None, True),
        ("他人会话，拒绝", 2, 1, None, False),
        ("本人会话，订单属于他人，拒绝", 1, 1, 2, True),
        ("本人会话，订单属于本人，允许", 1, 1, 1, True),
        ("他人会话，即使订单属于本人，也拒绝", 2, 1, 1, False),
    ]
    for i, (desc, session_uid, order_uid, order_id, _) in enumerate(ticket_specs, 1):
        s = session(5000 + i, session_uid)
        o = None if order_id is None else order(order_id, uid=order_uid)
        d = decide_ticket(s, user(session_uid), o, TARGET)
        add(rows, f"tk-{i:02d}", "ticket", "ticket", desc, d.level,
            {"user_id": session_uid, "session_id": s.id, "session_owner_id": session_uid,
             "order": None if o is None else {"id": o.id, "user_id": o.user_id, "status": o.status, "total_price": float(o.total_price)}},
            d.rules_missed)

    note_specs = [
        ("本人会话写备注，允许", 1, 1),
        ("他人会话写备注，拒绝", 2, 1),
        ("本人会话无订单，允许", 1, 1),
    ]
    for i, (desc, uid, session_uid) in enumerate(note_specs, 1):
        d = decide_agent_note(session(6000 + i, session_uid), user(uid), TARGET)
        add(rows, f"nt-{i:02d}", "note", "note", desc, d.level,
            {"user_id": uid, "session_id": session_uid, "session_owner_id": session_uid}, d.rules_missed)

    permission_specs = [
        ("退款：非本人订单", 2, "refund"),
        ("改地址：非本人订单", 2, "address"),
        ("工单：非本人会话", 2, "ticket"),
    ]
    for i, (desc, uid, action) in enumerate(permission_specs, 1):
        o = order(7000 + i, uid=1, status="pending")
        s = session(7000 + i, 1)
        if action == "refund":
            d = decide_refund(o, user(uid), [], TARGET)
        elif action == "address":
            d = decide_address_change(o, user(uid), TARGET)
        else:
            d = decide_ticket(s, user(uid), o, TARGET)
        add(rows, f"pm-{i:02d}", "permission", action, desc, d.level,
            {"user_id": uid, "order": {"id": o.id, "user_id": o.user_id, "status": o.status, "total_price": float(o.total_price)}, "session_id": s.id, "session_owner_id": s.user_id},
            d.rules_missed)

    # 保证退款“日累计额度”边界在 dev/test 都有样本
    base_refund = next(row for row in rows if row["id"] == "rf-12")
    rows.append({
        "id": "rf-21",
        "split": "test",
        "category": "refund",
        "action": "refund",
        "description": "待支付 2500，已有 1 笔 3000 退款（test 同界样本）",
        "expected_level": "auto",
        "expected_missed": [],
        "input": base_refund["input"],
    })

    # 重新按顺序分 split，并固定关键边界样本位置，保证 dev/test 都能校准同一条规则
    split_overrides = {"rf-12": "dev", "rf-21": "test"}
    for i, row in enumerate(rows):
        row["split"] = split_overrides.get(row["id"], "dev" if i % 2 == 0 else "test")

    out = BASE / "cases.jsonl"
    out.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    print("cases", len(rows), "dev", sum(r["split"] == "dev" for r in rows), "test", sum(r["split"] == "test" for r in rows))
    from collections import Counter
    print("categories", Counter(r["category"] for r in rows))
    print("levels", Counter(r["expected_level"] for r in rows))


if __name__ == "__main__":
    main()
