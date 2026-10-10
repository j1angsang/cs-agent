from dataclasses import dataclass, field
from decimal import Decimal

# 档位枚举，全仓唯一出处。
LEVEL_AUTO = "auto"
LEVEL_HUMAN = "human"
LEVEL_DENY = "deny"


@dataclass(frozen=True)
class Decision:
    level: str
    reasons: list[str] = field(default_factory=list)
    rules_missed: list[str] = field(default_factory=list)

    @property
    def denied(self) -> bool:
        return self.level == LEVEL_DENY


@dataclass(frozen=True)
class RuleConfig:
    refund_amount_limit: Decimal
    refund_daily_max_count: int
    refund_daily_max_total: Decimal
    coupon_amount_limit: Decimal
    coupon_delay_days: int
    coupon_expire_days: int
