"""模块 3：决策引擎 —— 评分 + 一票否决 + 五档仲裁 + 七步流水线。

对应文档：
- Demo Doc-03 §3.3.3  评分算法（最高分 + 额外命中加分）
- Demo Doc-03 §3.3.4  一票否决（Veto）
- Demo Doc-03 §3.3.5  分值 → 等级 / 决策映射
- Demo Doc-03 §3.3.6  七步执行流水线
- SPEC §3.4          决策算法伪代码
- SPEC §3.7          R-RE-11 ~ R-RE-13

五档：Pass(<pass) / Mark([pass,mark)) / Challenge|Review([mark,reject)) / Reject(>=reject)
"""

import logging
import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..config import settings
from ..models import (RiskAssessment, RiskCase, RiskChallengeRecord,
                      RiskEvent, RiskFeature, RiskRuleScene, RiskUserProfile)
from ..utils import (new_assessment_id, new_case_id, new_event_id, now_dt,
                     round4, to_float)
from . import step2_feature as fe
from . import step3_rule as re_

log = logging.getLogger(__name__)


# ============================================================================
# 阈值配置
# ============================================================================
@dataclass
class DecisionConfig:
    """决策配置（从 risk_rule_scene 读取，不可硬编码 R-RE-10）。"""
    pass_threshold: int = 30
    mark_threshold: int = 60
    reject_threshold: int = 80
    multi_rule_bonus: int = 3
    veto_min_score: int = 90

    @classmethod
    def from_scene(cls, db: Session, scene_code: str) -> "DecisionConfig":
        scene = db.query(RiskRuleScene).filter(RiskRuleScene.scene_code == scene_code).first()
        if scene is None:
            scene = db.query(RiskRuleScene).filter(RiskRuleScene.scene_code == "all").first()
        if scene is None:
            return cls()
        return cls(
            pass_threshold=scene.pass_threshold,
            mark_threshold=scene.mark_threshold,
            reject_threshold=scene.reject_threshold,
            multi_rule_bonus=scene.multi_rule_bonus,
            veto_min_score=scene.veto_min_score,
        )


# ============================================================================
# 评分与判定（纯函数，便于单测）
# ============================================================================
def calculate_final_score(hits: list[re_.RuleHitResult], bonus: int | None = None) -> int:
    """`final = min(max(命中分值) + BONUS × (命中数-1), 100)`（Demo Doc-03 §3.3.3）。

    以最高分为主，避免低分规则过度叠加；额外规则的 BONUS 体现「数量佐证」而非「数量决定」。
    """
    if not hits:
        return 0
    bonus = settings.RISK_MULTI_RULE_BONUS if bonus is None else bonus
    max_score = max(h.risk_score for h in hits)
    return min(max_score + bonus * (len(hits) - 1), 100)


def check_veto(hits: list[re_.RuleHitResult]) -> bool:
    """一票否决：命中任一「极高」风险等级规则（Demo Doc-03 §3.3.4）。"""
    return any(h.risk_level == "极高" for h in hits)


def apply_veto(score: int, is_veto: bool, veto_min: int | None = None) -> int:
    """否决后分值至少为 veto_min_score（默认 90）。"""
    if not is_veto:
        return score
    return max(score, settings.RISK_VETO_MIN_SCORE if veto_min is None else veto_min)


def score_to_level(score: int, cfg: DecisionConfig) -> str:
    """分值 → 风险等级（四档：低 / 中 / 高 / 极高）。"""
    if score < cfg.pass_threshold:
        return "低"
    if score < cfg.mark_threshold:
        return "中"
    if score < cfg.reject_threshold:
        return "高"
    return "极高"


def arbitrate(score: int, hits: list[re_.RuleHitResult], cfg: DecisionConfig) -> tuple[str, str, bool]:
    """五档仲裁（SPEC R-RE-13），返回 (风险等级, 决策, 是否需要二次验证)。

    - `< pass`                     → Pass
    - `[pass, mark)`               → Mark 标记观察（记录并打标，放行）
    - `>= reject`                  → Reject
    - `[mark, reject)` 中风险档     → 命中含不可挑战规则 → Review；全部可挑战 → Challenge
    """
    if score < cfg.pass_threshold:
        return "低", "Pass", False
    if score < cfg.mark_threshold:
        return "中", "Mark", False
    if score >= cfg.reject_threshold:
        return "极高", "Reject", False
    if not hits or any(not h.challengeable for h in hits):
        return "高", "Review", False
    return "高", "Challenge", True


