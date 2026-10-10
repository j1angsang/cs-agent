"""进程内 Prometheus 指标，单机单进程，进程重启归零。"""
from prometheus_client import Counter, Gauge

STREAM_DEGRADE_TOTAL = Counter("stream_degrade_total", "流式首 token 前失败降级次数")
TOOL_FAILURE_TOTAL = Counter("tool_failure_total", "工具最终失败次数")
# 429 命中，scope 取 chat 或 auth
RATE_LIMIT_REJECTED_TOTAL = Counter(
    "rate_limit_rejected_total", "限流命中 429 次数", ["scope"]
)
LLM_TOKENS_TOTAL = Counter(
    "llm_tokens_total", "LLM token 消耗", ["kind"]
)
MEMORY_EXTRACTION_TOTAL = Counter("memory_extraction_total", "后台记忆抽取完成次数")
MEMORY_EXTRACTION_TIMEOUT_TOTAL = Counter(
    "memory_extraction_timeout_total", "后台记忆抽取超时次数"
)
ACTION_DECISION_TOTAL = Counter("action_decision_total", "写动作判定分布", ["action", "result"])
NEEDS_HUMAN_SESSIONS = Gauge("needs_human_sessions", "当前待人工会话数")
DB_POOL_INUSE = Gauge("db_pool_inuse", "SQLAlchemy 连接池 checkedout 连接数")
