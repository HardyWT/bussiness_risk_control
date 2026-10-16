"""模块 4：案件流转 —— 案件 CRUD、状态机、处置动作、名单库、用户画像。

对应文档：
- Demo Doc-03 §4.2  案件管理（9 个函数）
- SPEC §4.4 ~ §4.7  状态机 / 处置动作映射 / 业务规则 R-CF-01 ~ R-CF-08
- SPEC §5.5         用户画像查询（含降级）
"""

import logging
from datetime import datetime

from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from ..config import CASE_STATUSES
from ..models import (RiskAssessment, RiskBlacklist, RiskCase,
                      RiskDisposalRecord, RiskEvent, RiskFeature, RiskUserProfile)
from ..utils import (case_no_label, from_json, mask_phone, new_id, now_dt,
                     parse_iso, round4, to_float)

log = logging.getLogger(__name__)

# 状态机（SPEC §4.4）
ALLOWED_TRANSITIONS = {
    "待审核": {"审核中", "已关闭"},
    "审核中": {"待审核", "已处置"},
    "已处置": {"已归档"},
    "已关闭": set(),
    "已归档": set(),
}

# 处置结论 → 自动执行动作（SPEC §4.5）
DISPOSAL_ACTIONS = {
    "pass": [],
    "reject": ["block_order"],
    "blacklist": ["block_order", "blacklist_user", "ban_device"],
}

# 允许被 `actions` 参数显式指定的动作白名单（防止拼错的动作名被静默接受）
ALLOWED_ACTIONS = {"block_order", "blacklist_user", "ban_device", "reject_refund", "pass"}

# 可取消的订单状态（R-CF-09）。取值取自业务库 `order_status` 表的 7 种状态，
# 只有**履约尚未推进**（未发货）的订单才能被风控拦截回改。
# 「已发货 / 已签收 / 已完成 / 退款中」不回改 —— 货已出门，回改状态会让
# 库存、物流与对账数据自相矛盾。
CANCELLABLE_ORDER_STATUSES = {"待付款", "已支付"}

# 筛选参数别名：前端下拉框 / API 客户端可用英文枚举，统一归一化为库内中文口径
STATUS_ALIAS = {"pending": "待审核", "reviewing": "审核中", "disposed": "已处置",
                "closed": "已关闭", "archived": "已归档"}
LEVEL_ALIAS = {"critical": "极高", "high": "高", "medium": "中", "low": "低"}


def _norm_filter(value: str | None, alias: dict) -> str | None:
    """筛选值归一化：中英文枚举均可命中；未知值原样返回（由 DB 决定空结果）。"""
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    return alias.get(v.lower(), v)


# ============================================================================
# 案件查询
# ============================================================================
def get_case_list(db: Session, case_status: str | None = None,
                  case_category: str | None = None, risk_level: str | None = None,
                  event_type: str | None = None, start_time: str | None = None,
                  end_time: str | None = None, keyword: str | None = None,
                  page: int = 1, page_size: int = 20) -> dict:
    """案件列表 + 复合筛选 + 分页（FR-23）。筛选值支持中英文枚举。"""
    query = db.query(RiskCase)
    status_norm = _norm_filter(case_status, STATUS_ALIAS)
    level_norm = _norm_filter(risk_level, LEVEL_ALIAS)
    if status_norm:
        query = query.filter(RiskCase.case_status == status_norm)
    if case_category:
        query = query.filter(RiskCase.case_category == str(case_category).strip())
    if keyword:
        # 关键字既能是 ULID（`cas01m...`）也能是短号（`C-1024` / `1024`）
        kw = str(keyword).strip()
        conds = [RiskCase.case_id.like(f"%{kw}%"), RiskCase.user_id.like(f"%{kw}%")]
        digits = "".join(ch for ch in kw if ch.isdigit())
        if digits and kw[:1].upper() == "C":
            conds.append(RiskCase.case_no == int(digits))
        elif digits.isdigit() and len(digits) <= 6:
            conds.append(RiskCase.case_no == int(digits))
        query = query.filter(or_(*conds))
    if level_norm:
        query = query.filter(RiskCase.risk_level == level_norm)
    if start_time:
        query = query.filter(RiskCase.create_time >= (parse_iso(start_time) or datetime.min))
    if end_time:
        query = query.filter(RiskCase.create_time <= (parse_iso(end_time) or datetime.max))
    if event_type:
        # 直接按 event_id 子查询过滤即可。v3.0 在这里套了两层子查询
        # （把 IN 的结果再 subquery() 一次），生成的 SQL 多一层派生表却没有语义差异。
        event_ids = db.query(RiskEvent.event_id).filter(RiskEvent.event_type == event_type)
        query = query.filter(RiskCase.event_id.in_(event_ids))

    total = query.count()
    rows = (query.order_by(RiskCase.create_time.desc())
            .offset(max(page - 1, 0) * page_size).limit(page_size).all())

    stats = get_case_statistics(db)
    return {
        "total": total,                                   # 当前筛选条件下的数量（分页用）
        "page": page,
        "page_size": page_size,
        # statistics 为**全量**口径（顶部统计卡）：total + 五态计数
        "statistics": {"total": stats["total"], **stats["by_status"]},
        "by_category": stats["by_category"],              # 全量分类分布，供筛选项动态生成
        "items": _attach_top_rule(db, rows),
    }


