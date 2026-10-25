# -*- coding: utf-8 -*-
"""客服策略评测的外层 meta-loop。

内层：给定一种改进策略，校准阈值。
外层：比较“只调退款”“只调券”“全调”三种策略，用 meta-train 拟合、meta-val 选择，
test 只做最终审计。

边界：固定策略空间，不是开放式自我改写；不自动上线。
"""
from __future__ import annotations

import itertools
import json
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

import cs_rsi as cs

BASE = Path(__file__).resolve().parent
RESULTS = BASE / "results"
POLICY = BASE / "meta_policy.json"
DEFAULT = cs.CSConfig()


def make_folds(dev: list[dict], k: int = 3) -> list[tuple[list[dict], list[dict]]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for case in dev:
        groups[case["category"]].append(case)
    buckets: list[list[dict]] = [[] for _ in range(k)]
    for group in groups.values():
        for i, case in enumerate(group):
            buckets[i % k].append(case)
    folds = []
    for i in range(k):
        val = buckets[i]
        train = [case for j, bucket in enumerate(buckets) if j != i for case in bucket]
        folds.append((train, val))
    return folds


def changed_fields(cfg: cs.CSConfig) -> int:
    return sum(asdict(cfg)[k] != asdict(DEFAULT)[k] for k in asdict(DEFAULT))


def distance(cfg: cs.CSConfig) -> float:
    before, after = asdict(DEFAULT), asdict(cfg)
    return sum(abs(float(after[k]) - float(before[k])) for k in before)


def strategies():
    return {
        "refund_only": {
            "refund_amount_limit": [2198.0, 2500.0, 3000.0, 3500.0],
            "refund_daily_max_count": [1, 2, 3],
            "refund_daily_max_total": [4396.0, 5000.0, 6000.0, 8000.0],
        },
        "coupon_only": {
            "coupon_amount_limit": [439.6, 500.0, 600.0, 700.0],
            "coupon_delay_days": [2, 3, 4],
        },
        "full": {
            "refund_amount_limit": [2198.0, 2500.0, 3000.0, 3500.0],
            "refund_daily_max_count": [1, 2, 3],
            "refund_daily_max_total": [4396.0, 5000.0, 6000.0, 8000.0],
            "coupon_amount_limit": [439.6, 500.0, 600.0, 700.0],
            "coupon_delay_days": [2, 3, 4],
        },
    }


def fit(space: dict[str, list], meta_train: list[dict]) -> tuple[cs.CSConfig, dict]:
    baseline = asdict(DEFAULT)
    best: tuple[tuple, cs.CSConfig, dict] | None = None
    keys = list(space)
    for values in itertools.product(*(space[k] for k in keys)):
        data = dict(baseline)
        data.update(dict(zip(keys, values)))
        cfg = cs.CSConfig(**data)
        ev = cs.evaluate(meta_train, cfg)
        key = (ev["quality"], -changed_fields(cfg), -distance(cfg))
        if best is None or key > best[0]:
            best = (key, cfg, ev)
    assert best is not None
    return best[1], best[2]


def main() -> None:
    cases = cs.load_cases()
    dev = [c for c in cases if c["split"] == "dev"]
    test = [c for c in cases if c["split"] == "test"]
    folds = make_folds(dev, 3)
    base_test = cs.evaluate(test, cs.CSConfig())

    rows = []
    fitted = []
    for name, space in strategies().items():
        fold_scores = []
        for train, val in folds:
            cfg, _ = fit(space, train)
            fold_scores.append(cs.evaluate(val, cfg)["quality"])
        full_cfg, full_train = fit(space, dev)
        mean_val = round(sum(fold_scores) / len(fold_scores), 4)
        meta_score = mean_val
        rows.append({
            "strategy": name,
            "changed_fields": changed_fields(full_cfg),
            "config": asdict(full_cfg),
            "train_quality": full_train["quality"],
            "fold_scores": fold_scores,
            "meta_val_mean": mean_val,
            "meta_score": meta_score,
        })
        fitted.append((name, full_cfg, mean_val, meta_score))

    name, chosen, chosen_val, _ = max(
        fitted,
        key=lambda item: (
            item[3],
            changed_fields(item[1]),
            -distance(item[1]),
        ),
    )
    chosen_test = cs.evaluate(test, chosen)
    test_ok, reasons = cs.test_gate(base_test, chosen_test)
    committed = chosen if test_ok else cs.CSConfig()

    report = {
        "meta": {"cases": len(cases), "dev": len(dev), "test": len(test), "folds": len(folds)},
        "baseline": {"test": base_test["quality"]},
        "strategies": rows,
        "selected": {
            "strategy": name,
            "config": asdict(chosen),
            "meta_val_mean": chosen_val,
            "test_quality": chosen_test["quality"],
            "test_gate": {"accepted": test_ok, "reasons": reasons},
        },
        "committed_config": asdict(committed),
    }
    POLICY.write_text(json.dumps(asdict(committed), ensure_ascii=False, indent=2), encoding="utf-8")
    (RESULTS / "meta_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 客服策略外层 meta-loop",
        "",
        "> 内层校准阈值，外层比较“只调退款 / 只调券 / 全调”三种改进策略；3 折 meta-validation 选择，test 只审计。",
        "",
        f"- 数据：{len(cases)} 题（dev {len(dev)} / test {len(test)}；3 折 meta CV）",
        "",
        "| 策略 | 改动字段 | 各折 meta-val | 均值 | meta-score | 选中 |",
        "|---|---:|---|---:|---:|---|",
    ]
    for row in rows:
        folds_text = " / ".join(str(x) for x in row["fold_scores"])
        lines.append(
            f"| {row['strategy']} | {row['changed_fields']} | {folds_text} | "
            f"{row['meta_val_mean']} | {row['meta_score']} | "
            f"{'是' if row['strategy'] == name else ''} |"
        )
    lines += [
        "",
        "## 选择结果",
        f"- 选中策略：`{name}`",
        f"- 配置：`{json.dumps(asdict(chosen), ensure_ascii=False)}`",
        f"- meta-val 均值：{chosen_val}",
        f"- test：{chosen_test['quality']}（gate={'通过' if test_ok else '拒绝，回退 baseline'}）",
        f"- 最终配置：`{json.dumps(asdict(committed), ensure_ascii=False)}`",
        "",
        "## 边界",
        "- 策略空间是固定的 3 种，不是开放式自我改写；",
        "- 3 折 meta-val 选策略，test 只做最终审计；",
        "- 不自动上线。",
    ]
    (RESULTS / "meta_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"selected={name} meta_val={chosen_val} test={chosen_test['quality']} gate={test_ok}")
    print(f"saved {RESULTS / 'meta_report.md'}")


if __name__ == "__main__":
    main()
