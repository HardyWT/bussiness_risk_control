"""Pydantic 请求 / 响应模型。

对应文档：
- SPEC §1.4  RiskCheckRequest（含字段校验规则；响应体契约见同节 JSON 示例）
- SPEC §3.8  规则 / 名单 / 阈值接口出入参
- SPEC §4.6  案件接口出入参
- SPEC §9.4  Agent 接口出入参
- SPEC §11.3 认证接口出入参
- Demo Doc-03 §2.4 Literal 枚举约束 + Field 范围约束
"""


from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .config import EVENT_TYPES, REQUIRE_BIZ_NO_TYPES, REQUIRE_DEVICE_TYPES

EventType = Literal["login", "coupon_receive", "order_create", "order_pay",
                    "after_sale_apply", "logistics_complaint"]
RuleCategory = Literal["订单欺诈", "支付风险", "账户风险", "售后滥用", "地址风险", "物流风险"]
RiskLevel = Literal["低", "中", "高", "极高"]
RuleAction = Literal["通过", "标记", "人工审核", "拒绝"]
CaseStatus = Literal["待审核", "审核中", "已处置", "已关闭", "已归档"]
Conclusion = Literal["放行", "拦截", "拉黑"]
ListType = Literal["black", "white", "gray"]
EntityType = Literal["user", "phone", "ip", "device", "address"]
# 角色常量在 `config.DEMO_USERS` 与 `deps.require_role(...)` 中定义；
# 角色从不作为请求体字段出现（由 JWT 解析得出），因此不需要 Literal 别名。