def get_case_statistics(db: Session) -> dict:
    """案件统计（FR-24）。**每种状态补零**，保证返回固定 5 个键（R-CF-08）。"""
    total = db.query(func.count(RiskCase.case_id)).scalar() or 0
    status_rows = (db.query(RiskCase.case_status, func.count(RiskCase.case_id))
                   .group_by(RiskCase.case_status).all())
    category_rows = (db.query(RiskCase.case_category, func.count(RiskCase.case_id))
                     .group_by(RiskCase.case_category).all())

    by_status = {s: 0 for s in CASE_STATUSES}
    for status, count in status_rows:
        by_status[status or "未知"] = count
    by_category = {(c or "未知"): n for c, n in category_rows}

    return {"total": total, "by_status": by_status, "by_category": by_category}


def _case_brief(case: RiskCase) -> dict:
    """案件列表项（含关联评估的分值与命中规则摘要）。"""
    return {
        "case_id": case.case_id,
        "case_no": case.case_no,
        "case_no_label": case_no_label(case.case_no, case.case_id),
        "event_id": case.event_id,
        "user_id": case.user_id,
        "case_status": case.case_status,
        "case_category": case.case_category,
        "final_score": case.final_score,
        "risk_level": case.risk_level,
        "assignee": case.assignee,
        "conclusion": case.conclusion,
        "top_rule_id": None,             # 由 _attach_top_rule 批量回填（最高分命中规则）
        "create_time": case.create_time.isoformat() if case.create_time else None,
        "disposed_at": case.disposed_at.isoformat() if case.disposed_at else None,
    }


def _attach_top_rule(db: Session, rows: list[RiskCase]) -> list[dict]:
    """批量回填列表项的最高分命中规则号（单次查询，避免逐行 N+1）。"""
    briefs = [_case_brief(r) for r in rows]
    ids = [r.assessment_id for r in rows if r.assessment_id]
    if not ids:
        return briefs

    top_map: dict[str, str] = {}
    for aid, rule_results in (db.query(RiskAssessment.assessment_id,
                                       RiskAssessment.rule_results)
                              .filter(RiskAssessment.assessment_id.in_(ids)).all()):
        best_id, best_score = None, None
        for hit in (from_json(rule_results) or []):
            if not isinstance(hit, dict) or not hit.get("rule_id"):
                continue
            score = to_float(hit.get("risk_score"))
            if best_score is None or score > best_score:
                best_id, best_score = hit["rule_id"], score
        if best_id:
            top_map[aid] = best_id

    for brief, row in zip(briefs, rows):
        brief["top_rule_id"] = top_map.get(row.assessment_id)
    return briefs


