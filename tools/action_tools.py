import hashlib
import os
from typing import Annotated

from dotenv import load_dotenv
from langchain.tools import InjectedState, tool

from tools import _backend_client as _client
from tools.order_tools import BACKEND_BASE  # 测试接缝同源：故障注入改写一处即全局生效

load_dotenv()


@tool(parse_docstring=True)
def request_refund(order_no: str, reason: str, state: Annotated[dict, InjectedState]) -> str:
    """为用户的未发货订单提交退款申请（金额由系统按规则判定，不能指定金额）。

    何时调用：用户明确要求退款/不想要了，且订单未发货（pending）。
    何时不调用：只是查询退款进度（用 check_refund_status）；订单已发货（系统会拒绝，
    已发货退款请联系人工）；订单不是用户的（系统会拒绝）。
    返回"已提交人工审核"时必须立即调用 escalate_to_human 上报转人工。
    拿不准用户意图时先向用户确认，不要直接调用。

    Args:
        order_no: 订单号，例如 20260816001。
        reason: 用户给出的退款原因（原话概括，不要编造）。
    """
    caller = state.get("user") or {}
    data = _client.post(
        "/refunds", caller,
        json_body={"order_no": order_no, "reason": reason, "session_id": caller.get("session_id")},
        idempotency_key=f"refund:{order_no}", base_url=BACKEND_BASE,
        tool_name="request_refund", tool_args={"order_no": order_no},
    )
    if "error" in data:
        return f"退款未能办理：{data['error']}"
    if data.get("decision") == "auto":
        return f"退款已完成：退款单 {data['id']}，金额 {data['amount']} 元，已原路退回。"
    return (f"退款申请已提交（退款单 {data['id']}，金额 {data['amount']} 元），"
            f"需要人工审核，请立即调用 escalate_to_human 上报转人工。")


@tool(parse_docstring=True)
def escalate_to_human(reason: str, priority: str, state: Annotated[dict, InjectedState]) -> str:
    """把当前会话转接人工客服（复用既有转人工链路，转接后本轮会话交给人工处理）。

    何时调用：工具结果明确要求人工审核/人工确认（如退款单待审核）；用户强烈要求真人；
    连续处理失败或你无法安全处理当前请求。
    何时不调用：普通咨询、工具一次失败（会自动重试，别急着升级）。

    Args:
        reason: 转人工原因（带上相关单号，例如：退款单 123 待人工审核）。
        priority: 优先级，normal（普通）或 high（投诉/纠纷等紧急场景）。
    """
    caller = state.get("user") or {}
    session_id = caller.get("session_id")
    if not session_id:
        return "当前会话缺少会话标识，无法转人工，请稍后再试。"
    data = _client.post(
        f"/chat/sessions/{session_id}/needs-human", caller,
        json_body={"reason": reason, "priority": priority},
        idempotency_key=f"needs-human:{session_id}", base_url=BACKEND_BASE,
        tool_name="escalate_to_human", tool_args={"reason": reason, "priority": priority},
    )
    if "error" in data:
        return f"转人工失败：{data['error']}"
    return "已转人工，客服将尽快接入。"


@tool(parse_docstring=True)
def urge_order(order_no: str, state: Annotated[dict, InjectedState]) -> str:
    """为用户的已发货订单提交催单/加急请求（物流优先处理）。

    何时调用：用户催促物流且订单已发货。何时不调用：订单未发货或已签收（系统会拒绝）；
    用户只是查询物流状态。拿不准时先向用户确认。
    返回"已提交人工确认"时必须立即调用 escalate_to_human 上报转人工。

    Args:
        order_no: 订单号，例如 20260816001。
    """
    caller = state.get("user") or {}
    data = _client.post(
        f"/orders/{order_no}/urge", caller,
        json_body={"session_id": caller.get("session_id")},
        idempotency_key=f"urge:{order_no}", base_url=BACKEND_BASE,
        tool_name="urge_order", tool_args={"order_no": order_no},
    )
    if "error" in data:
        return f"加急未能办理：{data['error']}"
    if data.get("level") == "human":
        return "加急申请已提交，需要人工确认，请立即调用 escalate_to_human 上报转人工。"
    if data.get("already_urgent"):
        return f"订单 {order_no} 已在加急处理中，无需重复操作。"
    return f"已为订单 {order_no} 标记加急，物流将优先处理。"


