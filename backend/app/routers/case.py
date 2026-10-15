"""模块 4 路由：案件列表 / 统计 / 详情 / 处置 / 暂存 / 关闭。

对应 SPEC §4.6 API-4.1 ~ API-4.6。
权限（R-AU-11）：
  - GET  /cases/**            reviewer / admin（admin 只读）
  - POST /cases/*/dispose|hold|close  仅 reviewer（admin 不可处置，PRD §2.1.1）
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import CASE_READ_ROLES, CASE_WRITE_ROLES, ApiError, ok
from ..models import RiskUser
from ..schemas import CaseClose, CaseDispose, CaseHold
from ..services import step4_case as case_svc
from ..services import step6_audit as audit_svc

router = APIRouter(prefix="/api/v1", tags=["案件流转"])

# 错误码 → 文案。⚠ 2004 已移除（v3.1）：部分失败改由响应体
# data.partial_failure 表达（R-CF-04），不再占用错误码编号。
ERROR_MESSAGES = {
    "2001": "案件不存在",
    "2002": "处置结论非法（不在 pass/reject/blacklist 内）",
    "2003": "案件状态不允许该操作（非法状态流转）",
}


@router.get("/cases")
def list_cases(
    case_status: str | None = None,
    status: str | None = None,                 # v2 兼容
    case_category: str | None = None,
    risk_level: str | None = None,
    event_type: str | None = None,
    scene_code: str | None = None,             # v2 兼容
    start_time: str | None = None,
    end_time: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    _: RiskUser = Depends(CASE_READ_ROLES),
    db: Session = Depends(get_db),
):
    """API-4.1 案件列表（复合筛选 + 分页 + 状态统计，FR-23 / FR-24）。"""
    data = case_svc.get_case_list(
        db,
        case_status=case_status or status,
        case_category=case_category,
        risk_level=risk_level,
        event_type=event_type or scene_code,
        start_time=start_time, end_time=end_time,
        page=page, page_size=page_size,
    )
    return ok(data)


@router.get("/cases/statistics")
def case_statistics(_: RiskUser = Depends(CASE_READ_ROLES), db: Session = Depends(get_db)):
    """API-4.2 案件统计（按状态、按分类，FR-24）。"""
    return ok(case_svc.get_case_statistics(db))


@router.get("/cases/{case_id}")
def case_detail(case_id: str, _: RiskUser = Depends(CASE_READ_ROLES),
                db: Session = Depends(get_db)):
    """API-4.3 案件详情聚合（FR-25~27）。

    ⚠ 本接口是**读写操作** —— 首次读取「待审核」案件时联动置为「审核中」（R-CF-02）。
    """
    data, err = case_svc.get_case_detail(db, case_id)
    if err:
        raise ApiError(err, ERROR_MESSAGES.get(err, "案件不存在"), 404)
    return ok(data)


@router.post("/cases/{case_id}/dispose")
def dispose_case(case_id: str, body: CaseDispose, background: BackgroundTasks,
                 user: RiskUser = Depends(CASE_WRITE_ROLES),
                 db: Session = Depends(get_db)):
    """API-4.4 提交处置结论并执行联动动作（FR-28~30）。

    - **同步**：执行 `block_order` / `blacklist_user` / `ban_device`，写处置明细与审计，提交事务
    - **异步**：把案件从「已处置」转入终态「已归档」（R-CF-03 / G-12）

    ⚠ 部分失败（R-CF-04）**不**改变 HTTP 状态码，而是通过响应体
    `data.partial_failure = true` + `disposals[].result = "failed"` 表达 ——
    请求本身是被受理并执行了的，用 4xx/5xx 会让前端把它当成「没执行」。
    """
    data, err = case_svc.dispose_case(db, case_id, body.conclusion, body.remark,
                                      user.username, body.actions)
    if err:
        raise ApiError(err, ERROR_MESSAGES.get(err, "处置失败"), 400)

    # R-CF-05：每次处置必须写 disposal_record 并同步写 audit_log
    audit_svc.write_audit(db, "case_dispose", case_id, "dispose", user.username,
                          role=user.role,
                          before=data.pop("_before", None),
                          after=data.pop("_after", None),
                          detail={"disposals": data.get("disposals")})

    # R-CF-03：处置事务已提交，异步归档（失败不影响已返回的处置结果）
    background.add_task(_archive_in_background, case_id, user.username)

    return ok(data)


def _archive_in_background(case_id: str, operator: str) -> None:
    """后台归档任务：独立开 Session，不依赖请求上下文。

    异步任务里**不能**复用请求的 `db` —— 请求结束即关闭。
    归档失败只记 warn（案件停在「已处置」，可被运维脚本补扫）。
    """
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        case_svc.archive_case(db, case_id, operator)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning("案件 %s 异步归档失败：%s", case_id, exc)
    finally:
        db.close()


@router.post("/cases/{case_id}/hold")
def hold_case(case_id: str, body: CaseHold = CaseHold(),
              user: RiskUser = Depends(CASE_WRITE_ROLES), db: Session = Depends(get_db)):
    """API-4.5 暂存挂起（审核中 → 待审核）。"""
    data, err = case_svc.hold_case(db, case_id, user.username)
    if err:
        raise ApiError(err, ERROR_MESSAGES.get(err, "暂存失败"), 400)

    audit_svc.write_audit(db, "case_dispose", case_id, "hold", user.username,
                          role=user.role,
                          before=data.pop("_before", None),
                          after=data.pop("_after", None))
    return ok(data)


@router.post("/cases/{case_id}/close")
def close_case(case_id: str, body: CaseClose = CaseClose(),
               user: RiskUser = Depends(CASE_WRITE_ROLES), db: Session = Depends(get_db)):
    """API-4.6 关闭为误报（待审核 → 已关闭，不处置）。"""
    data, err = case_svc.close_case(db, case_id, body.remark, user.username)
    if err:
        raise ApiError(err, ERROR_MESSAGES.get(err, "关闭失败"), 400)

    audit_svc.write_audit(db, "case_dispose", case_id, "close", user.username,
                          role=user.role,
                          before=data.pop("_before", None),
                          after=data.pop("_after", None))
    return ok(data)
