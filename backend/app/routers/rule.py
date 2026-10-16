"""模块 3 路由：规则 CRUD、场景阈值、特征字典、名单库、二次验证回调。

对应 SPEC §3.8 API-3.1 ~ API-3.16。
权限（SPEC R-AU-11 / PRD §2.1.1）：
  - 读（rules / features / blacklist GET）：reviewer / strategist / analyst / admin（**ops 不可读**）
  - 写（rules / blacklist）：**仅 strategist** —— admin 对这两项为只读，
    与 PRD §2.1.1 的 👁 标记一致（HANDOFF §九 · 8.2 已记录曾误把 admin 放进写角色）
  - 阈值改（/rules/scenes/**）：**仅 admin**
  - 挑战相关：回调 strategist / admin；查询 strategist / analyst / admin
"""

from fastapi import APIRouter, Depends, Query

from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import (ADMIN_ONLY, AGENT_ROLES, ApiError, RULE_READ_ROLES,
                    STRATEGY_ROLES, ok)
from ..engine import step2_feature as fe
from ..engine import step3_rule as re_
from ..models import RiskRule, RiskRuleScene, RiskUser
from ..schemas import (BlacklistCreate, BlacklistImport, ChallengeResolve,
                       RuleCreate, RuleUpdate, SceneThresholdUpdate)
from ..services import step3_challenge as challenge_svc
from ..services import step4_case as case_svc
from ..services import step6_audit as audit_svc
from ..utils import now_dt

router = APIRouter(prefix="/api/v1", tags=["策略与名单"])


# ================================================================ 特征字典（API-3.9）
@router.get("/features/dict")
def features_dict(dimension: str | None = None,
                  _: RiskUser = Depends(RULE_READ_ROLES)):
    """特征字典（33 个，可按维度过滤）—— FR-10。"""
    return ok(fe.get_feature_dict(dimension))


# ================================================================ 规则 CRUD（API-3.1 ~ 3.6）
@router.get("/rules")
def list_rules(rule_category: str | None = None, event_type: str | None = None,
               is_enabled: int | None = None, scene_code: str | None = None,
               status: str | None = None,
               _: RiskUser = Depends(RULE_READ_ROLES), db: Session = Depends(get_db)):
    """规则列表（FR-31）。兼容 v2 的 `scene_code` / `status` 查询参数。"""
    query = db.query(RiskRule)
    if rule_category:
        query = query.filter(RiskRule.rule_category == rule_category)
    if event_type:
        query = query.filter(RiskRule.event_type == event_type)
    if is_enabled is not None:
        query = query.filter(RiskRule.is_enabled == is_enabled)
    if scene_code:                                   # v2 兼容
        query = query.filter(RiskRule.event_type == scene_code)
    if status:                                       # v2 兼容
        query = query.filter(RiskRule.is_enabled == (1 if status == "enabled" else 0))
    rows = query.order_by(RiskRule.priority.desc(), RiskRule.rule_id).all()
    return ok([re_.rule_to_dict(r) for r in rows])


@router.get("/rules/scenes")
def list_scenes(_: RiskUser = Depends(RULE_READ_ROLES), db: Session = Depends(get_db)):
    """场景与阈值配置（API-3.7 / FR-40a）。"""
    rows = db.query(RiskRuleScene).order_by(RiskRuleScene.scene_code).all()
    return ok([{
        "scene_code": s.scene_code, "scene_name": s.scene_name, "biz_event": s.biz_event,
        "pass_threshold": s.pass_threshold, "mark_threshold": s.mark_threshold,
        "reject_threshold": s.reject_threshold, "multi_rule_bonus": s.multi_rule_bonus,
        "veto_min_score": s.veto_min_score, "enabled": bool(s.enabled),
    } for s in rows])