@tool(parse_docstring=True)
def add_session_note(note: str, state: Annotated[dict, InjectedState]) -> str:
    """在当前会话写一条结论式备注（覆盖式），供人工接手时快速了解进展。

    何时调用：本轮对话得出阶段性结论（如"客户关注退款进度，已提交申请"）值得留给人工时。
    何时不调用：普通问答没有结论；备注内容要客观简洁，不要写敏感个人信息。
    返回"已提交人工确认"时必须立即调用 escalate_to_human 上报转人工。

    Args:
        note: 备注内容（结论式摘要，不超过 50 字）。
    """
    caller = state.get("user") or {}
    session_id = caller.get("session_id")
    if not session_id:
        return "当前会话缺少会话标识，无法写备注。"
    data = _client.post(
        f"/chat/sessions/{session_id}/agent-note", caller,
        json_body={"note": note},
        idempotency_key=f"agent-note:{session_id}", base_url=BACKEND_BASE,
        tool_name="add_session_note", tool_args={"note": note},
    )
    if "error" in data:
        return f"备注未能写入：{data['error']}"
    if data.get("level") == "human":
        return "会话备注已提交人工确认，请立即调用 escalate_to_human 上报转人工。"
    return "会话备注已更新。"


@tool(parse_docstring=True)
def create_ticket(type: str, subject: str, state: Annotated[dict, InjectedState]) -> str:
    """为当前会话创建一张售后工单（同会话同类型未关闭工单已存在时返回既有工单号）。

    何时调用：需要人工/售后团队跟进处理（如退款审核、投诉登记）。
    何时不调用：普通咨询能直接回答；同一问题已建过工单（系统会返回既有工单号）。
    返回"已提交人工确认"时必须立即调用 escalate_to_human 上报转人工。

    Args:
        type: 工单类型，例如：退款咨询、物流问题、投诉。
        subject: 一句话主题（不超过 50 字）。
    """
    caller = state.get("user") or {}
    session_id = caller.get("session_id")
    if not session_id:
        return "当前会话缺少会话标识，无法创建工单。"
    # Idempotency-Key 只能 ASCII，中文类型须用摘要，否则 httpx 编码 header 时报 UnicodeEncodeError
    type_digest = hashlib.md5(type.encode("utf-8")).hexdigest()[:8]
    data = _client.post(
        "/tickets", caller,
        json_body={"session_id": session_id, "type": type, "subject": subject},
        idempotency_key=f"ticket:{session_id}:{type_digest}", base_url=BACKEND_BASE,
        tool_name="create_ticket", tool_args={"type": type, "subject": subject},
    )
    if "error" in data:
        return f"工单未能创建：{data['error']}"
    if data.get("level") == "human":
        return "工单申请已提交，需要人工确认，请立即调用 escalate_to_human 上报转人工。"
    if data.get("existing"):
        return f"已有同类型未关闭工单：{data['ticket_no']}，无需重复创建。"
    return f"工单已创建：{data['ticket_no']}（类型：{type}）。"