# ============================================================================
# 案件详情（读操作联动写状态，R-CF-02）
# ============================================================================
def get_case_detail(db: Session, case_id: str) -> tuple[dict | None, str | None]:
    """案件详情聚合：案件 + 评估 + 命中规则 + 画像 + 特征快照 + 图谱。"""
    case = db.query(RiskCase).filter(RiskCase.case_id == case_id).first()
    if case is None:
        return None, "2001"

    # R-CF-02：首次读取「待审核」案件时联动置为「审核中」
    if case.case_status == "待审核":
        case.case_status = "审核中"
        db.commit()

    assessment = (db.query(RiskAssessment)
                  .filter(RiskAssessment.assessment_id == case.assessment_id).first())
    event = db.query(RiskEvent).filter(RiskEvent.event_id == case.event_id).first()

    features, baseline = {}, {}
    if event is not None:
        for f in db.query(RiskFeature).filter(RiskFeature.event_id == case.event_id).all():
            features[f.feature_name] = to_float(f.feature_value)
            baseline[f.feature_name] = to_float(f.baseline_value)

    profile = get_user_profile(db, case.user_id)

    order = None
    if event is not None and event.source_id:
        row = db.execute(text("""
            SELECT oi.order_id, oi.order_status, oi.receive_id, oi.create_time,
                   COALESCE(SUM(od.final_amount), 0) AS total_amount,
                   MIN(od.sku_name) AS item_name
            FROM order_info oi LEFT JOIN order_detail od ON oi.order_id = od.order_id
            WHERE oi.order_id = :oid GROUP BY oi.order_id
        """), {"oid": event.source_id}).first()
        if row:
            order = {
                "order_id": row.order_id,
                "order_status": row.order_status,
                "total_amount": to_float(row.total_amount),
                "item_name": row.item_name or "—",
                "receive_id": row.receive_id,
            }

    from . import step5_graph as graph

    return {
        "case_id": case.case_id,
        "case_no": case.case_no,
        "case_no_label": case_no_label(case.case_no, case.case_id),
        "assessment_id": case.assessment_id,
        "event_id": case.event_id,
        "user_id": case.user_id,
        "case_status": case.case_status,
        "case_category": case.case_category,
        "final_score": case.final_score,
        "risk_level": case.risk_level,
        "decision": assessment.decision if assessment else None,
        "is_veto": bool(assessment.is_veto) if assessment else False,
        "hit_source": assessment.hit_source if assessment else None,
        "assignee": case.assignee,
        "conclusion": case.conclusion,
        "remark": case.remark,
        "create_time": case.create_time.isoformat() if case.create_time else None,
        "disposed_at": case.disposed_at.isoformat() if case.disposed_at else None,
        "profile": profile,
        "order": order,
        "features": features,
        "feature_baseline": baseline,
        "triggered_rules": (from_json(assessment.rule_results) or []) if assessment else [],
        "graph": graph.get_graph(db, case.user_id),
        "disposals": [
            {"action_type": d.action_type, "target_id": d.target_id,
             "result": d.result, "operator": d.operator,
             "create_time": d.create_time.isoformat() if d.create_time else None}
            for d in db.query(RiskDisposalRecord)
            .filter(RiskDisposalRecord.case_id == case_id)
            .order_by(RiskDisposalRecord.create_time).all()
        ],
    }, None


# ============================================================================
# 状态流转
# ============================================================================
def _check_transition(case: RiskCase, target: str) -> bool:
    return target in ALLOWED_TRANSITIONS.get(case.case_status, set())


def archive_case(db: Session, case_id: str, operator: str = "system") -> tuple[dict | None, str | None]:
    """把「已处置」案件转入终态「已归档」（SPEC R-CF-03 / PRD G-12）。

    ⚠ 为什么必须真的执行：状态机声明了 `已处置 → 已归档` 这条边，
    但 v3.0 没有任何代码触发它 —— 案件永久停在「已处置」，
    「已归档 4」这类统计恒为 0，终态覆盖率永远不达标。
    由处置接口的 `BackgroundTasks` 在响应返回后调用。

    幂等：已是「已归档」直接返回成功（重复归档不报错）；
    仍在「审核中 / 待审核」说明处置事务未提交成功，不越级归档。
    """
    case = db.query(RiskCase).filter(RiskCase.case_id == case_id).first()
    if case is None:
        return None, "2001"
    if case.case_status == "已归档":
        return {"case_id": case_id, "case_status": "已归档", "already": True}, None
    if not _check_transition(case, "已归档"):
        return None, "2003"

    before = {"case_status": case.case_status}
    case.case_status = "已归档"
    db.commit()
    return {"case_id": case_id, "case_status": "已归档", "already": False,
            "_before": before, "_after": {"case_status": "已归档"}}, None


