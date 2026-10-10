"""工具注册入口：只读工具 + 写工具按开关汇总成模型可见的工具集。"""
from config.actions import MODE_OFF, action_mode
from policies.registry import ACTION_REGISTRY
from tools import action_tools
from tools.order_tools import TOOLS as _READ_TOOLS

# 动作名 -> 工具名；不在注册表里的工具恒注册，如 escalate_to_human
_TOOL_TO_ACTION: dict[str, str] = {spec.tool_name: spec.name for spec in ACTION_REGISTRY.values()}


def enabled_tools() -> list:
    """按当前开关返回模型可用工具列表；每个请求现建 agent 时调用，开关改动即生效。"""
    tools = list(_READ_TOOLS)
    for t in action_tools.ACTION_TOOLS:
        action = _TOOL_TO_ACTION.get(t.name)
        if action is None or action_mode(action) != MODE_OFF:
            tools.append(t)
    return tools
