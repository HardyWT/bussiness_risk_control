"""模块 3：二次验证（Challenge）—— 结果回调。

对应文档：
- SPEC §3.7  R-RE-14（通过→Pass 不建案 / 失败→Review 建案）、R-RE-15（窗口 120s）、R-RE-16
- SPEC §3.8  API-3.15
- PRD §2.4   五档决策模型 · Challenge 状态机
"""

import logging
from datetime import timedelta

from sqlalchemy.orm import Session

from ..config import settings
from ..models import (RiskAssessment, RiskCase, RiskChallengeRecord, RiskEvent,
                      RiskRuleScene)
from ..utils import new_case_id, next_case_no, now_dt

log = logging.getLogger(__name__)

RESULT_PASS = "pass"
RESULT_FAIL = "fail"
RESULT_TIMEOUT = "timeout"
RESULT_DENY = "deny"


def is_expired(row: RiskChallengeRecord) -> bool:
    """挑战窗口默认 120s，超时视为 timeout（R-RE-15）。"""
    if row.issued_at is None:
        return False
    return (now_dt() - row.issued_at).total_seconds() > settings.CHALLENGE_WINDOW_SECONDS


def resolve_challenge(db: Session, event_id: str, result: str,
                      operator: str | None = None) -> tuple[dict | None, str | None]:
    """二次验证结果回调（API-3.15 / R-RE-14）。

    - 通过        → 决策升级为 Pass，**不建案**
    - 失败/超时/拒绝 → 决策升级为 Review，**创建案件**
    """
    row = (db.query(RiskChallengeRecord)
           .filter(RiskChallengeRecord.event_id == event_id)
           .order_by(RiskChallengeRecord.issued_at.desc()).first())
    if row is None or row.status != "pending":
        return None, "3005"                       # 挑战不存在或已结束

    # 即便调用方传 pass，超过窗口也按 timeout 处理
    if result == RESULT_PASS and is_expired(row):
        result = RESULT_TIMEOUT

    assessment = (db.query(RiskAssessment)
                  .filter(RiskAssessment.event_id == event_id).first())
    if assessment is None:
        return None, "3005"

    now = now_dt()

    if result == RESULT_PASS:
        row.status = "passed"
        row.resolved_at = now
        assessment.decision = "Pass"
        assessment.risk_level = "低"
        db.commit()
        return {
            "event_id": event_id, "decision": "Pass", "case_id": None,
            "challenge_status": row.status,
            "note": "二次验证通过，决策升级为 Pass（不建案）",
        }, None

    # 失败 / 超时 / 用户拒绝 → 统一升级 Review 并建案
    status = "timeout" if result == RESULT_TIMEOUT else "failed"
    note = ("二次验证超时未响应，已升级人审并创建案件"
            if result == RESULT_TIMEOUT else "二次验证未通过，已升级人审并创建案件")
    data, err = _escalate_to_review(db, row, status=status, note=note)
    if err is None:
        db.commit()
    return data, err


def _medium_level(db: Session, score: int) -> str:
    """按场景阈值回填中风险档的等级（升级 Review 时用）。"""
    scene = db.query(RiskRuleScene).filter(RiskRuleScene.scene_code == "all").first()
    pass_t = scene.pass_threshold if scene else 30
    reject_t = scene.reject_threshold if scene else 80
    if score < pass_t:
        return "低"
    if score < reject_t:
        return "高"
    return "极高"


def _pick_category(hits: list) -> str:
    if not hits:
        return "未知"
    try:
        return max(hits, key=lambda h: h.get("risk_score", 0)).get("rule_category", "未知")
    except (AttributeError, TypeError):
        return "未知"


def sweep_expired(db: Session, limit: int = 200) -> int:
    """兜底扫描超时的二次验证（SPEC R-RE-15）。

    ⚠ 为什么必须有这条路径：`is_expired()` 只在**回调到达时**才被求值。
    如果用户既不在页面上点验证、也没有任何回调（真实场景里这是常态 ——
    用户直接关掉页面就走了），挑战会永久停在 `pending`，
    对应事件的决策停在 `Challenge`：**既不放行、也不人审**，是「悬空事件」。
    PRD §6.1 明确要求「失败/超时 → Review 人审」。

    触发时机：挂在每次决策流水线返回前（成本极低 —— 按 `idx_ch_pending`
    走索引查 `status='pending'`，正常情况命中 0 条）。

    返回被升级的条数。
    """
    cutoff = now_dt() - timedelta(seconds=settings.CHALLENGE_WINDOW_SECONDS)
    rows = (db.query(RiskChallengeRecord)
            .filter(RiskChallengeRecord.status == "pending")
            .filter(RiskChallengeRecord.issued_at < cutoff)
            .order_by(RiskChallengeRecord.issued_at)
            .limit(limit).all())
    if not rows:
        return 0

    upgraded = 0
    for row in rows:
        # 复用回调路径的升级逻辑，保证「超时」与「回调失败」结果一致
        data, err = _escalate_to_review(db, row, status="timeout",
                                        note="二次验证超时未响应，已升级人审并创建案件")
        if err is None:
            upgraded += 1
    if upgraded:
        db.commit()
    return upgraded


def _escalate_to_review(db: Session, row: RiskChallengeRecord, status: str,
                        note: str) -> tuple[dict | None, str | None]:
    """把挑战对应的事件升级为 `Review` 并建案（幂等，R-RE-18）。"""
    event_id = row.event_id
    assessment = (db.query(RiskAssessment)
                  .filter(RiskAssessment.event_id == event_id).first())
    if assessment is None:
        return None, "3005"

    row.status = status
    row.resolved_at = now_dt()
    assessment.decision = "Review"
    assessment.risk_level = _medium_level(db, assessment.final_score)

    # R-RE-18：同一事件重复升级复用已有案件，不重复建案
    existing = db.query(RiskCase).filter(RiskCase.event_id == event_id).first()
    if existing is not None:
        return {"event_id": event_id, "decision": "Review",
                "case_id": existing.case_id, "challenge_status": status,
                "note": note}, None

    event = db.query(RiskEvent).filter(RiskEvent.event_id == event_id).first()
    case = RiskCase(
        case_id=new_case_id(),
        case_no=next_case_no(db),
        assessment_id=assessment.assessment_id,
        event_id=event_id,
        user_id=assessment.user_id,
        case_status="待审核",
        case_category=_pick_category(assessment.rule_results or []),
        final_score=assessment.final_score,
        risk_level=assessment.risk_level,
        create_time=now_dt(),
    )
    db.add(case)
    db.flush()
    return {"event_id": event_id, "decision": "Review", "case_id": case.case_id,
            "challenge_status": status, "note": note}, None


def get_challenge(db: Session, event_id: str) -> list[dict]:
    """查询某事件的二次验证记录（仿真页展示用）。"""
    rows = (db.query(RiskChallengeRecord)
            .filter(RiskChallengeRecord.event_id == event_id)
            .order_by(RiskChallengeRecord.issued_at.desc()).all())
    return [{
        "challenge_id": c.challenge_id,
        "event_id": c.event_id,
        "channel": c.channel,
        "target": c.target,
        "status": c.status,
        "issued_at": c.issued_at.isoformat() if c.issued_at else None,
        "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
    } for c in rows]