def hold_case(db: Session, case_id: str, operator: str) -> tuple[dict | None, str | None]:
    """暂存挂起：审核中 → 待审核（API-4.5）。"""
    case = db.query(RiskCase).filter(RiskCase.case_id == case_id).first()
    if case is None:
        return None, "2001"
    if not _check_transition(case, "待审核"):
        return None, "2003"
    before = {"case_status": case.case_status}
    case.case_status = "待审核"
    case.assignee = operator
    db.commit()
    return {"case_id": case_id, "case_status": case.case_status,
            "_before": before, "_after": {"case_status": case.case_status}}, None


def close_case(db: Session, case_id: str, remark: str | None,
               operator: str) -> tuple[dict | None, str | None]:
    """关闭为误报：待审核 → 已关闭（不处置）。"""
    case = db.query(RiskCase).filter(RiskCase.case_id == case_id).first()
    if case is None:
        return None, "2001"
    if not _check_transition(case, "已关闭"):
        return None, "2003"
    before = {"case_status": case.case_status}
    case.case_status = "已关闭"
    case.assignee = operator
    case.remark = remark
    db.commit()
    return {"case_id": case_id, "case_status": case.case_status,
            "_before": before, "_after": {"case_status": case.case_status,
                                          "remark": remark}}, None


def dispose_case(db: Session, case_id: str, conclusion: str, remark: str | None,
                 operator: str, actions: list[str] | None = None) -> tuple[dict | None, str | None]:
    """提交处置结论并执行联动动作（API-4.4 / R-CF-03 ~ R-CF-07）。"""
    case = db.query(RiskCase).filter(RiskCase.case_id == case_id).first()
    if case is None:
        return None, "2001"
    if conclusion not in DISPOSAL_ACTIONS:
        return None, "2002"
    if not _check_transition(case, "已处置"):
        return None, "2003"          # R-CF-06：已处置/已归档/已关闭重复提交

    event = db.query(RiskEvent).filter(RiskEvent.event_id == case.event_id).first()
    now = now_dt()
    # 显式传入的 actions 需过滤到白名单内：拼错的动作名会被 `_exec_action`
    # 静默返回「未知动作」，处置看起来成功但什么也没做。
    action_list = [a for a in (actions or DISPOSAL_ACTIONS[conclusion])
                   if a in ALLOWED_ACTIONS]
    before = {"case_status": case.case_status, "conclusion": case.conclusion,
              "remark": case.remark}

    results: list[dict] = []
    for action in action_list:
        try:
            target_id, detail = _exec_action(db, action, case, event, operator, now)
            results.append({"action_type": action, "target_id": target_id,
                            "result": "success", "detail": detail})
        except Exception as exc:  # noqa: BLE001  R-CF-04：部分失败不阻断
            log.warning("处置动作 %s 执行失败：%s", action, exc)
            results.append({"action_type": action, "target_id": "-",
                            "result": "failed", "detail": str(exc)})

        db.add(RiskDisposalRecord(
            disposal_id=new_id("dsp_"),
            case_id=case.case_id,
            action_type=action,
            target_id=results[-1]["target_id"],
            operator=operator,                    # R-AU-08：取真实登录用户
            result=results[-1]["result"],
            detail=results[-1].get("detail"),
            create_time=now,
        ))

    case.case_status = "已处置"
    case.conclusion = {"pass": "放行", "reject": "拦截", "blacklist": "拉黑"}[conclusion]
    case.remark = remark
    case.assignee = operator
    case.disposed_at = now
    db.commit()

    failed = [r for r in results if r["result"] == "failed"]
    return {
        "case_id": case.case_id,
        "case_status": case.case_status,
        "conclusion": case.conclusion,
        "disposals": results,
        "disposed_at": now.isoformat(),
        "partial_failure": bool(failed),
        "_before": before,
        "_after": {"case_status": case.case_status, "conclusion": case.conclusion,
                   "remark": remark},
    }, None