@tool(parse_docstring=True)
def issue_delay_coupon(order_no: str, state: Annotated[dict, InjectedState]) -> str:
    """为延迟发货的已发货订单申请补偿券（面额由系统规则决定，不能指定金额）。

    何时调用：订单已发货且用户反馈超过承诺时效未收到商品，要求补偿。
    何时不调用：订单未发货/已签收/该单已发过券（系统会拒绝或转人工）；用户只是查询物流；
    订单不是用户的。返回"已提交人工审核"时必须立即调用 escalate_to_human 上报转人工。

    Args:
        order_no: 订单号，例如 20260816001。
    """
    caller = state.get("user") or {}
    data = _client.post(
        f"/orders/{order_no}/delay-coupon", caller,
        json_body={"session_id": caller.get("session_id")},
        idempotency_key=f"coupon:{order_no}", base_url=BACKEND_BASE,
        tool_name="issue_delay_coupon", tool_args={"order_no": order_no},
    )
    if "error" in data:
        return f"补偿券未能发放：{data['error']}"
    if data.get("level") == "human":
        return "补偿券申请已提交，需要人工审核，请立即调用 escalate_to_human 上报转人工。"
    return f"已为订单 {order_no} 发放 {data.get('amount')} 元补偿券，有效期以券详情为准。"


@tool(parse_docstring=True)
def change_address(order_no: str, new_address: str, state: Annotated[dict, InjectedState]) -> str:
    """修改未发货订单的收货地址（执行前必须先向用户复述新地址并获用户确认）。

    何时调用：订单未发货，用户提供了完整新地址并已明确确认。
    何时不调用：订单已发货（系统会拒绝并提示转人工）；用户还没确认新地址；
    订单不是用户的（系统会拒绝）。
    返回"已提交人工审核"时必须立即调用 escalate_to_human 上报转人工。

    Args:
        order_no: 订单号，例如 20260816001。
        new_address: 用户确认后的完整新收货地址（不要编造/补全）。
    """
    caller = state.get("user") or {}
    # 幂等键含新地址摘要：同一地址重试不重复写，换地址是新意图。
    addr_digest = hashlib.md5(new_address.encode("utf-8")).hexdigest()[:8]
    data = _client.post(
        f"/orders/{order_no}/address", caller,
        json_body={"new_address": new_address, "session_id": caller.get("session_id")},
        idempotency_key=f"address:{order_no}:{addr_digest}", base_url=BACKEND_BASE,
        tool_name="change_address",
        # 地址等个人信息不许进日志明文，日志只带订单号
        tool_args={"order_no": order_no},
    )
    if "error" in data:
        return f"地址修改未能办理：{data['error']}"
    if data.get("level") == "human":
        return "地址修改已提交，需要人工审核，请立即调用 escalate_to_human 上报转人工。"
    return f"订单 {order_no} 的收货地址已更新。"


@tool(parse_docstring=True)
def cancel_order(order_no: str, reason: str, state: Annotated[dict, InjectedState]) -> str:
    """取消未发货订单并按系统退款规则处理（自动退款或转人工审核）。

    何时调用：用户明确要求取消未发货订单，且已确认退款按订单实付金额原路退回。
    何时不调用：订单已发货（系统会拒绝，需人工协助）；订单不是用户的；用户只是咨询订单状态。
    返回"已提交人工审核"时必须立即调用 escalate_to_human 上报转人工。

    Args:
        order_no: 订单号，例如 20260816001。
        reason: 用户给出的取消原因（原话概括，不要编造）。
    """
    caller = state.get("user") or {}
    data = _client.post(
        f"/orders/{order_no}/cancel", caller,
        json_body={"reason": reason, "session_id": caller.get("session_id")},
        idempotency_key=f"cancel:{order_no}", base_url=BACKEND_BASE,
        tool_name="cancel_order", tool_args={"order_no": order_no},
    )
    if "error" in data:
        return f"取消订单未能办理：{data['error']}"
    if data.get("level") == "human":
        return (f"取消申请已提交（退款单 {data.get('refund_id')}，金额 {data.get('amount')} 元），"
                f"需要人工审核，请立即调用 escalate_to_human 上报转人工。")
    return (f"订单 {order_no} 已取消并退款，退款单 {data.get('refund_id')}，"
            f"金额 {data.get('amount')} 元，已原路退回。")


ACTION_TOOLS = [
    request_refund, cancel_order, urge_order, add_session_note, create_ticket,
    issue_delay_coupon, change_address, escalate_to_human,
]