# ============================================================ 模块 1：事件网关
class RiskCheckRequest(BaseModel):
    """POST /api/v1/risk/check 请求体（SPEC §1.4）。"""
    event_type: EventType
    source_id: str = Field(..., min_length=1, max_length=64, description="业务单据号")
    user_id: str = Field(..., min_length=1, max_length=64)
    order_id: str | None = None
    receive_id: str | None = None
    device_id: str | None = None
    ip: str | None = None
    phone: str | None = None
    # ⚠ v3.1 补上 `amount`。v3.0 的模型里**没有这个字段**，而
    #   `run_risk_check()` 用 `getattr(request, "amount", 0)` 取金额写进
    #   `risk_event.amount` —— Pydantic 默认忽略未声明字段，于是每条事件的金额
    #   都是 0，「挽回资损」（PRD FR-46）恒等于 ¥0，卡片永远显示 0。
    #   缺省时由参数补全从订单明细反查（SPEC R-EV-08）。
    amount: float | None = Field(default=None, ge=0, description="金额；缺省时从订单明细反查")
    event_time: str | None = Field(default=None, description="ISO8601；缺省用当前时间")
    event_data: dict[str, Any] | None = None
    dry_run: bool = Field(default=False, description="true 时不落库（仿真）")

    @field_validator("ip")
    @classmethod
    def _check_ip(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        parts = str(v).split(".")
        if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            raise ValueError("ip 格式非法，应为合法 IPv4")
        return v

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        digits = "".join(ch for ch in str(v) if ch.isdigit())
        if len(digits) != 11:
            raise ValueError("phone 格式非法，应为 11 位数字")
        return digits

    def validate_conditional(self) -> str | None:
        """条件必填校验（SPEC §1.4），返回缺失字段名或 None。"""
        if self.event_type in REQUIRE_DEVICE_TYPES and not self.device_id:
            return "device_id"
        if self.event_type in REQUIRE_BIZ_NO_TYPES and not self.source_id:
            return "source_id"
        return None


# ⚠ 事件网关**响应体**不在此定义（v3.1 删除 `RiskCheckResponse` / `RuleHitInfo`）：
#   它由 `engine/step3_decision._build_response()` 直接构造 dict，外层再由
#   `deps.ok()` 包成 `{code, message, data}`。用 Pydantic 模型描述它反而危险 ——
#   一旦挂成 `response_model`，FastAPI 会按模型**裁剪**字段，
#   `thresholds` / `event_type` / `source_id` 这些前端在用的键会被静默丢掉。
#   权威契约见 SPEC §1.4 API-1.1 的 JSON 示例，实现见 `_build_response()`。


# ============================================================ 模块 3：规则与名单
class RuleCreate(BaseModel):
    rule_name: str = Field(..., min_length=1, max_length=100)
    rule_category: RuleCategory
    event_type: str = Field(default="通用", max_length=20)
    rule_condition: dict[str, Any] = Field(..., description="JSON 条件树")
    risk_level: RiskLevel
    risk_score: int = Field(..., ge=0, le=100)
    action: RuleAction
    challengeable: int = Field(default=1, ge=0, le=1)
    priority: int = Field(default=0, ge=0, le=9999)
    description: str | None = None

    @field_validator("event_type")
    @classmethod
    def _check_event(cls, v: str) -> str:
        if v not in EVENT_TYPES and v != "通用":
            raise ValueError(f"event_type 非法，须为 {EVENT_TYPES} 之一或「通用」")
        return v


class RuleUpdate(BaseModel):
    rule_name: str | None = Field(default=None, max_length=100)
    rule_category: RuleCategory | None = None
    event_type: str | None = None
    rule_condition: dict[str, Any] | None = None
    risk_level: RiskLevel | None = None
    risk_score: int | None = Field(default=None, ge=0, le=100)
    action: RuleAction | None = None
    challengeable: int | None = Field(default=None, ge=0, le=1)
    priority: int | None = Field(default=None, ge=0, le=9999)
    description: str | None = None


class SceneThresholdUpdate(BaseModel):
    """页面七「风险阈值」—— 按 PRD §2.1.1 仅 admin 可改（FR-40a）。"""
    pass_threshold: int | None = Field(default=None, ge=0, le=100)
    mark_threshold: int | None = Field(default=None, ge=0, le=100)
    reject_threshold: int | None = Field(default=None, ge=0, le=100)
    multi_rule_bonus: int | None = Field(default=None, ge=0, le=50)
    veto_min_score: int | None = Field(default=None, ge=0, le=100)


class BlacklistCreate(BaseModel):
    list_type: ListType
    entity_type: EntityType
    entity_value: str = Field(..., min_length=1, max_length=200)
    reason: str | None = None
    expire_time: str | None = Field(default=None, description="ISO8601；空表示永久")


class BlacklistImportItem(BaseModel):
    entity_value: str
    reason: str | None = None


class BlacklistImport(BaseModel):
    list_type: ListType
    entity_type: EntityType
    items: list[BlacklistImportItem]


class ChallengeResolve(BaseModel):
    result: Literal["pass", "fail", "timeout", "deny"]


# ============================================================ 模块 4：案件
class CaseDispose(BaseModel):
    conclusion: Literal["pass", "reject", "blacklist"]
    remark: str | None = None
    actions: list[str] | None = Field(default=None, description="留空则按结论取默认动作集")


class CaseHold(BaseModel):
    remark: str | None = None


class CaseClose(BaseModel):
    remark: str | None = Field(default="确认为误报，关闭案件")


# ============================================================ 模块 7：仿真
class SimulateRun(RiskCheckRequest):
    dry_run: bool = True


# ============================================================ 模块 9：AI Agent
class AgentChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    session_id: str | None = None


class AgentClearRequest(BaseModel):
    session_id: str = Field(..., min_length=1)


class ToolCallRecord(BaseModel):
    tool: str
    args: dict[str, Any] = {}
    result_summary: str = ""
    cost_ms: int = 0
    status: str = "ok"


class AgentChatResponse(BaseModel):
    reply: str
    session_id: str
    tool_calls: list[ToolCallRecord] = []
    degraded: bool = False
    degrade_reason: str | None = None


# ============================================================ 模块 11：认证
class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)


class ChangePassword(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8)
