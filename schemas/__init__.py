from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str | None
    price: float
    stock: int


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_no: str
    user_id: int
    product_id: int
    quantity: int
    total_price: float
    status: str
    created_at: datetime


class RefundPendingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_no: str
    username: str
    amount: float
    reason: str | None = None
    created_at: datetime


class RefundOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    order_id: int
    user_id: int
    amount: float
    reason: str | None
    status: str
    original_status: str | None = None
    created_at: datetime
    decision: str | None = None
    reasons: list[str] | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    message: str = Field(..., min_length=1, max_length=500)
    session_id: int | None = None


class SessionResumeRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    reply: str | None = Field(default=None, min_length=1, max_length=2000)
    decision: str | None = Field(default=None, pattern="^(approve|reject)$")
    comment: str | None = Field(default=None, max_length=500)
    approval_ref: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _reply_or_decision(self):
        if self.reply is None and self.decision is None:
            raise ValueError("reply 与 decision 至少提供其一")
        return self


class NeedsHumanRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    reason: str = Field(..., min_length=1, max_length=500)
    priority: str = Field(default="normal", pattern="^(low|normal|high)$")


class ChatResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    session_id: int
    answer: str


class OrderCreate(BaseModel):
    product_id: int
    quantity: int = Field(..., ge=1, description="购买数量必须大于 0")


class RefundCreate(BaseModel):
    # 金额与 user_id 一律服务端实读，请求体不接受。
    order_id: int | None = None
    order_no: str | None = Field(default=None, max_length=32)
    reason: str = Field(..., min_length=1, max_length=500)
    session_id: int | None = None

    @model_validator(mode="after")
    def _exactly_one_order_ref(self):
        if (self.order_id is None) == (self.order_no is None):
            raise ValueError("order_id 与 order_no 必须二选一")
        return self


class OrderStatusUpdate(BaseModel):
    status: str


class OrderActionRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    session_id: int | None = None


class AddressChangeRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    new_address: str = Field(..., min_length=1, max_length=200)
    session_id: int | None = None


class CancelRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    reason: str = Field(..., min_length=1, max_length=500)
    session_id: int | None = None


class TicketCreate(BaseModel):
    session_id: int
    type: str = Field(..., min_length=1, max_length=30)
    subject: str = Field(..., min_length=1, max_length=200)


class TicketOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ticket_no: str
    session_id: int
    user_id: int
    type: str
    subject: str
    status: str
    created_at: datetime


class AgentNoteRequest(BaseModel):
    note: str = Field(..., min_length=1, max_length=200)


class RefundReview(BaseModel):
    action: str = Field(..., pattern="^(approve|reject)$")
    comment: str | None = Field(default=None, max_length=500)


class ChatSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    needs_human: bool = False
    created_at: datetime
    updated_at: datetime
    last_message: str | None = None
    last_role: str | None = None
    title: str | None = None
    message_count: int = 0
    is_owner: bool = True
    username: str | None = None
    human_handler_id: int | None = None
    handler_username: str | None = None


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    session_id: int
    role: str
    content: str
    created_at: datetime
