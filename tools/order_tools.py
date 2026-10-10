import os
import urllib.parse
from typing import Annotated

from dotenv import load_dotenv
from langchain.tools import InjectedState, tool

from tools import _backend_client as _client
from tools._backend_client import TOOL_FAILURE_COUNTS  # noqa: F401  re-export：routers/chat.py 从本模块导入

load_dotenv()


# 测试接缝：故障注入运行时改写 BACKEND_BASE，_get 每次请求读本模块全局，勿在 import 期固化
BACKEND_BASE = os.getenv("BACKEND_BASE", "http://127.0.0.1:8000")


def _get(path: str, caller: dict | None = None, *,
         tool_name: str | None = None,
         tool_args: dict | None = None) -> dict:
    return _client.get(
        path, caller, base_url=BACKEND_BASE,
        tool_name=tool_name, tool_args=tool_args,
    )


@tool(parse_docstring=True)
def query_order(order_no: str, state: Annotated[dict, InjectedState]) -> str:
    """查询订单状态，返回订单详情（状态/金额/数量/下单时间）。

    Args:
        order_no: 订单号，例如 20260816001。
    """
    caller = state.get("user")
    data = _get(
        f"/orders/{order_no}", caller,
        tool_name="query_order", tool_args={"order_no": order_no},
    )
    if "error" in data:
        return f"查询失败：{data['error']}"
    return (f"订单 {data['order_no']}：状态={data['status']}，"
            f"金额={data['total_price']}元，数量={data['quantity']}，"
            f"下单时间={data['created_at']}")


@tool(parse_docstring=True)
def query_product(keyword: str, state: Annotated[dict, InjectedState]) -> str:
    """搜索商品，返回匹配的商品列表。

    Args:
        keyword: 商品关键词，例如 耳机、键盘。
    """
    caller = state.get("user")
    data = _get(
        f"/products?keyword={urllib.parse.quote(keyword)}", caller,
        tool_name="query_product", tool_args={"keyword": keyword},
    )
    if "error" in data:
        return f"查询失败：{data['error']}"
    if not data:
        return "没有找到匹配的商品"
    return "\n".join(f"- {p['name']}：{p['price']}元，库存{p['stock']}" for p in data[:5])


@tool(parse_docstring=True)
def check_refund_status(refund_id: int, state: Annotated[dict, InjectedState]) -> str:
    """查询退款进度，返回审核状态。

    Args:
        refund_id: 退款单号（数字）。
    """
    caller = state.get("user")
    data = _get(
        f"/refunds/{refund_id}", caller,
        tool_name="check_refund_status", tool_args={"refund_id": refund_id},
    )
    if "error" in data:
        return f"查询失败：{data['error']}"
    return f"退款单 {refund_id}：状态={data['status']}，金额={data['amount']}元"


@tool
def query_coupons(state: Annotated[dict, InjectedState]) -> str:
    """查询我的优惠券/补偿券列表（金额、状态、来源、有效期）。

    何时调用：用户询问"我的券/补偿券/优惠券"时。何时不调用：与券无关的咨询。
    """
    caller = state.get("user")
    data = _get(
        "/coupons/my", caller,
        tool_name="query_coupons", tool_args={},
    )
    if "error" in data:
        return f"查询失败：{data['error']}"
    if not data:
        return "您目前没有可用的优惠券。"
    lines = []
    for cp in data:
        expire = str(cp.get("expires_at", ""))[:10]
        lines.append(f"- {cp['amount']}元券（{cp['source']}，状态={cp['status']}，有效期至 {expire}）")
    return "您的优惠券：\n" + "\n".join(lines)


@tool(parse_docstring=True)
def compare_orders(order_nos: list[str], state: Annotated[dict, InjectedState]) -> str:
    """对比多个订单的状态/金额/时间，汇总成对照结果（一次调用查多单）。

    何时调用：用户给出两个及以上订单号想比较、或想同时看几个订单的进度。
    何时不调用：只查一个订单（用 query_order 更直接）。最多一次对比 5 单。

    Args:
        order_nos: 要对比的订单号列表。
    """
    caller = state.get("user")
    if not order_nos:
        return "请提供要对比的订单号。"
    rows = []
    for no in order_nos[:5]:  # 上限 5 单，防止一次拖太多
        data = _get(
            f"/orders/{no}", caller,
            tool_name="compare_orders", tool_args={"order_no": no},
        )
        if "error" in data:
            rows.append(f"- {no}：查询失败（{data['error']}）")
        else:
            rows.append(f"- {data['order_no']}：状态={data['status']}，金额={data['total_price']}元，下单={str(data['created_at'])[:16]}")
    return "订单对比：\n" + "\n".join(rows)


TOOLS = [query_order, query_product, check_refund_status, query_coupons, compare_orders]