@router.put("/rules/scenes/{scene_code}")
def update_scene(scene_code: str, body: SceneThresholdUpdate,
                 user: RiskUser = Depends(ADMIN_ONLY), db: Session = Depends(get_db)):
    """修改阈值（API-3.8 / FR-40a）。

    ⚠ 按 PRD §2.1.1「修改风险分档阈值」**仅 admin** 可操作。
    校验：pass < mark < reject 且 veto_min_score >= reject_threshold。

    ⚠ **写入即全局同步**：页面七是「全局阈值」配置（不是按场景差异化），
    因此这里把新值同步写进**全部 7 条场景**。前端只需提交一次
    （`PUT /rules/scenes/all`）—— 遍历 7 个场景逐个调用会产生 7 条重复审计
    与 7 倍写放大（SPEC R-FE2-22）。
    """
    scene = db.query(RiskRuleScene).filter(RiskRuleScene.scene_code == scene_code).first()
    if scene is None:
        raise ApiError("3001", f"场景不存在：{scene_code}", 404)

    before = {"pass_threshold": scene.pass_threshold, "mark_threshold": scene.mark_threshold,
              "reject_threshold": scene.reject_threshold,
              "multi_rule_bonus": scene.multi_rule_bonus,
              "veto_min_score": scene.veto_min_score}

    new = {
        "pass_threshold": body.pass_threshold if body.pass_threshold is not None else scene.pass_threshold,
        "mark_threshold": body.mark_threshold if body.mark_threshold is not None else scene.mark_threshold,
        "reject_threshold": body.reject_threshold if body.reject_threshold is not None else scene.reject_threshold,
        "multi_rule_bonus": body.multi_rule_bonus if body.multi_rule_bonus is not None else scene.multi_rule_bonus,
        "veto_min_score": body.veto_min_score if body.veto_min_score is not None else scene.veto_min_score,
    }
    if not (new["pass_threshold"] < new["mark_threshold"] < new["reject_threshold"]):
        raise ApiError("3003", "阈值必须满足 Pass 上界 < Mark 上界 < Reject 下界", 400)
    if new["veto_min_score"] < new["reject_threshold"]:
        raise ApiError("3003", "一票否决最低分不得低于 Reject 下界", 400)

    # 全局阈值：同步其余场景（页面七为全局配置）
    for other in db.query(RiskRuleScene).all():
        other.pass_threshold = new["pass_threshold"]
        other.mark_threshold = new["mark_threshold"]
        other.reject_threshold = new["reject_threshold"]
        other.multi_rule_bonus = new["multi_rule_bonus"]
        other.veto_min_score = new["veto_min_score"]
    db.commit()

    audit_svc.write_audit(db, "system_config", f"scenes:{scene_code}", "update",
                          user.username, role=user.role, before=before,
                          after={**new, "synced_scenes": 7},
                          detail={"note": "全局阈值，已同步全部场景"})
    return ok({"scene_code": scene_code, **new, "synced_scenes": 7})


@router.get("/rules/{rule_id}")
def get_rule(rule_id: str, _: RiskUser = Depends(RULE_READ_ROLES),
             db: Session = Depends(get_db)):
    """规则详情（API-3.2）。"""
    if rule_id == "scenes":                      # 避免与 /rules/scenes 冲突
        raise ApiError("3001", "规则不存在：scenes", 404)
    rule = db.query(RiskRule).filter(RiskRule.rule_id == rule_id).first()
    if rule is None:
        raise ApiError("3001", f"规则不存在：{rule_id}", 404)
    return ok(re_.rule_to_dict(rule))


@router.post("/rules")
def create_rule(body: RuleCreate, user: RiskUser = Depends(STRATEGY_ROLES),
                db: Session = Depends(get_db)):
    """新建规则（API-3.3 / FR-32）。

    ⚠ **规则编码由服务端生成**（SPEC §3.8）：`RuleCreate` 不接收 `rule_id`，
    一律用 `next_rule_id()` 取「库中最大序号 + 1」。编码会被
    `risk_assessment.rule_results`、`risk_audit_log.biz_id` 与规则效果分析引用，
    允许客户端自定义会造成编码空洞与人工撞号。
    """
    valid, err = re_.validate_condition(body.rule_condition)
    if not valid:
        raise ApiError("3002", err, 400)

    rule_id = re_.next_rule_id(db)
    if db.query(RiskRule).filter(RiskRule.rule_id == rule_id).first():
        raise ApiError("3004", f"规则 ID 已存在：{rule_id}", 409)

    rule = RiskRule(
        rule_id=rule_id, rule_name=body.rule_name, rule_category=body.rule_category,
        event_type=body.event_type, rule_condition=body.rule_condition,
        risk_level=body.risk_level, risk_score=body.risk_score, action=body.action,
        challengeable=body.challengeable, priority=body.priority,
        is_enabled=1, version=1, description=body.description,
        updated_by=user.username, create_time=now_dt(), update_time=now_dt(),
    )
    db.add(rule)
    db.commit()
    re_.clear_rule_cache()                       # R-RE-05：即时生效

    audit_svc.write_audit(db, "rule_change", rule_id, "create", user.username,
                          role=user.role, after=re_.rule_to_dict(rule))
    return ok({"rule_id": rule.rule_id, "is_enabled": rule.is_enabled,
               "version": rule.version, "update_time": rule.update_time.isoformat()})


