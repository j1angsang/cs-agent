# -*- coding: utf-8 -*-
"""客服策略校准的离线 RSI 最小闭环。

诊断 -> 编辑阈值配置 -> dev 回归 -> test 门控 -> 回滚/写入。

边界：
- 评测对象是确定性策略（退款、优惠券、改地址、催单、工单、备注、权限）；
- 编辑对象是 RuleConfig 的 6 个阈值，不改代码、不改规则函数；
- 不是生产级 RSI，不上线；自动审批只用于演示。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from policies.action_rules import decide_address_change, decide_agent_note, decide_coupon, decide_ticket, decide_urge
from policies.decision import RuleConfig
from policies.refund_policy import decide_refund

BASE = Path(__file__).resolve().parent
CASES = BASE / "cases.jsonl"
RESULTS = BASE / "results"
WORK = BASE / "rsi_workspace"
HISTORY = WORK / "history"
CONFIG = WORK / "config.json"
DEFAULT_MODEL = "qwen3.5:4b-q4_K_M"


@dataclass(frozen=True)
class CSConfig:
    refund_amount_limit: float = 2198.0
    refund_daily_max_count: int = 1
    refund_daily_max_total: float = 4396.0
    coupon_amount_limit: float = 439.6
    coupon_delay_days: int = 3
    coupon_expire_days: int = 30

    def as_rule_config(self) -> RuleConfig:
        return RuleConfig(
            refund_amount_limit=Decimal(str(self.refund_amount_limit)),
            refund_daily_max_count=self.refund_daily_max_count,
            refund_daily_max_total=Decimal(str(self.refund_daily_max_total)),
            coupon_amount_limit=Decimal(str(self.coupon_amount_limit)),
            coupon_delay_days=self.coupon_delay_days,
            coupon_expire_days=self.coupon_expire_days,
        )


def load_cases() -> list[dict]:
    return [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]


def parse_now(raw: str | None) -> datetime:
    if not raw:
        return datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def build_objects(case: dict):
    inp = case["input"]
    uid = int(inp["user_id"])
    user = SimpleNamespace(id=uid)
    order_data = inp.get("order")
    order = None
    if order_data:
        order = SimpleNamespace(
            id=int(order_data["id"]),
            user_id=int(order_data["user_id"]),
            status=order_data["status"],
            total_price=Decimal(str(order_data["total_price"])),
            shipped_at=datetime.fromisoformat(order_data["shipped_at"]) if order_data.get("shipped_at") else None,
        )
    session = SimpleNamespace(
        id=int(inp.get("session_id", 0)),
        user_id=int(inp.get("session_owner_id", uid)),
    )
    recent = [SimpleNamespace(amount=Decimal(str(x))) for x in inp.get("recent_amounts", [])]
    return user, order, session, recent


def decide(case: dict, cfg: CSConfig) -> str:
    user, order, session, recent = build_objects(case)
    rule_cfg = cfg.as_rule_config()
    action = case["action"]
    if action == "refund":
        return decide_refund(order, user, recent, rule_cfg, active_order_refund=bool(case["input"].get("active_order_refund"))).level
    if action == "coupon":
        return decide_coupon(order, user, parse_now(case["input"].get("now")), rule_cfg, coupon_exists=bool(case["input"].get("coupon_exists"))).level
    if action == "address":
        return decide_address_change(order, user, rule_cfg).level
    if action == "urge":
        return decide_urge(order, user, rule_cfg).level
    if action == "ticket":
        return decide_ticket(session, user, order, rule_cfg).level
    if action == "note":
        return decide_agent_note(session, user, rule_cfg).level
    raise ValueError(f"unknown action: {action}")


def evaluate(cases: list[dict], cfg: CSConfig) -> dict:
    rows = []
    for case in cases:
        actual = decide(case, cfg)
        passed = actual == case["expected_level"]
        rows.append({
            "id": case["id"],
            "category": case["category"],
            "action": case["action"],
            "description": case["description"],
            "expected": case["expected_level"],
            "actual": actual,
            "passed": passed,
        })

    def mean(vals):
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    categories = sorted({r["category"] for r in rows})
    return {
        "count": len(rows),
        "quality": mean([1.0 if r["passed"] else 0.0 for r in rows]),
        "by_category": {
            c: mean([1.0 if r["passed"] else 0.0 for r in rows if r["category"] == c])
            for c in categories
        },
        "rows": rows,
    }


def failure_rows(evaluation: dict) -> list[dict]:
    return [r for r in evaluation["rows"] if not r["passed"]]


def call_model(prompt: str, model: str) -> str:
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "format": "json",
        "think": False,
        "stream": False,
        "options": {"temperature": 0.2, "num_ctx": 2048, "num_predict": 1200},
    }).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=240) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    raw = data.get("response", "")
    if not raw.strip():
        raise RuntimeError(f"empty response; done_reason={data.get('done_reason')} eval={data.get('eval_count')}")
    return raw


def parse_json(raw: str) -> dict:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object: {raw[:300]!r}")
    return json.loads(raw[start:end + 1])


def diagnose(rows: list[dict], model: str) -> dict:
    failures = failure_rows({"rows": rows})[:12]
    payload = [
        {
            "id": r["id"], "action": r["action"], "category": r["category"],
            "description": r["description"], "expected": r["expected"], "actual": r["actual"],
        }
        for r in failures
    ]
    prompt = f"""你是客服策略诊断器。只输出 JSON。