# ============================================================================
# 决策结果
# ============================================================================
@dataclass
class DecisionResult:
    final_score: int
    risk_level: str
    decision: str
    hits: list[re_.RuleHitResult] = field(default_factory=list)
    hit_source: str = "rule"          # whitelist / blacklist / gray / rule
    is_veto: bool = False
    need_challenge: bool = False
    cost_ms: float = 0.0

    @property
    def rule_count(self) -> int:
        return len(self.hits)


def decide_from_hits(hits: list[re_.RuleHitResult], cfg: DecisionConfig,
                     hit_source: str = "rule") -> DecisionResult:
    """由命中规则推导完整决策（不含落库）。"""
    score = calculate_final_score(hits, cfg.multi_rule_bonus)
    is_veto = check_veto(hits)
    score = apply_veto(score, is_veto, cfg.veto_min_score)
    level, decision, need_challenge = arbitrate(score, hits, cfg)
    return DecisionResult(score, level, decision, hits, hit_source, is_veto, need_challenge)


# ============================================================================
# 七步执行流水线（Demo Doc-03 §3.3.6）
# ============================================================================
def run_risk_check(db: Session, request, *, event_id: str | None = None,
                   dry_run: bool = False, hit_source_override: str | None = None,
                   precomputed_hits: list[re_.RuleHitResult] | None = None,
                   event_ctx: dict | None = None) -> dict:
    """完整风险检查流水线。

    Step 1 创建事件记录 → Step 2 计算特征 → Step 3 规则匹配 → Step 4 评分与仲裁
    → Step 5 持久化评估 → Step 6 按需建案 → Step 7 更新用户画像

    ⚠ `dry_run=True` 时**不落库**（仿真，R-SM-01），但**其余行为与线上完全一致**
      —— 包括参数补全结果（R-SM-02）。

    `event_ctx`：事件处理管道补全后的上下文字典。传入时以它为准构建事件，
    保证仿真与线上拿到同一套 `order_id` / `receive_id` / `logistics_id`。
    """
    t0 = time.perf_counter()

    from ..utils import next_case_no

    event_dict = {
        "event_id": event_id or new_event_id(),
        "event_type": request.event_type,
        "source_id": request.source_id,
        "user_id": request.user_id,
        "device_id": request.device_id,
        "ip": request.ip,
        "phone": request.phone,
        "receive_id": (event_ctx or {}).get("receive_id") or request.receive_id,
        # 金额：优先用调用方传入的，其次用管道补全出的（从订单明细反查）
        "amount": to_float(getattr(request, "amount", None)) or to_float(
            (event_ctx or {}).get("amount")),
        "event_time": request.event_time,
        "event_data": request.event_data,
    }
    order_id = (event_ctx or {}).get("order_id") or getattr(request, "order_id", None)
    if order_id:
        event_dict["order_id"] = order_id

    # ---- Step 1：事件时间 → 先写入窗口（R-FE-02：目标事件计入自身特征）----
    fe.record_event(event_dict)

    # ---- Step 2：计算 33 个特征 ----
    features = fe.compute_all_features(db, event_dict)

    # ---- Step 3：加载并匹配规则 ----
    cfg = DecisionConfig.from_scene(db, request.event_type)
    hits = precomputed_hits
    if hits is None:
        rules = re_.load_enabled_rules(db, event_type=request.event_type)
        hits = re_.match_rules(rules, features)

    # ---- Step 4：评分与仲裁 ----
    result = decide_from_hits(hits, cfg, hit_source=hit_source_override or "rule")
    result.cost_ms = round((time.perf_counter() - t0) * 1000, 3)

    if dry_run:
        # 仿真：不落库，直接返回
        return _build_response(event_dict, result, cfg, case_id=None, challenge=None,
                               features=features, dry_run=True)

    # ---- Step 5：持久化（事件 + 特征 + 评估，同一事务）----
    created_at = _event_time_as_dt(request.event_time)
    event_row = RiskEvent(
        event_id=event_dict["event_id"],
        event_type=request.event_type,
        source_id=request.source_id,
        user_id=request.user_id,
        device_id=request.device_id,
        ip=request.ip,
        phone=request.phone,
        # 与特征计算使用同一个 receive_id（补全后的），避免「算的是 A、存的是 B」
        address_id=event_dict["receive_id"],
        amount=event_dict["amount"],
        event_data=request.event_data,
        event_time=created_at,
        create_time=created_at,
    )
    db.add(event_row)
    db.flush()                             # 先写父表，避免外键顺序问题

    baselines = fe.baseline_features(db, exclude_event_id=event_dict["event_id"])
    for key, value in features.items():
        db.add(RiskFeature(
            event_id=event_dict["event_id"],
            dimension=fe.DIMENSION_OF.get(key, "其他"),
            feature_name=key,
            feature_value=round4(value),
            baseline_value=round4(baselines.get(key, 0.0)),
            compute_time=created_at,
        ))

    assessment_row = RiskAssessment(
        assessment_id=new_assessment_id(),
        event_id=event_dict["event_id"],
        user_id=request.user_id,
        rule_results=[h.to_dict() for h in result.hits],
        rule_count=result.rule_count,
        final_score=result.final_score,
        risk_level=result.risk_level,
        decision=result.decision,
        is_veto=int(result.is_veto),
        hit_source=result.hit_source,
        cost_ms=result.cost_ms,
        create_time=created_at,
    )
    db.add(assessment_row)

    # ---- Step 5b：画像与图谱增量更新（R-PG-05，与事件落库同批次）----
    try:
        from ..services import step5_graph as graph
        graph.touch_event(db, event_dict, features, created_at)
    except Exception as exc:  # noqa: BLE001  图谱失败不阻断决策
        log.warning("实体关系增量更新失败：%s", exc)

    # ---- Step 6：按需创建案件（Review / Reject → 100% 建案，R-CF-01）----
    case_row = None
    challenge_row = None
    if result.decision in ("Review", "Reject"):
        case_row = RiskCase(
            case_id=new_case_id(),
            case_no=next_case_no(db),
            assessment_id=assessment_row.assessment_id,
            event_id=event_dict["event_id"],
            user_id=request.user_id,
            case_status="待审核",
            case_category=_pick_case_category(result.hits),
            final_score=result.final_score,
            risk_level=result.risk_level,
            create_time=created_at,
        )
        db.add(case_row)
    elif result.decision == "Challenge":
        # Challenge 不建案（PRD §2.4），写二次验证记录
        challenge_row = RiskChallengeRecord(
            challenge_id=new_event_id().replace("evt", "chl"),
            event_id=event_dict["event_id"],
            channel="sms",
            target=_mask(request.phone),
            status="pending",
            issued_at=created_at,
        )
        db.add(challenge_row)

    # ---- Step 7：UPSERT 用户画像 ----
    _upsert_user_profile(db, request.user_id, features, result, created_at)

    db.commit()

    # 超时二次验证的兜底扫描**不放这里**（v3.1）。
    # 曾经把它挂在决策返回前，结果是：一个与二次验证毫无关系的登录事件，
    # 就能把历史上所有 pending 挑战一次性改写成 Review —— 111 条评估被静默重写，
    # 大盘的「决策分布」里 Challenge 从 111 直接变 0，而且没人知道是谁改的。
    # 它本质是**周期性后台任务**，见 `main.py` 的 lifespan 与 R-RE-15。
    return _build_response(
        event_dict, result, cfg,
        case_id=case_row.case_id if case_row else None,
        challenge=_challenge_payload(challenge_row) if challenge_row else None,
        features=features, dry_run=False,
        assessment_id=assessment_row.assessment_id,
    )


