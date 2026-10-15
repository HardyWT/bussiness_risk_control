"""运维路由：存活探针 + 数据重置与基准灌入（FR-54）。

⚠ 数据重置属破坏性操作，仅 admin 可调用，且必须写审计（R-MA-02）。
"""

from fastapi import APIRouter, Depends
from sqlalchemy import delete, func
from sqlalchemy.orm import Session

from .. import metrics
from ..config import settings
from ..database import get_db
from ..deps import ADMIN_ONLY, ok
from ..engine.step2_feature import reset_windows
from ..engine.step3_rule import clear_rule_cache
from ..models import (RiskAgentMessage, RiskAgentSession, RiskAssessment,
                      RiskAuditLog, RiskBlacklist, RiskCase,
                      RiskChallengeRecord, RiskDisposalRecord, RiskEntity,
                      RiskEntityRelation, RiskEvent, RiskFeature, RiskRule,
                      RiskRuleScene, RiskUser, RiskUserProfile)
from ..services.step6_audit import write_audit

router = APIRouter(prefix="/api/v1/admin", tags=["运维辅助"])

# 可重置的业务/风控数据表（配置表默认保留）
#
# ⚠ 顺序是**先子后父**，与风控表之间的 4 条外键对应
# （SPEC §4.3 已提示；顺序写反会直接抛外键约束错误，重置失败）：
#   risk_disposal_record.case_id      → risk_case
#   risk_challenge_record.event_id    → risk_event
#   risk_feature.event_id             → risk_event
#   risk_agent_message.session_id     → risk_agent_session
RESETTABLE = [
    RiskDisposalRecord,    # 子：依赖 risk_case
    RiskChallengeRecord,   # 子：依赖 risk_event
    RiskFeature,           # 子：依赖 risk_event
    RiskAgentMessage,      # 子：依赖 risk_agent_session
    RiskAgentSession,
    RiskAssessment,        # 逻辑外键指向 risk_event
    RiskCase,
    RiskEntityRelation,
    RiskEntity,
    RiskUserProfile,
    RiskEvent,             # 父：必须最后删
]
CONFIG_TABLES = [RiskRule, RiskRuleScene, RiskBlacklist, RiskUser]


@router.get("/health")
def health(db: Session = Depends(get_db)):
    """存活探针 + 实体计数（**无需鉴权**，用于启动自检）。"""
    return ok({
        "status": "ok",
        "service": "ercs-backend",
        "version": "v3.1",
        "llm_configured": settings.llm_configured,
        "counts": {
            "risk_event": db.query(func.count(RiskEvent.event_id)).scalar() or 0,
            "risk_assessment": db.query(func.count(RiskAssessment.assessment_id)).scalar() or 0,
            "risk_case": db.query(func.count(RiskCase.case_id)).scalar() or 0,
            "risk_rule": db.query(func.count(RiskRule.rule_id)).scalar() or 0,
            "risk_blacklist": db.query(func.count(RiskBlacklist.blacklist_id)).scalar() or 0,
            "risk_audit_log": db.query(func.count(RiskAuditLog.log_id)).scalar() or 0,
            "risk_user": db.query(func.count(RiskUser.user_id)).scalar() or 0,
        },
    })


@router.post("/reset")
def reset_data(keep_config: bool = True, user: RiskUser = Depends(ADMIN_ONLY),
               db: Session = Depends(get_db)):
    """数据重置（FR-54）。`keep_config=true` 时保留规则 / 阈值 / 名单 / 账号。"""
    # 审计日志不可删除（触发器禁止）—— 重置时也**不触碰**
    removed = {}
    for model in RESETTABLE:
        table = model.__tablename__
        removed[table] = db.execute(delete(model)).rowcount or 0

    if not keep_config:
        for model in CONFIG_TABLES:
            if model is RiskUser:
                continue                       # 账号永不清空（否则无法登录）
            table = model.__tablename__
            removed[table] = db.execute(delete(model)).rowcount or 0

    db.commit()
    reset_windows()
    clear_rule_cache()
    metrics.reset()          # 接口计数器归零，与数据一起回到干净基线

    write_audit(db, "system_config", "reset", "delete", user.username, role=user.role,
                detail={"keep_config": keep_config, "removed": removed})

    return ok({"keep_config": keep_config, "removed": removed,
               "note": "审计日志（risk_audit_log）不受重置影响"})


@router.post("/seed-config")
def seed_config(user: RiskUser = Depends(ADMIN_ONLY)):
    """仅灌入规则 / 场景 / 名单配置（不改业务数据）。"""
    from scripts.init_db import seed_config_only

    result = seed_config_only()
    return ok(result)
