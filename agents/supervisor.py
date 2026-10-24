import logging
import os
import time
from typing import Annotated, NotRequired, TypedDict

from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from agents.graph import (
    CustomerServiceState,
    _FALLBACK_ACTIVE,
    _collect_tool_trace,
    _handler_usage_totals,
    _history_to_messages,
    _normalize_agent_model,
    _preview,
    _run_config,
    _thread_base_messages,
    _tool_ok,
    get_llm,
    long_term_memory_hook,
)
from rag.rag import retrieve
from services.memory import get_checkpointer, thread_config
from tools.order_tools import check_refund_status, query_order, query_product

logger = logging.getLogger(__name__)


ORDER_PROMPT = """你是订单/退款专员，只处理订单状态、金额、物流、退款进度等问题。
你必须调用工具拿真实数据，不能凭记忆编造。回答简洁友好。"""

PRODUCT_PROMPT = """你是商品专员，只处理商品搜索、价格、库存、参数等问题。
你必须调用 query_product 工具拿真实数据，不能编造商品信息。回答简洁友好。"""

KNOWLEDGE_PROMPT = """你是售后政策专员，只回答退换货、发票、配送、会员等政策问题。
需要知识库时调用 search_knowledge 工具；知识库没有的内容要明确说无法确认，不得编造。"""

SUPERVISOR_PROMPT = """你是客服主管，只做路由，不回答业务问题。
根据用户问题选择一个 worker，并只输出一个英文单词：
- order：查询某个具体订单/退款单的状态、金额、物流、审核进度（通常带单号或我的订单）
- product：查询某个具体商品的搜索、价格、库存、参数
- knowledge：问通用政策/流程/规则（退货政策、发票、配送时效、会员、是否支持某服务）

注意：问题里出现退款/订单不一定走 order；如果是在问一般多久到账/政策是什么/是否支持这类规则，走 knowledge。
用户问题：{question}
只输出 order / product / knowledge 之一。"""


@tool(parse_docstring=True)
def search_knowledge(query: str) -> str:
    """检索售后政策知识库，用于回答退换货、发票、配送、会员等政策问题。

    Args:
        query: 用户问题的核心检索词。
    """
    return retrieve(query)


class SupervisorState(TypedDict):
    messages: Annotated[list, add_messages]
    user: NotRequired[dict | None]
    # 长期记忆随 state 进子 Agent，否则 USE_SUPERVISOR=true 时 Store 只写不读、记忆静默失效
    long_term_memory: NotRequired[str]


def _build_workers():
    """每个请求现建 worker；以对象 id 做缓存键会因 id 复用错误命中，故不缓存。"""
    model = _normalize_agent_model(get_llm())
    workers = {
        "order": create_agent(
            model=model,
            tools=[query_order, check_refund_status],
            system_prompt=ORDER_PROMPT,
            middleware=[long_term_memory_hook],
            state_schema=CustomerServiceState,
            name="order_agent",
        ),
        "product": create_agent(
            model=model,
            tools=[query_product],
            system_prompt=PRODUCT_PROMPT,
            middleware=[long_term_memory_hook],
            state_schema=CustomerServiceState,
            name="product_agent",
        ),
        "knowledge": create_agent(
            model=model,
            tools=[search_knowledge],
            system_prompt=KNOWLEDGE_PROMPT,
            middleware=[long_term_memory_hook],
            state_schema=CustomerServiceState,
            name="knowledge_agent",
        ),
    }
    logger.info("supervisor.workers 已构建 order/product/knowledge")
    return workers


def _rule_route(question: str) -> str | None:
    text = question or ""
    order_words = ("订单", "退款", "物流", "发货", "快递", "金额")
    product_words = ("商品", "库存", "价格", "多少钱", "参数", "推荐", "对比", "耳机", "键盘", "显示器", "椅子", "充电器", "扩展坞")
    if any(w in text for w in order_words):
        return "order"
    if any(w in text for w in product_words):
        return "product"
    if any(w in text for w in ("退货", "换货", "发票", "配送", "会员", "政策", "售后", "保修")):
        return "knowledge"
    return None