# ============================================================================
# 辅助函数
# ============================================================================
def _event_time_as_dt(value: str | None):
    from ..utils import parse_iso
    return parse_iso(value) or now_dt()


def _pick_case_category(hits: list[re_.RuleHitResult]) -> str:
    """案件分类取**最高分规则**的 rule_category（Demo Doc-03 §3.3.6 Step 6 / R-CF-01）。"""
    if not hits:
        return "未知"
    return max(hits, key=lambda h: h.risk_score).rule_category


def _mask(phone: str | None) -> str | None:
    from ..utils import mask_phone
    return mask_phone(phone)


def _challenge_payload(row: RiskChallengeRecord) -> dict:
    return {
        "type": "sms_captcha",
        "channel": row.channel,
        "token": row.challenge_id,
        "target": row.target,
        "expire_in": settings.CHALLENGE_WINDOW_SECONDS,
        "reason": "中风险事件需二次验证",
        "case": None,
    }


def _upsert_user_profile(db: Session, user_id: str, features: dict[str, float],
                         result: DecisionResult, created_at) -> None:
    """Step 7：用户画像 UPSERT（Demo Doc-03 §3.3.6）。

    ⚠ 风险标签的实现**只有一份**，在 `services/step5_graph.derive_risk_tags()`。
    决策流水线写画像、图谱模块写实体时都调它 —— 两处各写一份的话，
    条件一旦分叉就会出现「画像说高风险、图谱说没标签」的自相矛盾（SPEC §5.4）。
    """
    from ..services.step5_graph import derive_risk_tags

    profile = db.query(RiskUserProfile).filter(RiskUserProfile.user_id == user_id).first()
    if profile is None:
        profile = RiskUserProfile(user_id=user_id)
        db.add(profile)

    tags = derive_risk_tags(features)
    profile.risk_score = result.final_score
    profile.risk_level = result.risk_level
    profile.total_orders = int(features.get("user_total_orders", 0))
    profile.total_amount = to_float(features.get("user_total_amount"))
    profile.total_refunds = int(features.get("user_refund_count", 0))
    profile.refund_rate = round4(features.get("user_refund_rate"))
    profile.avg_order_amount = to_float(features.get("user_avg_order_amount"))
    profile.address_count = int(features.get("user_address_count", 0))
    profile.complaint_count = int(features.get("user_complaint_count", 0))
    profile.assessment_count = int(profile.assessment_count or 0) + 1
    profile.risk_tags = tags
    profile.last_assessment_time = created_at
    profile.update_time = created_at