def _exec_action(db: Session, action: str, case: RiskCase, event: RiskEvent | None,
                 operator: str, now) -> tuple[str, dict]:
    """执行单个联动动作，返回 (目标对象编号, 详情)。"""
    order_id = event.source_id if event else None

    if action == "block_order":
        if not order_id:
            return "-", {"note": "无关联订单"}
        # R-CF-09：只对**可取消**的订单生效。订单可能已经发货甚至签收，
        # 此时把状态回改成「已取消」会破坏真实履约数据（库存、物流、对账全乱）。
        # 只有「未支付 / 已下单未发货」这类状态才可取消。
        current = db.execute(text(
            "SELECT order_status FROM order_info WHERE order_id = :oid"
        ), {"oid": order_id}).first()
        if current is None:
            return "-", {"note": "订单不存在", "skipped": True}
        status = current.order_status
        if status not in CANCELLABLE_ORDER_STATUSES:
            log.info("订单 %s 当前状态为 %s，不满足可取消条件，跳过拦截动作", order_id, status)
            return order_id, {"skipped": True, "reason": "订单状态不可取消",
                              "current_status": status,
                              "cancellable_statuses": sorted(CANCELLABLE_ORDER_STATUSES)}

        row = db.execute(text(
            "UPDATE order_info SET order_status = '已取消' WHERE order_id = :oid"
        ), {"oid": order_id})
        # 同步拒绝关联售后（SPEC §4.5：售后申请标记拒绝）
        db.execute(text("""
            UPDATE postsale p JOIN order_detail od ON p.order_detail_id = od.order_detail_id
            SET p.postsale_status = '已拒绝'
            WHERE od.order_id = :oid AND p.postsale_status NOT IN ('已完成', '已拒绝')
        """), {"oid": order_id})
        return order_id, {"affected_rows": row.rowcount, "new_status": "已取消",
                          "skipped": False}

    if action == "blacklist_user":
        # R-CF-10：已在黑名单中视为幂等成功，不报 failed
        created = _add_blacklist(db, "black", "user", case.user_id,
                                 f"案件 {case.case_id} 违规拦截并拉黑", operator, now)
        return case.user_id, {"entity_type": "user", "already_exists": not created}

    if action == "ban_device":
        device = event.device_id if event else None
        if not device:
            return "-", {"note": "事件无设备指纹", "skipped": True}
        # R-CF-07：拉黑同时写 user 与 device 两类
        created = _add_blacklist(db, "black", "device", device,
                                 f"案件 {case.case_id} 封禁设备", operator, now)
        return device, {"entity_type": "device", "already_exists": not created}

    if action == "reject_refund":
        if not order_id:
            return "-", {"note": "无关联售后单"}
        row = db.execute(text("""
            UPDATE postsale p JOIN order_detail od ON p.order_detail_id = od.order_detail_id
            SET p.postsale_status = '已拒绝' WHERE od.order_id = :oid
        """), {"oid": order_id})
        return order_id, {"affected_rows": row.rowcount, "new_status": "已拒绝"}

    if action == "pass":
        return case.case_id, {"note": "放行，标记误报"}

    return "-", {"note": f"未知动作 {action}"}


def _add_blacklist(db: Session, list_type: str, entity_type: str, value: str,
                   reason: str, operator: str, now) -> bool:
    """处置动作里的名单写入（幂等）。

    返回 `True` 表示**新写入**，`False` 表示该主体已在名单中。
    调用方据返回值在 `detail.already_exists` 里标注，而不是报 `failed`
    —— 「本条无新动作」与「执行出错」是两件完全不同的事（R-CF-10）。
    """
    exists = (db.query(RiskBlacklist)
              .filter(RiskBlacklist.list_type == list_type)
              .filter(RiskBlacklist.entity_type == entity_type)
              .filter(RiskBlacklist.entity_value == str(value)).first())
    if exists:
        return False
    db.add(RiskBlacklist(
        list_type=list_type, entity_type=entity_type, entity_value=str(value),
        reason=reason, operator=operator, expire_time=None, hit_count=0,
        create_time=now,
    ))
    return True


