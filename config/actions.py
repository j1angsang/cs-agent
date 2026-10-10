from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv

from policies.decision import LEVEL_AUTO, LEVEL_HUMAN, RuleConfig

load_dotenv()

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_AUTO = "auto"
VALID_MODES = (MODE_OFF, MODE_SHADOW, MODE_AUTO)

WRITE_ACTIONS_MODE = os.getenv("WRITE_ACTIONS_MODE", MODE_SHADOW).strip().lower()

# 阈值取自 customer_service_db：单笔退款上限=客单价中位数，券面额=其 20%；.env 可覆盖
_REFUND_AMOUNT_LIMIT_RAW = os.getenv("REFUND_AUTO_AMOUNT_LIMIT", "2198.00")
_REFUND_DAILY_MAX_COUNT_RAW = os.getenv("REFUND_DAILY_MAX_COUNT", "1")
_REFUND_DAILY_MAX_TOTAL_RAW = os.getenv("REFUND_DAILY_MAX_TOTAL", "4396.00")
_COUPON_AMOUNT_LIMIT_RAW = os.getenv("COUPON_AMOUNT_LIMIT", "439.60")
_COUPON_DELAY_DAYS_RAW = os.getenv("COUPON_DELAY_DAYS", "3")
_COUPON_EXPIRE_DAYS_RAW = os.getenv("COUPON_EXPIRE_DAYS", "30")


def _decimal(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, AttributeError, ValueError):
        return None
    return value if value >= 0 else None


def _int(raw: str) -> int | None:
    try:
        value = int(raw.strip())
    except (AttributeError, ValueError):
        return None
    return value if value >= 0 else None


def config_errors() -> list[str]:
    errors: list[str] = []
    for env_name, value in (
        ("WRITE_ACTIONS_MODE", WRITE_ACTIONS_MODE),
        *[(spec.mode_env, os.getenv(spec.mode_env, "").strip().lower())
          for spec in _iter_specs()],
    ):
        if value and value not in VALID_MODES:
            errors.append(f"- {env_name}: 非法值 {value!r}，允许 {'|'.join(VALID_MODES)}")
    for env_name, raw, parser in (
        ("REFUND_AUTO_AMOUNT_LIMIT", _REFUND_AMOUNT_LIMIT_RAW, _decimal),
        ("REFUND_DAILY_MAX_COUNT", _REFUND_DAILY_MAX_COUNT_RAW, _int),
        ("REFUND_DAILY_MAX_TOTAL", _REFUND_DAILY_MAX_TOTAL_RAW, _decimal),
        ("COUPON_AMOUNT_LIMIT", _COUPON_AMOUNT_LIMIT_RAW, _decimal),
        ("COUPON_DELAY_DAYS", _COUPON_DELAY_DAYS_RAW, _int),
        ("COUPON_EXPIRE_DAYS", _COUPON_EXPIRE_DAYS_RAW, _int),
    ):
        if parser(raw) is None:
            errors.append(f"- {env_name}: 必须是非负数字，当前 {raw!r}")
    return errors


def _iter_specs():
    # 延迟 import：避免 policies 与 config 循环依赖
    from policies.registry import ACTION_REGISTRY

    return ACTION_REGISTRY.values()


def action_mode(action_name: str) -> str:
    from policies.registry import ACTION_REGISTRY

    spec = ACTION_REGISTRY.get(action_name)
    override = os.getenv(spec.mode_env, "").strip().lower() if spec else ""
    return override if override in VALID_MODES else (WRITE_ACTIONS_MODE if WRITE_ACTIONS_MODE in VALID_MODES else MODE_SHADOW)


def effective_level(decision_level: str, mode: str) -> str:
    if mode == MODE_AUTO:
        return decision_level
    if mode == MODE_SHADOW and decision_level == LEVEL_AUTO:
        return LEVEL_HUMAN
    return decision_level


def load_rule_config() -> RuleConfig:
    raw_amount = os.getenv("REFUND_AUTO_AMOUNT_LIMIT", "2198.00")
    raw_daily_count = os.getenv("REFUND_DAILY_MAX_COUNT", "1")
    raw_daily_total = os.getenv("REFUND_DAILY_MAX_TOTAL", "4396.00")
    raw_coupon_amount = os.getenv("COUPON_AMOUNT_LIMIT", "439.60")
    raw_delay_days = os.getenv("COUPON_DELAY_DAYS", "3")
    raw_expire_days = os.getenv("COUPON_EXPIRE_DAYS", "30")
    return RuleConfig(
        refund_amount_limit=_decimal(raw_amount) or Decimal("0"),
        refund_daily_max_count=_int(raw_daily_count) or 0,
        refund_daily_max_total=_decimal(raw_daily_total) or Decimal("0"),
        coupon_amount_limit=_decimal(raw_coupon_amount) or Decimal("0"),
        coupon_delay_days=_int(raw_delay_days) or 0,
        coupon_expire_days=_int(raw_expire_days) or 0,
    )
