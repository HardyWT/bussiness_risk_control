"""模块 1：事件处理管道 —— 名单前置拦截 + 参数自动补全。

对应文档：
- Demo Doc-03 §4.1  事件处理管道（process_event → 黑名单检查 → enrich → run_risk_check）
- SPEC §1.5         R-EV-01 ~ R-EV-09
- SPEC §3.7         R-RE-01/02/03/08（名单匹配优先级）

⚠ 名单匹配**前置**于特征计算与规则匹配 —— 命中黑名单直接 Reject(100)，不产生特征开销（FR-11）。
"""

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..engine import step3_decision as dec
from ..models import RiskBlacklist
from ..utils import from_json, now_dt, to_float

log = logging.getLogger(__name__)

# 五个维度 → 事件中的字段名
DIM_FIELDS = [
    ("user", "user_id"),
    ("phone", "phone"),
    ("ip", "ip"),
    ("device", "device_id"),
    ("address", "receive_id"),
]


# ============================================================================
# 名单匹配
# ============================================================================
def find_blacklist(db: Session, entity_type: str, value: str | None) -> RiskBlacklist | None:
    """按维度 + 值查名单（含过期判断，Demo Doc-03 §4.2.5 / FR-39）。

    `expire_time IS NULL` 表示永久；已过期的记录**不触发拦截**。
    """
    if not value:
        return None
    row = (db.query(RiskBlacklist)
           .filter(RiskBlacklist.entity_type == entity_type)
           .filter(RiskBlacklist.entity_value == str(value))
           .filter(RiskBlacklist.list_type.in_(["black", "white", "gray"]))
           .first())
    if row is None:
        return None
    if row.expire_time and row.expire_time < now_dt():
        return None                       # 已过期，不拦截
    return row


def match_watchlist(db: Session, event: dict) -> tuple[str, RiskBlacklist] | None:
    """五维度名单快筛（R-RE-01/02/03/08）。

    优先级：**白名单 > 黑名单 > 灰名单**（R-RE-08：同在白/黑名单时按白名单处理）。
    同一类型内按 user → phone → ip → device → address 顺序命中即返回。
    """
    for list_type in ("white", "black", "gray"):
        for entity_type, field in DIM_FIELDS:
            value = event.get(field)
            if not value:
                continue
            row = (db.query(RiskBlacklist)
                   .filter(RiskBlacklist.list_type == list_type)
                   .filter(RiskBlacklist.entity_type == entity_type)
                   .filter(RiskBlacklist.entity_value == str(value))
                   .first())
            if row is not None and (row.expire_time is None or row.expire_time >= now_dt()):
                return list_type, row
    return None


def bump_hit_count(db: Session, row: RiskBlacklist) -> None:
    row.hit_count = int(row.hit_count or 0) + 1


def check_blacklist(db: Session, entity_type: str, value: str | None) -> bool:
    """供 AI 工具与外部复用的单点检查。"""
    row = find_blacklist(db, entity_type, value)
    return row is not None and row.list_type == "black"


