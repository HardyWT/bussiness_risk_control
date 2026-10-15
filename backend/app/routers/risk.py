"""模块 1 路由：事件网关。

对应 SPEC §1.4 API-1.1 / API-1.2、R-AU-06（外部业务系统走服务标识，不走 JWT）。
"""

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import ApiError, get_current_user, ok
from ..models import RiskUser
from ..schemas import RiskCheckRequest
from ..services import step1_event as event_svc

router = APIRouter(prefix="/api/v1", tags=["事件网关"])


def _check_service_id(x_service_id: str | None) -> None:
    """R-AU-06：v1.0 先校验固定 Header 服务标识（签名校验留 v2）。"""
    if x_service_id and x_service_id != settings.SERVICE_ID:
        raise ApiError("1004", f"服务标识无效：{x_service_id}", 401)


@router.post("/risk/check")
def risk_check(body: RiskCheckRequest,
               db: Session = Depends(get_db),
               x_service_id: str | None = Header(default=None)):
    """API-1.1 提交风控事件（核心接口）。

    ⚠ 本接口**不校验 JWT** —— 给外部业务系统调用，鉴权走 X-Service-Id（R-AU-06）。
    """
    _check_service_id(x_service_id)

    missing = body.validate_conditional()
    if missing:
        raise ApiError("1002", f"缺少必填字段：{missing}", 400)

    try:
        result = event_svc.process_event(db, body, dry_run=body.dry_run)
    except Exception as exc:  # noqa: BLE001
        raise ApiError("5000", f"内部错误：{exc}", 500) from exc

    result.pop("_dry_run", None)
    return ok(result)


@router.get("/risk/event/{event_id}")
def event_detail(event_id: str,
                 _: RiskUser = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    """API-1.2 查询事件详情（事件 + 33 特征快照 + 命中明细 + 评估）。"""
    data = event_svc.get_event_detail(db, event_id)
    if data is None:
        raise ApiError("1005", f"事件不存在：{event_id}", 404)
    return ok(data)


@router.get("/events")
def list_events(
    event_type: str | None = None,
    user_id: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    _: RiskUser = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """分页查询事件（支持类型 / 用户 / 时间过滤）。

    ⚠ 时间参数解析失败时必须**忽略该条件**：直接把 `parse_iso()` 的 `None` 拼进
    `WHERE create_time >= NULL`，SQL 求值恒为 NULL → 静默返回空列表，
    看起来像「这段时间确实没有事件」。`list_audit()` 已用 `datetime.min/max` 兜底，
    本接口与之保持一致。
    """
    from datetime import datetime

    from ..models import RiskAssessment, RiskEvent
    from ..utils import mask_phone, parse_iso

    query = db.query(RiskEvent, RiskAssessment).join(
        RiskAssessment, RiskAssessment.event_id == RiskEvent.event_id, isouter=True)
    if event_type:
        query = query.filter(RiskEvent.event_type == event_type)
    if user_id:
        query = query.filter(RiskEvent.user_id == user_id)
    start_dt = parse_iso(start_time) if start_time else None
    end_dt = parse_iso(end_time) if end_time else None
    if start_time and start_dt is None:
        raise ApiError("1003", "start_time 格式非法，应为 ISO8601（如 2026-10-01T00:00:00）", 400)
    if end_time and end_dt is None:
        raise ApiError("1003", "end_time 格式非法，应为 ISO8601（如 2026-10-14T23:59:59）", 400)
    if start_dt:
        query = query.filter(RiskEvent.create_time >= start_dt)
    else:
        query = query.filter(RiskEvent.create_time >= datetime.min)
    if end_dt:
        query = query.filter(RiskEvent.create_time <= end_dt)

    total = query.count()
    rows = (query.order_by(RiskEvent.create_time.desc())
            .offset((page - 1) * page_size).limit(page_size).all())

    return ok({
        "total": total, "page": page, "page_size": page_size,
        "items": [{
            "event_id": e.event_id,
            "event_type": e.event_type,
            "user_id": e.user_id,
            "source_id": e.source_id,
            "amount": float(e.amount or 0),
            "final_score": a.final_score if a else 0,
            "risk_level": a.risk_level if a else "低",
            "decision": a.decision if a else "Pass",
            "hit_source": a.hit_source if a else None,
            "phone": mask_phone(e.phone),
            "create_time": e.create_time.isoformat() if e.create_time else None,
        } for e, a in rows],
    })