async def route_question(question: str) -> dict:
    """只做 Supervisor 路由决策，不执行工具。"""
    raw = ""
    source = "llm"
    try:
        resp = await get_llm().ainvoke([
            SystemMessage(content=SUPERVISOR_PROMPT.format(question=question)),
            HumanMessage(content=question),
        ])
        raw = str(getattr(resp, "content", resp) or "")
        normalized = raw.strip().lower()
        for route in ("order", "product", "knowledge"):
            if route in normalized:
                return {"route": route, "source": source, "raw": raw}
        logger.warning("supervisor.route LLM 返回非法内容，使用规则兜底 raw=%s", _preview(raw))
        source = "rule_fallback"
    except Exception:
        logger.exception("supervisor.route LLM 路由失败，使用规则兜底")
        source = "rule_fallback"
    route = _rule_route(question) or "knowledge"
    return {"route": route, "source": source, "raw": raw}


async def supervisor_node(state: SupervisorState) -> dict:
    question = ""
    for msg in reversed(state.get("messages", [])):
        if isinstance(msg, HumanMessage):
            question = str(msg.content)
            break
    decision = await route_question(question)
    logger.info(
        "supervisor.route question=%s route=%s source=%s raw=%s",
        _preview(question), decision["route"], decision["source"], _preview(decision["raw"]),
    )
    return {"route": decision["route"]}


def _build_graph():
    workers = _build_workers()
    graph = StateGraph(SupervisorState)
    graph.add_node("supervisor", supervisor_node)
    for name, worker in workers.items():
        graph.add_node(name, worker)
    graph.set_entry_point("supervisor")
    graph.add_conditional_edges(
        "supervisor",
        lambda state: state.get("route", "knowledge"),
        {"order": "order", "product": "product", "knowledge": "knowledge"},
    )
    for name in workers:
        graph.add_edge(name, END)
    # checkpointer 由图统一持有子图共用；USE_CHECKPOINTER=false 时为 None，走旧 history 路径
    return graph.compile(checkpointer=get_checkpointer())


async def run_supervisor(user_message: str, history: list[dict] | None = None, user: dict | None = None,
                         thread_id: str | None = None, long_term_memory: str | None = None,
                         return_meta: bool = False) -> str | dict:
    """跑一遍 Supervisor 图，返回最终回答。long_term_memory 由 hook 注入系统提示，不进 messages。

    return_meta=True 时返回 {answer, route, usage, tool_trace}，供评测使用；
    默认仍返回 str，线上调用方（routers/chat.py）行为不变。
    """
    base_msgs, config = await _thread_base_messages(history, thread_id)
    msgs = base_msgs + [HumanMessage(content=user_message)]
    _FALLBACK_ACTIVE.set(False)
    started = time.perf_counter()
    graph = _build_graph()
    input_state = {"messages": msgs, "user": user}
    if long_term_memory:
        input_state["long_term_memory"] = long_term_memory
    # 显式设置 recursion_limit 作图级安全网，取值同单 Agent 的 _run_config
    usage_handler = UsageMetadataCallbackHandler()
    result = await graph.ainvoke(input_state, config=_run_config(config, usage_handler))
    answer = "抱歉，我没有理解您的问题。"
    for msg in reversed(result["messages"]):
        if isinstance(msg, AIMessage) and not getattr(msg, "tool_calls", None):
            answer = msg.content
            break
    logger.info(
        "supervisor.finish elapsed_ms=%.1f route=%s answer_preview=%s",
        (time.perf_counter() - started) * 1000,
        result.get("route", "-"),
        _preview(answer),
    )
    if not return_meta:
        return answer
    return {
        "answer": answer,
        "route": result.get("route"),
        "usage": _handler_usage_totals(usage_handler) or {},
        "tool_trace": _collect_tool_trace(result["messages"]),
    }


async def astream_supervisor(user_message: str, history: list[dict] | None = None, user: dict | None = None,
                             thread_id: str | None = None, long_term_memory: str | None = None):
    """Supervisor 模式真流式：只转发 worker 子图的 token 事件，图每请求现建。"""
    base_msgs, config = await _thread_base_messages(history, thread_id)
    msgs = base_msgs + [HumanMessage(content=user_message)]
    _FALLBACK_ACTIVE.set(False)
    graph = _build_graph()
    input_state = {"messages": msgs, "user": user}
    if long_term_memory:
        input_state["long_term_memory"] = long_term_memory
    async for event in graph.astream_events(input_state, config=_run_config(config), version="v2"):
        if event.get("event") != "on_chat_model_stream":
            continue
        chunk = event.get("data", {}).get("chunk")
        content = getattr(chunk, "content", "") if chunk is not None else ""
        if content:
            yield content