@router.put("/rules/{rule_id}")
def update_rule(rule_id: str, body: RuleUpdate,
                user: RiskUser = Depends(STRATEGY_ROLES), db: Session = Depends(get_db)):
    """编辑规则（部分更新，API-3.4 / FR-32、FR-34）。"""
    rule = db.query(RiskRule).filter(RiskRule.rule_id == rule_id).first()
    if rule is None:
        raise ApiError("3001", f"规则不存在：{rule_id}", 404)

    payload = body.model_dump(exclude_unset=True)
    if "rule_condition" in payload and payload["rule_condition"] is not None:
        valid, err = re_.validate_condition(payload["rule_condition"])
        if not valid:
            raise ApiError("3002", err, 400)

    before = re_.rule_to_dict(rule)
    for key, value in payload.items():
        if value is not None:
            setattr(rule, key, value)
    rule.version = int(rule.version or 1) + 1
    rule.updated_by = user.username
    rule.update_time = now_dt()
    db.commit()
    re_.clear_rule_cache()

    audit_svc.write_audit(db, "rule_change", rule_id, "update", user.username,
                          role=user.role, before=before, after=re_.rule_to_dict(rule))
    return ok({"rule_id": rule.rule_id, "is_enabled": rule.is_enabled,
               "version": rule.version, "update_time": rule.update_time.isoformat()})


@router.patch("/rules/{rule_id}/toggle")
@router.put("/rules/{rule_id}/toggle")
def toggle_rule(rule_id: str, user: RiskUser = Depends(STRATEGY_ROLES),
                db: Session = Depends(get_db)):
    """启停用切换（API-3.5 / FR-33，即时生效无需重启）。

    同时挂 PATCH 与 PUT —— SPEC 为 PATCH，旧契约前端用 PUT。
    """
    rule = db.query(RiskRule).filter(RiskRule.rule_id == rule_id).first()
    if rule is None:
        raise ApiError("3001", f"规则不存在：{rule_id}", 404)

    before = re_.rule_to_dict(rule)
    rule.is_enabled = 0 if rule.is_enabled == 1 else 1
    rule.version = int(rule.version or 1) + 1
    rule.updated_by = user.username
    rule.update_time = now_dt()
    db.commit()
    re_.clear_rule_cache()

    audit_svc.write_audit(db, "rule_change", rule_id, "toggle", user.username,
                          role=user.role, before=before, after=re_.rule_to_dict(rule))
    return ok({"rule_id": rule.rule_id, "is_enabled": rule.is_enabled,
               "status": "enabled" if rule.is_enabled == 1 else "disabled",
               "version": rule.version, "update_time": rule.update_time.isoformat()})


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: str, user: RiskUser = Depends(STRATEGY_ROLES),
                db: Session = Depends(get_db)):
    """删除规则（API-3.6 / FR-35，前端需二次确认）。"""
    rule = db.query(RiskRule).filter(RiskRule.rule_id == rule_id).first()
    if rule is None:
        raise ApiError("3001", f"规则不存在：{rule_id}", 404)

    before = re_.rule_to_dict(rule)
    db.delete(rule)
    db.commit()
    re_.clear_rule_cache()

    audit_svc.write_audit(db, "rule_change", rule_id, "delete", user.username,
                          role=user.role, before=before)
    return ok({"rule_id": rule_id, "deleted": True})