# ============================================================================
# 名单库 CRUD（API-3.10 ~ 3.14）
# ============================================================================
def get_blacklist(db: Session, list_type: str | None = None, entity_type: str | None = None,
                  keyword: str | None = None, status: str | None = None,
                  page: int = 1, page_size: int = 20) -> dict:
    """名单列表 + 各类型数量角标 + 过期标注（FR-37~39）。"""
    query = db.query(RiskBlacklist)
    if list_type:
        query = query.filter(RiskBlacklist.list_type == list_type)
    if entity_type:
        query = query.filter(RiskBlacklist.entity_type == entity_type)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(or_(RiskBlacklist.entity_value.like(like),
                                 RiskBlacklist.reason.like(like)))

    now = now_dt()
    if status == "expired":
        query = query.filter(RiskBlacklist.expire_time.isnot(None),
                             RiskBlacklist.expire_time < now)
    elif status == "active":
        query = query.filter(or_(RiskBlacklist.expire_time.is_(None),
                                 RiskBlacklist.expire_time >= now))

    total = query.count()
    rows = (query.order_by(RiskBlacklist.create_time.desc())
            .offset(max(page - 1, 0) * page_size).limit(page_size).all())

    counts = {t: db.query(func.count(RiskBlacklist.blacklist_id))
              .filter(RiskBlacklist.list_type == t).scalar() or 0
              for t in ("black", "white", "gray")}

    return {
        "total": total, "page": page, "page_size": page_size, "counts": counts,
        "items": [_blacklist_to_dict(r, now) for r in rows],
    }


def _blacklist_to_dict(row: RiskBlacklist, now) -> dict:
    expired = bool(row.expire_time and row.expire_time < now)
    return {
        "blacklist_id": row.blacklist_id,
        "list_type": row.list_type,
        "entity_type": row.entity_type,
        "entity_value": mask_phone(row.entity_value) if row.entity_type == "phone" else row.entity_value,
        "reason": row.reason,
        "operator": row.operator,
        "expire_time": row.expire_time.isoformat() if row.expire_time else None,
        "expired": expired,
        "hit_count": row.hit_count,
        "create_time": row.create_time.isoformat() if row.create_time else None,
    }


def add_blacklist(db: Session, list_type: str, entity_type: str, entity_value: str,
                  reason: str | None, expire_time: str | None,
                  operator: str) -> tuple[dict | None, str | None]:
    """新增名单条目（FR-37/38）。命中 UNIQUE 约束返回 3004。"""
    expires = parse_iso(expire_time)
    exists = (db.query(RiskBlacklist)
              .filter(RiskBlacklist.list_type == list_type)
              .filter(RiskBlacklist.entity_type == entity_type)
              .filter(RiskBlacklist.entity_value == str(entity_value)).first())
    if exists:
        return None, "3004"

    row = RiskBlacklist(
        list_type=list_type, entity_type=entity_type, entity_value=str(entity_value),
        reason=reason, operator=operator, expire_time=expires, hit_count=0,
        create_time=now_dt(),
    )
    db.add(row)
    db.commit()
    return _blacklist_to_dict(row, now_dt()), None


def remove_blacklist(db: Session, blacklist_id: int) -> tuple[dict | None, str | None]:
    row = db.query(RiskBlacklist).filter(RiskBlacklist.blacklist_id == blacklist_id).first()
    if row is None:
        return None, "3001"
    before = _blacklist_to_dict(row, now_dt())
    db.delete(row)
    db.commit()
    return {"blacklist_id": blacklist_id, "deleted": True, "_before": before}, None


