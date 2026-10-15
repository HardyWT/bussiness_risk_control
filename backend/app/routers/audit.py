"""模块 6 路由：审计日志查询。

对应 SPEC §6.4 API-6.9、FR-53。
⚠ 本模块**只提供 GET** —— 不提供任何修改 / 删除接口，审计不可篡改是产品承诺
（SPEC §6.3 / R-MA-03；AC 直接验证 DELETE /api/v1/audit/{id} 返回 404/405）。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import AUDIT_ROLES, ok
from ..models import RiskUser
from ..services import step6_audit as audit_svc

router = APIRouter(prefix="/api/v1", tags=["监控与审计"])


@router.get("/audit/logs")
def list_logs(
    biz_type: str | None = None,
    operator: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    user: RiskUser = Depends(AUDIT_ROLES),
    db: Session = Depends(get_db),
):
    """API-6.9 审计日志查询（FR-53）。

    数据范围（PRD §2.1.1 / R-MA-10）：admin 全量；reviewer / strategist / ops 仅本人操作；
    分析师无权访问本接口（路由层已排除）。
    非 admin 时前端传的 operator 参数被**忽略**。
    """
    return ok(audit_svc.list_audit(db, user, biz_type, operator,
                                   start_time, end_time, page, page_size))


# 说明：故意不实现 DELETE /api/v1/audit/{log_id} 与 PUT/PATCH —— 见模块说明。