# ================================================================ 名单库（API-3.10 ~ 3.14）
@router.get("/blacklist")
def list_blacklist(list_type: str | None = None, entity_type: str | None = None,
                   keyword: str | None = None, status: str | None = None,
                   page: int = Query(default=1, ge=1),
                   page_size: int = Query(default=20, ge=1, le=200),
                   _: RiskUser = Depends(RULE_READ_ROLES),
                   db: Session = Depends(get_db)):
    """名单列表（FR-37~39）。"""
    return ok(case_svc.get_blacklist(db, list_type, entity_type, keyword, status,
                                     page, page_size))


@router.post("/blacklist")
def create_blacklist(body: BlacklistCreate, user: RiskUser = Depends(STRATEGY_ROLES),
                     db: Session = Depends(get_db)):
    """新增名单条目（API-3.11 / FR-37、FR-38）。"""
    row, err = case_svc.add_blacklist(db, body.list_type, body.entity_type,
                                      body.entity_value, body.reason,
                                      body.expire_time, user.username)
    if err:
        raise ApiError("3004", "名单条目重复（命中 UNIQUE 约束）", 409)

    audit_svc.write_audit(db, "blacklist_change", str(row["blacklist_id"]), "create",
                          user.username, role=user.role, after=row)
    return ok(row)


@router.delete("/blacklist/{blacklist_id}")
def delete_blacklist(blacklist_id: int, user: RiskUser = Depends(STRATEGY_ROLES),
                     db: Session = Depends(get_db)):
    """移除名单条目（API-3.12）。"""
    data, err = case_svc.remove_blacklist(db, blacklist_id)
    if err:
        raise ApiError("3001", f"名单条目不存在：{blacklist_id}", 404)

    audit_svc.write_audit(db, "blacklist_change", str(blacklist_id), "delete",
                          user.username, role=user.role, before=data.pop("_before", None))
    return ok(data)


@router.post("/blacklist/import")
def import_blacklist(body: BlacklistImport, user: RiskUser = Depends(STRATEGY_ROLES),
                     db: Session = Depends(get_db)):
    """批量导入（API-3.13 / FR-39）。返回成功 / 失败行与原因。"""
    result = case_svc.import_blacklist(db, body.list_type, body.entity_type,
                                       body.items, user.username)
    audit_svc.write_audit(db, "blacklist_change", f"import:{body.entity_type}", "import",
                          user.username, role=user.role,
                          after={"list_type": body.list_type, "entity_type": body.entity_type,
                                 "success": result["success"], "failed": result["failed"]})
    return ok(result)


@router.get("/blacklist/stats")
def blacklist_stats(_: RiskUser = Depends(RULE_READ_ROLES), db: Session = Depends(get_db)):
    """名单命中统计（API-3.14 / FR-40）。"""
    return ok(case_svc.get_blacklist_stats(db))


# ================================================================ 二次验证（API-3.15 / 3.16）
@router.post("/challenge/{event_id}/resolve")
def resolve_challenge(event_id: str, body: ChallengeResolve,
                      user: RiskUser = Depends(STRATEGY_ROLES),
                      db: Session = Depends(get_db)):
    """二次验证结果回调（FR-19）。

    - 通过            → 决策升级 Pass，不建案（R-RE-14）
    - 失败/超时/拒绝   → 决策升级 Review 并创建案件（幂等，R-RE-18）
    """
    data, err = challenge_svc.resolve_challenge(db, event_id, body.result, user.username)
    if err:
        raise ApiError(err, "挑战事件不存在或已结束（状态非 pending）", 400)

    # biz_type 用 'challenge'（PRD Q14 已登记该取值，审计页筛选项与之对应）
    audit_svc.write_audit(db, "challenge", event_id, "resolve", user.username,
                          role=user.role, before={"challenge_status": "pending"},
                          after={"result": body.result, "decision": data["decision"],
                                 "case_id": data["case_id"]})
    return ok(data)


@router.get("/challenge/{event_id}")
def get_challenge(event_id: str, _: RiskUser = Depends(AGENT_ROLES),
                  db: Session = Depends(get_db)):
    """查询某事件的二次验证记录（API-3.16，仿真页展示用）。"""
    return ok(challenge_svc.get_challenge(db, event_id))