def import_blacklist(db: Session, list_type: str, entity_type: str,
                     items: list, operator: str) -> dict:
    """批量导入（FR-39）：逐行预校验，返回成功 / 失败行与原因。"""
    success, failed = 0, []
    for idx, item in enumerate(items, start=1):
        value = (getattr(item, "entity_value", "") or "").strip()
        if not value:
            failed.append({"row": idx, "value": getattr(item, "entity_value", ""),
                           "reason": "名单值为空"})
            continue
        if entity_type == "phone" and not (value.isdigit() and len(value) == 11):
            failed.append({"row": idx, "value": value,
                           "reason": "手机号格式非法（应为 11 位数字）"})
            continue
        exists = (db.query(RiskBlacklist)
                  .filter(RiskBlacklist.list_type == list_type)
                  .filter(RiskBlacklist.entity_type == entity_type)
                  .filter(RiskBlacklist.entity_value == value).first())
        if exists:
            failed.append({"row": idx, "value": value, "reason": "已存在（重复）"})
            continue
        db.add(RiskBlacklist(
            list_type=list_type, entity_type=entity_type, entity_value=value,
            reason=getattr(item, "reason", None) or "批量导入",
            operator=operator, expire_time=None, hit_count=0, create_time=now_dt(),
        ))
        success += 1
    db.commit()
    return {"success": success, "failed": len(failed), "total": len(items), "errors": failed}


def get_blacklist_stats(db: Session, top: int = 20) -> dict:
    """名单命中统计（FR-40）。"""
    rows = (db.query(RiskBlacklist)
            .order_by(RiskBlacklist.hit_count.desc()).limit(top).all())
    return {
        "total": len(rows), "page": 1, "page_size": top,
        "items": [{"blacklist_id": r.blacklist_id, "list_type": r.list_type,
                   "entity_type": r.entity_type,
                   "entity_value": mask_phone(r.entity_value) if r.entity_type == "phone" else r.entity_value,
                   "hit_count": r.hit_count} for r in rows],
    }


# ============================================================================
# 用户画像（API-5.1，含降级）
# ============================================================================
def get_user_profile(db: Session, user_id: str) -> dict | None:
    """用户风险画像查询；无画像时**降级为实时计算**（Demo Doc-03 §5.1.4）。"""
    profile = db.query(RiskUserProfile).filter(RiskUserProfile.user_id == user_id).first()
    if profile is None:
        return _compute_profile_on_the_fly(db, user_id)

    return {
        "user_id": profile.user_id,
        "has_profile": True,
        "risk_score": profile.risk_score,
        "risk_level": profile.risk_level,
        "total_orders": profile.total_orders,
        "total_amount": to_float(profile.total_amount),
        "total_refunds": profile.total_refunds,
        "refund_rate": to_float(profile.refund_rate),
        "avg_order_amount": to_float(profile.avg_order_amount),
        "address_count": profile.address_count,
        "complaint_count": profile.complaint_count,
        "assessment_count": profile.assessment_count,
        "risk_tags": from_json(profile.risk_tags) or [],
        "last_assessment_time": profile.last_assessment_time.isoformat() if profile.last_assessment_time else None,
    }


def _compute_profile_on_the_fly(db: Session, user_id: str) -> dict:
    """无画像时实时计算用户维度特征作为降级画像。"""
    from ..engine import step2_feature as fe

    try:
        features = fe.compute_user_features(db, user_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("用户 %s 画像降级计算失败：%s", user_id, exc)
        features = {}

    return {
        "user_id": user_id,
        "has_profile": False,
        "risk_score": 0,
        "risk_level": "低",
        "total_orders": int(features.get("user_total_orders", 0)),
        "total_amount": to_float(features.get("user_total_amount")),
        "total_refunds": int(features.get("user_refund_count", 0)),
        "refund_rate": round4(features.get("user_refund_rate")),
        "avg_order_amount": to_float(features.get("user_avg_order_amount")),
        "address_count": int(features.get("user_address_count", 0)),
        "complaint_count": int(features.get("user_complaint_count", 0)),
        "assessment_count": 0,
        "risk_tags": [],
        "last_assessment_time": None,
        "computed_features": features,
    }