# ============================================================================
# 参数自动补全（R-EV-08）
# ============================================================================
def enrich_request(db: Session, request) -> dict:
    """按事件类型自动关联业务数据，返回事件上下文字典（Demo Doc-03 §4.1.3）。

    | 事件类型 | source_id 含义 | 补全逻辑 |
    | order_create / order_pay | order_id | receive_id 与金额从 order_info / order_detail 反查 |
    | after_sale_apply | postsale_id | postsale → order_detail → order_info 三表 JOIN |
    | logistics_complaint | 投诉记录 ID | logistics_complaints_record → logistics |

    ⚠ **金额补全（v3.1 新增）**：`amount` 缺省时从订单明细 `SUM(final_amount)` 反查。
    没有这一步时，调用方不传 `amount` 的事件在库里金额恒为 0，
    「挽回资损」（PRD FR-46）就永远是 0 —— 统计口径上看不出错，但整块功能是死的。
    """
    ctx: dict = {}
    event_type = request.event_type
    source_id = request.source_id

    try:
        if event_type in ("order_create", "order_pay"):
            ctx["order_id"] = request.order_id or source_id
            if not request.receive_id:
                row = db.execute(text(
                    "SELECT user_id, receive_id, create_time FROM order_info WHERE order_id = :oid"
                ), {"oid": ctx["order_id"]}).first()
                if row:
                    ctx["receive_id"] = row.receive_id
                    ctx.setdefault("order_user_id", row.user_id)
                else:
                    log.warning("订单 %s 不存在，地址与金额补全失败（R-EV-09 不阻断）",
                                ctx["order_id"])

        elif event_type == "after_sale_apply":
            row = db.execute(text("""
                SELECT oi.order_id, oi.receive_id, oi.user_id
                FROM postsale p
                JOIN order_detail od ON p.order_detail_id = od.order_detail_id
                JOIN order_info   oi ON od.order_id = oi.order_id
                WHERE p.postsale_id = :pid
            """), {"pid": source_id}).first()
            if row:
                ctx["order_id"] = row.order_id
                ctx["receive_id"] = row.receive_id
                ctx.setdefault("order_user_id", row.user_id)
            else:
                # ⚠ 容错分支（v3.1 新增）：`source_id` 装了**订单号**而不是售后单号。
                #
                #   真实成因有两类，都值得容忍而不是丢上下文：
                #     ① 对接方把「业务单据号」字段统一填成了订单号（集成里很常见）；
                #     ② 演示数据集里，某用户没有售后单却要发一条售后事件，
                #        生成器只能退化为绑定其订单。
                #   之前这里直接记 warn 并放弃补全，后果是：
                #     · 每回放一次就刷一屏「售后单 O-000xxx 未关联到订单」的告警，
                #       看起来像系统有病，其实是输入语义不清；
                #     · 这 70 条事件的订单维度 8 个特征与金额全为 0。
                #   按订单号再补一次：同样的信息本来就在 order_info 里，没有理由取不到。
                order_id = request.order_id or source_id
                row = db.execute(text(
                    "SELECT user_id, receive_id, create_time FROM order_info "
                    "WHERE order_id = :oid"
                ), {"oid": order_id}).first()
                if row:
                    ctx["order_id"] = order_id
                    ctx["receive_id"] = row.receive_id
                    ctx.setdefault("order_user_id", row.user_id)
                    log.info(
                        "售后事件 %s 的 source_id 是订单号（非售后单号），已按订单补全上下文",
                        source_id)
                else:
                    # 既不是售后单号、也不是订单号 —— 这才是真问题
                    log.warning("售后单 %s 无法补全（既非 postsale_id 也非 order_id，"
                                "R-EV-09 不阻断）", source_id)

        elif event_type == "logistics_complaint":
            row = db.execute(text(
                "SELECT logistics_id, user_id FROM logistics_complaints_record "
                "WHERE record_id = :rid"
            ), {"rid": _to_int(source_id)}).first()
            if row:
                ctx["logistics_id"] = row.logistics_id
                ctx.setdefault("order_user_id", row.user_id)

        # coupon_receive / login 不关联订单

        # 金额缺省时从订单明细反查（有订单号才做）
        if not request.amount and ctx.get("order_id"):
            total = db.execute(text(
                "SELECT COALESCE(SUM(final_amount), 0) FROM order_detail WHERE order_id = :oid"
            ), {"oid": ctx["order_id"]}).scalar()
            if total and to_float(total) > 0:
                ctx["amount"] = to_float(total)
    except Exception as exc:  # noqa: BLE001  R-EV-09：补全失败不阻断
        log.warning("事件 %s 参数补全失败：%s", source_id, exc)

    return ctx


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ============================================================================
# 管道主入口
# ============================================================================
def process_event(db: Session, request, *, dry_run: bool = False,
                  event_id: str | None = None) -> dict:
    """统一入口（Demo Doc-03 §4.1.2）。

    流程：参数补全 → 名单前置拦截 → 决策流水线。

    ⚠ **参数补全结果必须无条件向下传递**（SPEC R-SM-02）。v3.0 只在
    `not dry_run` 时把补全出的 `receive_id` / `order_id` 回写到请求对象，
    于是仿真的决策链路拿不到地址与订单上下文 —— 算出的 `addr_is_new` /
    `addr_account_cnt_7d` / `order_*` 与线上不一致，「仿真走完整决策链路、
    与线上一致」的承诺失效。本版把补全结果放进 `event_ctx` 显式传给流水线，
    不再依赖对请求对象的副作用。
    """
    event_dict = {
        "event_type": request.event_type,
        "source_id": request.source_id,
        "user_id": request.user_id,
        "device_id": request.device_id,
        "ip": request.ip,
        "phone": request.phone,
        "receive_id": request.receive_id,
        "amount": to_float(getattr(request, "amount", 0)),
        "event_time": request.event_time,
    }

    # ---- 参数补全（补全后再做名单匹配，保证 address 维度可命中）----
    ctx = enrich_request(db, request)
    for key in ("order_id", "receive_id", "logistics_id"):
        if ctx.get(key) and not event_dict.get(key):
            event_dict[key] = ctx[key]
    if not event_dict.get("amount") and ctx.get("amount"):
        event_dict["amount"] = ctx["amount"]

    # ---- 名单前置拦截（R-RE-01/02/03）----
    hit = match_watchlist(db, event_dict)
    hit_source = None
    if hit is not None:
        list_type, row = hit
        hit_source = {"white": "whitelist", "black": "blacklist", "gray": "gray"}[list_type]
        if not dry_run:
            bump_hit_count(db, row)
            db.commit()

        if list_type == "white":
            # 白名单 → 直接 Pass，得分 0，不产生规则命中（R-RE-01）
            return _shortcut_response(db, request, event_dict, event_id,
                                      score=0, level="低", decision="Pass",
                                      hit_source="whitelist", dry_run=dry_run)
        if list_type == "black":
            # 黑名单 → 直接 Reject，得分 100、等级「极高」（R-RE-02）
            return _shortcut_response(db, request, event_dict, event_id,
                                      score=100, level="极高", decision="Reject",
                                      hit_source="blacklist", dry_run=dry_run)
        # 灰名单 → 仅记录，继续走规则（R-RE-03）

    # ---- 决策流水线（把补全后的上下文一并传入）----
    return dec.run_risk_check(db, request, event_id=event_id, dry_run=dry_run,
                              hit_source_override=hit_source, event_ctx=event_dict)