失败类型：threshold（阈值边界）、quota（次数/累计额度）、window（券延迟窗口）、amount（金额上限）、status（订单状态）、permission（归属权限）、other。
输出格式：
{{"summary":"一句话","failures":[{{"id":"...","type":"...","reason":"一句话"}}]}}

失败样本：
{json.dumps(payload, ensure_ascii=False)}"""
    try:
        return parse_json(call_model(prompt, model))
    except Exception as exc:
        print(f"diagnose fallback: {type(exc).__name__}: {exc}")
        return {"summary": "fallback diagnosis", "failures": []}


def normalize(data: dict, current: CSConfig) -> CSConfig | None:
    fields = {
        "refund_amount_limit": (0.0, 10000.0, float),
        "refund_daily_max_count": (0, 5, int),
        "refund_daily_max_total": (0.0, 20000.0, float),
        "coupon_amount_limit": (0.0, 2000.0, float),
        "coupon_delay_days": (0, 10, int),
        "coupon_expire_days": (1, 90, int),
    }
    values = asdict(current)
    for key, (low, high, typ) in fields.items():
        if key not in data:
            continue
        try:
            val = typ(data[key])
        except (TypeError, ValueError):
            return None
        if not low <= val <= high:
            return None
        values[key] = val
    return CSConfig(**values)


def config_diff(old: CSConfig, new: CSConfig) -> dict:
    before, after = asdict(old), asdict(new)
    return {k: {"from": before[k], "to": after[k]} for k in before if before[k] != after[k]}


def edit(rows: list[dict], diagnosis: dict, current: CSConfig, rejected: list[dict], model: str) -> tuple[CSConfig | None, dict, str]:
    failures = failure_rows({"rows": rows})[:12]
    prompt = f"""你是客服策略阈值编辑器。只能修改以下 6 个字段，不能改代码/规则函数/评测集：
- refund_amount_limit: 0..10000
- refund_daily_max_count: 0..5
- refund_daily_max_total: 0..20000
- coupon_amount_limit: 0..2000
- coupon_delay_days: 0..10
- coupon_expire_days: 1..90

当前配置：
{json.dumps(asdict(current), ensure_ascii=False)}

诊断：
{json.dumps(diagnosis, ensure_ascii=False)}

之前被拒绝的修改：
{json.dumps(rejected[-3:], ensure_ascii=False)}

失败样本：
{json.dumps([{"id": r["id"], "description": r["description"], "expected": r["expected"], "actual": r["actual"]} for r in failures], ensure_ascii=False)}