def _build_response(event_dict: dict, result: DecisionResult, cfg: DecisionConfig,
                    case_id: str | None, challenge: dict | None,
                    features: dict[str, float], dry_run: bool,
                    assessment_id: str | None = None) -> dict:
    """组装 API-1.1 响应体。

    ⚠ `assessment_id` 必须由调用方传入**已落库的那个**（`assessment_row.assessment_id`）。
    v3.0 在这里再调一次 `new_assessment_id()`，导致响应里的评估编号与
    `risk_assessment` 表的实际主键**不是同一个值** —— 客户端拿它反查永远查不到，
    案件详情页与事件详情页也无法据此串联。PRD §9.1 FR-01 已把「响应标识与落库标识一致」
    写成验收标准。
    """
    from ..utils import mask_phone, to_iso

    return {
        "assessment_id": ("simulate" if dry_run else assessment_id) or "simulate",
        "event_id": event_dict["event_id"],
        "event_type": event_dict.get("event_type"),
        "source_id": event_dict.get("source_id"),
        "user_id": event_dict["user_id"],
        "final_score": result.final_score,
        "risk_level": result.risk_level,
        "decision": result.decision,
        "is_veto": result.is_veto,
        "rule_count": result.rule_count,
        "case_id": case_id,
        "hit_source": result.hit_source,
        "triggered_rules": [h.to_dict() for h in result.hits],
        "features": {k: round4(v) for k, v in features.items()},
        "challenge": challenge,
        "phone_masked": mask_phone(event_dict.get("phone")),
        "cost_ms": result.cost_ms,
        "thresholds": {
            "pass": cfg.pass_threshold,
            "mark": cfg.mark_threshold,
            "reject": cfg.reject_threshold,
            "multi_rule_bonus": cfg.multi_rule_bonus,
            "veto_min_score": cfg.veto_min_score,
        },
        "create_time": to_iso(now_dt()),
        # 供仿真页与自检使用，不对外暴露于正式契约
        "_dry_run": dry_run,
    }
