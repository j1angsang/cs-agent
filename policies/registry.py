"""动作注册表：写动作的唯一事实源，开关过滤、提示词拼接、审批分发都从这里读。"""
from dataclasses import dataclass
from typing import Callable

from policies.decision import LEVEL_AUTO, LEVEL_DENY, LEVEL_HUMAN
from policies.action_rules import (
    decide_address_change,
    decide_agent_note,
    decide_coupon,
    decide_ticket,
    decide_urge,
)
from policies.refund_policy import decide_refund


@dataclass(frozen=True)
class ActionSpec:
    name: str
    tool_name: str
    rule_fn: Callable
    idempotency_key: str
    audit_required: tuple[str, ...]
    allowed_levels: tuple[str, ...]
    mode_env: str


ACTION_REGISTRY: dict[str, ActionSpec] = {
    spec.name: spec
    for spec in (
        ActionSpec(
            name="refund", tool_name="request_refund", rule_fn=decide_refund,
            idempotency_key="order_id，refunds 部分唯一索引",
            audit_required=("actor_type", "actor_id", "rules_hit", "thresholds"),
            allowed_levels=(LEVEL_AUTO, LEVEL_HUMAN, LEVEL_DENY),
            mode_env="ACTION_REFUND_MODE",
        ),
        ActionSpec(
            name="urge", tool_name="urge_order", rule_fn=decide_urge,
            idempotency_key="order_id，orders.urgent 已置位时是幂等 no-op",
            audit_required=("actor_type", "actor_id", "target_type", "target_id", "session_id", "rules_hit"),
            allowed_levels=(LEVEL_AUTO, LEVEL_DENY),
            mode_env="ACTION_URGE_MODE",
        ),
        ActionSpec(
            name="agent_note", tool_name="add_session_note", rule_fn=decide_agent_note,
            idempotency_key="session_id，一会话一条覆盖式",
            audit_required=("actor_type", "actor_id", "target_type", "target_id", "session_id"),
            allowed_levels=(LEVEL_AUTO, LEVEL_DENY),
            mode_env="ACTION_AGENT_NOTE_MODE",
        ),
        ActionSpec(
            name="ticket", tool_name="create_ticket", rule_fn=decide_ticket,
            idempotency_key="(session_id, type, status='open') 部分唯一",
            audit_required=("actor_type", "actor_id", "target_type", "target_id", "session_id", "rules_hit"),
            allowed_levels=(LEVEL_AUTO, LEVEL_DENY),
            mode_env="ACTION_TICKET_MODE",
        ),
        ActionSpec(
            name="delay_coupon", tool_name="issue_delay_coupon", rule_fn=decide_coupon,
            idempotency_key="order_id，user_coupons.order_id 唯一",
            audit_required=("actor_type", "actor_id", "target_type", "target_id", "session_id",
                            "rules_hit", "payload"),
            allowed_levels=(LEVEL_AUTO, LEVEL_HUMAN, LEVEL_DENY),
            mode_env="ACTION_DELAY_COUPON_MODE",
        ),
        ActionSpec(
            name="change_address", tool_name="change_address", rule_fn=decide_address_change,
            idempotency_key="变更历史，每次变更一行审计",
            audit_required=("actor_type", "actor_id", "target_type", "target_id", "session_id",
                            "rules_hit", "payload"),
            allowed_levels=(LEVEL_AUTO, LEVEL_DENY),
            mode_env="ACTION_CHANGE_ADDRESS_MODE",
        ),
        ActionSpec(
            name="cancel_order", tool_name="cancel_order", rule_fn=decide_refund,
            idempotency_key="order_id，复用退款部分唯一索引",
            audit_required=("actor_type", "actor_id", "rules_hit", "thresholds"),
            allowed_levels=(LEVEL_AUTO, LEVEL_HUMAN, LEVEL_DENY),
            mode_env="ACTION_CANCEL_ORDER_MODE",
        ),
    )
}


def get_action_spec(name: str) -> ActionSpec:
    return ACTION_REGISTRY[name]