只输出 JSON：{{"refund_amount_limit":3000,"refund_daily_max_count":2,"refund_daily_max_total":6000,"coupon_amount_limit":600,"coupon_delay_days":2,"coupon_expire_days":30,"rationale":"一句话"}}"""
    try:
        raw = call_model(prompt, model)
        data = parse_json(raw)
        candidate = normalize(data, current)
        if candidate is None:
            raise ValueError("invalid config")
        return candidate, data, raw
    except Exception as exc:
        print(f"edit failed: {type(exc).__name__}: {exc}")
        return None, {}, ""


def dev_gate(before: dict, after: dict) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if after["quality"] <= before["quality"] + 0.001:
        reasons.append(f"dev quality 未提升（{before['quality']} -> {after['quality']}）")
    for category, score in before["by_category"].items():
        if after["by_category"].get(category, 0) + 0.01 < score:
            reasons.append(f"dev {category} 下降")
    return not reasons, reasons


def test_gate(before: dict, after: dict) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if after["quality"] + 0.001 < before["quality"]:
        reasons.append(f"test quality 下降（{before['quality']} -> {after['quality']}）")
    for category, score in before["by_category"].items():
        if after["by_category"].get(category, 0) + 0.01 < score:
            reasons.append(f"test {category} 下降")
    return not reasons, reasons


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    HISTORY.mkdir(parents=True, exist_ok=True)
    cases = load_cases()
    dev = [c for c in cases if c["split"] == "dev"]
    test = [c for c in cases if c["split"] == "test"]

    current = CSConfig()
    if CONFIG.exists() and not args.reset:
        try:
            current = CSConfig(**json.loads(CONFIG.read_text(encoding="utf-8")))
        except Exception:
            current = CSConfig()
    if args.reset:
        CONFIG.write_text(json.dumps(asdict(current), ensure_ascii=False, indent=2), encoding="utf-8")

    dev_eval = evaluate(dev, current)
    test_eval = evaluate(test, current)
    print(f"cases={len(cases)} dev={len(dev)} test={len(test)}")
    print(f"baseline current={asdict(current)} dev={dev_eval['quality']} test={test_eval['quality']}")

    history = []
    rejected: list[dict] = []
    for round_no in range(1, args.rounds + 1):
        print(f"\n=== round {round_no} ===")
        diagnosis = diagnose(dev_eval["rows"], args.model)
        candidate, candidate_data, _ = edit(dev_eval["rows"], diagnosis, current, rejected, args.model)
        if candidate is None:
            item = {"round": round_no, "decision": "rejected", "reasons": ["模型未返回合法配置"], "diagnosis": diagnosis}
            history.append(item)
            rejected.append(item)
            continue
        dev_after = evaluate(dev, candidate)
        dev_ok, dev_reasons = dev_gate(dev_eval, dev_after)
        item = {
            "round": round_no,
            "diagnosis": diagnosis,
            "candidate": candidate_data,
            "candidate_config": asdict(candidate),
            "diff": config_diff(current, candidate),
            "dev_before": dev_eval["quality"],
            "dev_after": dev_after["quality"],
            "decision": "pending",
            "reasons": [],
        }
        if not dev_ok:
            item["decision"] = "rejected"
            item["reasons"] = dev_reasons
            history.append(item)
            rejected.append({"diff": item["diff"], "reasons": dev_reasons})
            print(f"candidate dev={dev_after['quality']} rejected: {dev_reasons}")
            continue
        test_after = evaluate(test, candidate)
        test_ok, test_reasons = test_gate(test_eval, test_after)
        item["test_after"] = test_after["quality"]
        item["test_gate"] = {"accepted": test_ok, "reasons": test_reasons}
        if not test_ok:
            item["decision"] = "rejected"
            item["reasons"] = test_reasons
            history.append(item)
            rejected.append({"diff": item["diff"], "reasons": test_reasons})
            print(f"candidate test={test_after['quality']} rejected: {test_reasons}")
            continue
        item["decision"] = "accepted"
        item["approval"] = "auto_demo"
        item["reasons"] = ["dev 提升且 test 门控通过"]
        history.append(item)
        current = candidate
        dev_eval = dev_after
        test_eval = test_after
        CONFIG.write_text(json.dumps(asdict(current), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"candidate accepted dev={dev_eval['quality']} test={test_eval['quality']}")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final = {
        "model": args.model,
        "config": asdict(current),
        "dev": dev_eval["quality"],
        "test": test_eval["quality"],
        "dev_by_category": dev_eval["by_category"],
        "test_by_category": test_eval["by_category"],
        "rounds": history,
        "scope": "deterministic customer-service policy calibration; not production RSI",
    }
    (RESULTS / f"cs_rsi_{stamp}.json").write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    (HISTORY / f"rounds-{stamp}.json").write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 客服策略 RSI 最小闭环",
        "",
        "> 诊断 Agent -> 编辑 Agent -> dev 回归 -> test 门控 -> 自动审批/回滚。",
        "> 审稿的是退款、优惠券、地址、催单、工单、备注、权限等确定性策略。",
        "",
        f"- 模型：`{args.model}`",
        f"- 用例：{len(cases)}（dev {len(dev)} / test {len(test)}）",
        f"- 最终配置：`{json.dumps(asdict(current), ensure_ascii=False)}`",
        f"- 最终 dev/test：{dev_eval['quality']}/{test_eval['quality']}",
        "",
        "## 每轮结果",
        "| 轮次 | 候选改动 | dev | test | 决策 | 原因 |",
        "|---|---|---:|---:|---|---|",
    ]
    for item in history:
        diff = json.dumps(item.get("diff", {}), ensure_ascii=False)
        if len(diff) > 90:
            diff = diff[:87] + "..."
        lines.append(f"| {item['round']} | `{diff}` | {item.get('dev_after', '-')} | {item.get('test_after', '-')} | {item['decision']} | {'; '.join(item.get('reasons', []))} |")
    lines += [
        "",
        "## 诊断摘要",
    ]
    for item in history:
        lines.append(f"- round {item['round']}: {item.get('diagnosis', {}).get('summary', '')}")
    lines += [
        "",
        "## 边界",
        "- 不是完整 RSI：只校准 6 个策略阈值，不改规则函数，没有自动上线；",
        "- test 只做门控，不参与编辑；",
        "- 自动审批只用于演示，真实流程需要人工审批和回滚。",
    ]
    (RESULTS / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"saved {RESULTS / 'report.md'}")


if __name__ == "__main__":
    main()