def _shortcut_response(db: Session, request, event_dict: dict, event_id: str | None,
                       score: int, level: str, decision: str,
                       hit_source: str, dry_run: bool) -> dict:
    """名单短路响应（不产生特征与规则明细）。"""
    from ..engine.step3_decision import DecisionConfig, _build_response, DecisionResult
    from ..utils import new_event_id

    cfg = DecisionConfig.from_scene(db, request.event_type)
    is_veto = decision == "Reject" and hit_source == "blacklist"
    result = DecisionResult(score, level, decision, [], hit_source, is_veto, False, 0.0)
    event_dict = {**event_dict, "event_id": event_id or new_event_id()}

    response = _build_response(event_dict, result, cfg, case_id=None, challenge=None,
                               features={}, dry_run=True)
    response["assessment_id"] = "blacklist_reject" if decision == "Reject" else "whitelist_pass"
    response["_dry_run"] = dry_run

    if dry_run:
        return response

    # 名单短路仍需落事件与评估（审计链完整），但不建案（R-RE-01/02）
    from ..engine.step3_decision import _event_time_as_dt
    from ..models import RiskAssessment, RiskEvent
    from ..utils import new_assessment_id

    created_at = _event_time_as_dt(request.event_time)
    db.add(RiskEvent(
        event_id=event_dict["event_id"], event_type=request.event_type,
        source_id=request.source_id, user_id=request.user_id,
        device_id=request.device_id, ip=request.ip, phone=request.phone,
        # 用补全后的值落库，保证「算的」与「存的」一致
        address_id=event_dict.get("receive_id"),
        amount=to_float(event_dict.get("amount")),
        event_data=request.event_data, event_time=created_at, create_time=created_at,
    ))
    db.flush()
    assessment_id = new_assessment_id()
    db.add(RiskAssessment(
        assessment_id=assessment_id, event_id=event_dict["event_id"],
        user_id=request.user_id, rule_results=[], rule_count=0,
        final_score=score, risk_level=level, decision=decision,
        is_veto=int(is_veto), hit_source=hit_source, cost_ms=0.0, create_time=created_at,
    ))
    db.commit()
    response["assessment_id"] = assessment_id
    return response


# ============================================================================
# 事件详情查询（API-1.2）
# ============================================================================
def get_event_detail(db: Session, event_id: str) -> dict | None:
    """事件 + 33 个特征快照 + 命中明细 + 评估结果。"""
    from ..models import RiskAssessment, RiskEvent, RiskFeature
    from ..utils import mask_phone

    row = db.query(RiskEvent).filter(RiskEvent.event_id == event_id).first()
    if row is None:
        return None

    features = {
        f.feature_name: {"value": to_float(f.feature_value),
                         "baseline": to_float(f.baseline_value),
                         "dimension": f.dimension}
        for f in db.query(RiskFeature).filter(RiskFeature.event_id == event_id).all()
    }
    assessment = db.query(RiskAssessment).filter(RiskAssessment.event_id == event_id).first()

    return {
        "event_id": row.event_id,
        "event_type": row.event_type,
        "source_id": row.source_id,
        "user_id": row.user_id,
        "device_id": row.device_id,
        "ip": row.ip,
        "phone": mask_phone(row.phone),
        "receive_id": row.address_id,
        "amount": to_float(row.amount),
        "event_data": from_json(row.event_data),
        "create_time": row.create_time.isoformat() if row.create_time else None,
        "features": features,
        "assessment": {
            "assessment_id": assessment.assessment_id,
            "final_score": assessment.final_score,
            "risk_level": assessment.risk_level,
            "decision": assessment.decision,
            "is_veto": bool(assessment.is_veto),
            "hit_source": assessment.hit_source,
            "rule_count": assessment.rule_count,
            "cost_ms": to_float(assessment.cost_ms),
            "triggered_rules": from_json(assessment.rule_results) or [],
        } if assessment else None,
    }
