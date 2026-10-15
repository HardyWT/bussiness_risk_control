"""模块 6 路由：态势大盘指标 + 系统健康度。

对应 SPEC §6.4 API-6.1 ~ API-6.11。
权限（R-AU-11）：
  - /dashboard/**   全部角色
  - /system/**      仅 admin / ops
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import DASHBOARD_ROLES, SYSTEM_ROLES, ok
from ..models import RiskUser
from ..services import step6_audit as audit_svc

router = APIRouter(prefix="/api/v1", tags=["监控与审计"])


# ================================================================ 大盘
@router.get("/dashboard/overview")
def overview(_: RiskUser = Depends(DASHBOARD_ROLES), db: Session = Depends(get_db)):
    """API-6.1 核心指标卡（FR-41）。"""
    return ok(audit_svc.dashboard_overview(db))


@router.get("/dashboard/trend")
def trend(days: int | None = Query(default=None, ge=1, le=365),
          hours: int | None = Query(default=None, ge=1, le=8760),
          event_type: str | None = Query(default=None),
          _: RiskUser = Depends(DASHBOARD_ROLES), db: Session = Depends(get_db)):
    """API-6.2 风险趋势（FR-42）。缺失日期补 0（R-MA-05）。

    兼容 `?days=7`（SPEC）与 `?hours=24`（旧契约）；
    `?event_type=` 可选，按事件类型过滤。
    """
    if days is None:
        days = max(1, round((hours or 168) / 24))
    return ok(audit_svc.dashboard_trend(db, days, event_type))


@router.get("/dashboard/risk_dist")
def risk_dist(_: RiskUser = Depends(DASHBOARD_ROLES), db: Session = Depends(get_db)):
    """API-6.3 风险等级分布（**四档**：低/中/高/极高，FR-43）。"""
    return ok(audit_svc.dashboard_risk_dist(db))


@router.get("/dashboard/top_rules")
def top_rules(top: int = Query(default=5, ge=1, le=50),
              _: RiskUser = Depends(DASHBOARD_ROLES), db: Session = Depends(get_db)):
    """API-6.4 规则命中排行 TOP N（FR-44）。返回**分页对象**（含 items 键）。"""
    return ok(audit_svc.dashboard_top_rules(db, top))


@router.get("/dashboard/realtime_events")
def realtime_events(limit: int = Query(default=12, ge=1, le=100),
                    _: RiskUser = Depends(DASHBOARD_ROLES), db: Session = Depends(get_db)):
    """API-6.5 实时事件滚屏（FR-45）。前端按 5s 轮询刷新（R-FE2-01）。"""
    return ok(audit_svc.dashboard_realtime_events(db, limit))


@router.get("/dashboard/loss_prevented")
def loss_prevented(_: RiskUser = Depends(DASHBOARD_ROLES), db: Session = Depends(get_db)):
    """API-6.6 资损防护统计（FR-46）。"""
    return ok(audit_svc.dashboard_loss_prevented(db))


@router.get("/dashboard/rule_effectiveness")
def rule_effectiveness(days: int | None = Query(default=None, ge=1, le=365),
                       event_type: str | None = Query(default=None),
                       _: RiskUser = Depends(DASHBOARD_ROLES), db: Session = Depends(get_db)):
    """API-6.7 规则效果分析（FR-47）——命中次数 / 命中率 / 零命中识别。

    `?days=` 限定统计窗口（缺省全量），`?event_type=` 按事件类型过滤。
    """
    return ok(audit_svc.dashboard_rule_effectiveness(db, days, event_type))


# ================================================================ 系统
@router.get("/system/health")
def system_health(_: RiskUser = Depends(SYSTEM_ROLES), db: Session = Depends(get_db)):
    """API-6.10 吞吐、接口健康度与容量指标（FR-52）。仅 admin / ops。"""
    return ok(audit_svc.system_health(db))


@router.get("/system/stats")
def system_stats(_: RiskUser = Depends(SYSTEM_ROLES), db: Session = Depends(get_db)):
    """API-6.11 业务表与风控表行数统计（FR-55）。"""
    return ok(audit_svc.system_stats(db))


@router.get("/system/config")
def system_config(user: RiskUser = Depends(SYSTEM_ROLES)):
    """只读运行配置（API-6.12）。

    ⚠ 权限为 **admin / ops**（与 `/system/health`、`/system/stats` 同属运维面）。
    v3.0 挂的是 `get_current_user`（任意登录角色）—— 该接口会暴露登录策略
    （锁定阈值与时长）、LLM 模型名、图谱节点上限等运行参数，
    对审核员 / 分析师没有使用价值，属不必要的信息暴露。
    """
    return ok({
        "loss_discount": settings.LOSS_DISCOUNT,
        "challenge_window_seconds": settings.CHALLENGE_WINDOW_SECONDS,
        "rule_cache_ttl_seconds": settings.RULE_CACHE_TTL_SECONDS,
        "login_max_fail": settings.LOGIN_MAX_FAIL,
        "login_lock_seconds": settings.LOGIN_LOCK_SECONDS,
        "jwt_expire_seconds": settings.JWT_EXPIRE_SECONDS,
        "max_graph_nodes": settings.MAX_GRAPH_NODES,
        "llm_configured": settings.llm_configured,
        "llm_model_name": settings.LLM_MODEL_NAME,
        "role": user.role,
    })
