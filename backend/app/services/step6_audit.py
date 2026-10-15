"""模块 6：监控与审计 —— 审计写入/查询、大盘指标、规则效果、系统健康度。

对应文档：
- SPEC §6.3  risk_audit_log DDL（触发器不可篡改）
- SPEC §6.4  API-6.1 ~ API-6.11
- SPEC §6.5  R-MA-01 ~ R-MA-10
- PRD §2.1.1 审计数据范围（管理员全量 / 其余仅本人 / 分析师无权）
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (RiskAssessment, RiskAuditLog, RiskCase, RiskEvent, RiskRule,
                      RiskUser)
from ..utils import from_json, new_id, now_dt, to_float

log = logging.getLogger(__name__)

# 业务表清单（用于 /system/stats）
BUSINESS_TABLES = [
    "user_info", "region", "receive_info", "product_category", "sku_info",
    "order_status", "order_info", "order_detail", "logistics", "order_logistics",
    "logistics_company", "logistics_complaint", "logistics_complaints_record",
    "postsale_status", "postsale_reason", "postsale", "postsale_logistics",
]
RISK_TABLES = [
    "risk_rule", "risk_rule_scene", "risk_event", "risk_feature",
    "risk_assessment", "risk_case", "risk_disposal_record", "risk_blacklist",
    "risk_user_profile", "risk_entity", "risk_entity_relation",
    "risk_challenge_record", "risk_audit_log", "risk_user",
    "risk_agent_session", "risk_agent_message",
]


# ============================================================================
# 审计写入（R-MA-02）
# ============================================================================
def write_audit(db: Session, biz_type: str, biz_id: str, action: str, operator: str,
                before=None, after=None, detail=None, role: str | None = None,
                commit: bool = True) -> RiskAuditLog:
    """所有写操作必须写审计（R-MA-02）。

    operator 一律取真实登录用户（R-AU-08），禁止由前端传入。
    before/after/detail 存 JSON，便于 diff 展示（R-MA-04）。
    """
    row = RiskAuditLog(
        log_id=new_id("log_"),
        biz_type=biz_type,
        biz_id=biz_id,
        action=action,
        operator=operator,
        before_value=before,
        after_value=after,
        detail=detail,
        role=role,
        create_time=now_dt(),
    )
    db.add(row)
    if commit:
        db.commit()
    return row


# ============================================================================
# 审计查询（API-6.9 / R-MA-10）
# ============================================================================
def list_audit(db: Session, current_user: RiskUser, biz_type: str | None = None,
               operator: str | None = None, start_time: str | None = None,
               end_time: str | None = None, page: int = 1, page_size: int = 20) -> dict:
    """审计流水查询。

    数据范围：admin → 全量；reviewer / strategist / ops → **强制附加**
    `operator = current_user.username`，前端传的 operator 参数被忽略。
    """
    from ..utils import parse_iso

    query = db.query(RiskAuditLog)
    if current_user.role == "admin":
        if operator:
            query = query.filter(RiskAuditLog.operator == operator)
        scope = "all"
    else:
        query = query.filter(RiskAuditLog.operator == current_user.username)
        scope = "self"

    if biz_type:
        query = query.filter(RiskAuditLog.biz_type == biz_type)
    if start_time:
        query = query.filter(RiskAuditLog.create_time >= (parse_iso(start_time) or datetime.min))
    if end_time:
        query = query.filter(RiskAuditLog.create_time <= (parse_iso(end_time) or datetime.max))

    total = query.count()
    rows = (query.order_by(RiskAuditLog.create_time.desc())
            .offset(max(page - 1, 0) * page_size).limit(page_size).all())

    return {
        "total": total, "page": page, "page_size": page_size, "scope": scope,
        "items": [{
            "log_id": r.log_id,
            "biz_type": r.biz_type,
            "biz_id": r.biz_id,
            "action": r.action,
            "operator": r.operator,
            "role": r.role,
            "before_value": from_json(r.before_value),
            "after_value": from_json(r.after_value),
            "detail": from_json(r.detail),
            "create_time": r.create_time.isoformat() if r.create_time else None,
        } for r in rows],
    }


# ============================================================================
# 大盘（API-6.1 ~ 6.7）
# ============================================================================
def _decisions_between(db: Session, start: datetime | None = None,
                       end: datetime | None = None) -> dict:
    """按 `decision` 分组计数（**SQL 聚合**，不把评估表读进内存）。

    R-MA-07：v3.0 的写法是 `db.query(RiskAssessment).all()` 后 Python 遍历，
    而大盘刷新一次要调用它 3~4 次（今日 / 昨日 / 全量 / 风险分布），
    等于每次刷新把整表读 4 遍。回放 1200 条事件即 4 万行 × 4。

    返回固定 5 个键（含 0 值）+ `total`。
    """
    counter = {"Pass": 0, "Mark": 0, "Challenge": 0, "Review": 0, "Reject": 0}
    query = db.query(RiskAssessment.decision, func.count(RiskAssessment.assessment_id))
    if start is not None:
        query = query.filter(RiskAssessment.create_time >= start)
    if end is not None:
        query = query.filter(RiskAssessment.create_time < end)
    total = 0
    for decision, count in query.group_by(RiskAssessment.decision).all():
        total += int(count or 0)
        if decision in counter:
            counter[decision] = int(count or 0)
    counter["total"] = total
    return counter


def _reject_amount_between(db: Session, start: datetime | None,
                           end: datetime | None = None) -> float:
    """窗口内判定为 Reject 的 `order_create` / `order_pay` 事件金额合计。"""
    query = (db.query(func.coalesce(func.sum(RiskEvent.amount), 0))
             .join(RiskAssessment, RiskAssessment.event_id == RiskEvent.event_id)
             .filter(RiskAssessment.decision == "Reject")
             .filter(RiskEvent.event_type.in_(["order_create", "order_pay"])))
    if start is not None:
        query = query.filter(RiskAssessment.create_time >= start)
    if end is not None:
        query = query.filter(RiskAssessment.create_time < end)
    return to_float(query.scalar())


def dashboard_overview(db: Session) -> dict:
    """核心指标卡（API-6.1 / FR-41）。

    ⚠ **口径必须自洽**（R-MA-11）：同一张卡上的「值」与「变化量」同窗口。
    v3.0 的写法把「今日评估量」与「全量高风险数 / 全量通过率」并列，
    还把 `pass_rate_delta` 写死 0.0、`loss_delta` 写成 `loss × 0.14` 的估算常量 ——
    页面上每个数字都好看，但互相之间没有关系。

    本版口径：
    - 今日类（`assess_*` / `high_risk_*` / `pass_rate*` / `case_delta`）→ 今日 vs 昨日
    - 累计类（`loss_prevented`）→ 全量，卡片标题写明「累计」
    - 瞬时类（`pending_cases`）→ 实时全量待审数
    """
    today = now_dt().replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)

    today_agg = _decisions_between(db, today)
    yday_agg = _decisions_between(db, yesterday, today)
    all_agg = _decisions_between(db)

    today_high = today_agg["Reject"] + today_agg["Review"]
    yday_high = yday_agg["Reject"] + yday_agg["Review"]

    pending = (db.query(func.count(RiskCase.case_id))
               .filter(RiskCase.case_status == "待审核").scalar() or 0)

    # R-MA-01：loss_prevented = Reject 的 order_* 事件金额累加 × 折扣系数（累计口径）
    loss_total = round(_reject_amount_between(db, None) * settings.LOSS_DISCOUNT, 2)
    loss_today = _reject_amount_between(db, today) * settings.LOSS_DISCOUNT
    loss_yday = _reject_amount_between(db, yesterday, today) * settings.LOSS_DISCOUNT

    today_total = today_agg["total"]
    yday_total = yday_agg["total"]
    today_pass_rate = round(today_agg["Pass"] / today_total, 4) if today_total else 0.0
    yday_pass_rate = round(yday_agg["Pass"] / yday_total, 4) if yday_total else 0.0

    return {
        "assess_total": today_total,
        "assess_delta": _delta(today_total, yday_total),
        "high_risk_count": today_high,
        "high_risk_delta": _delta(today_high, yday_high),
        "pending_cases": pending,
        "case_delta": today_high - yday_high,
        "pass_rate": today_pass_rate,
        "pass_rate_delta": round(today_pass_rate - yday_pass_rate, 4),
        "loss_prevented": loss_total,
        "loss_delta": round(loss_today - loss_yday, 2),
        "decision_dist": {
            "Pass": all_agg["Pass"], "Mark": all_agg["Mark"],
            "Challenge": all_agg["Challenge"], "Review": all_agg["Review"],
            "Reject": all_agg["Reject"],
        },
        # 口径说明：前端据此在卡片上标注「今日 / 累计 / 实时」
        "scope": {
            "assess_total": "today", "high_risk_count": "today",
            "pending_cases": "realtime", "pass_rate": "today",
            "loss_prevented": "all", "decision_dist": "all",
        },
    }


def _delta(cur: float, prev: float) -> float:
    if not prev:
        return 0.0
    return round((cur - prev) / prev, 4)


# 空桶模板（四档风险等级 + 五档决策）
_EMPTY_BUCKET = {
    "assess_cnt": 0, "high_risk_cnt": 0,
    "pass_cnt": 0, "mark_cnt": 0, "challenge_cnt": 0, "review_cnt": 0, "reject_cnt": 0,
    "low_cnt": 0, "medium_cnt": 0, "high_cnt": 0, "critical_cnt": 0,
}
_LEVEL_FIELD = {"低": "low_cnt", "中": "medium_cnt", "高": "high_cnt", "极高": "critical_cnt"}


def dashboard_trend(db: Session, days: int = 7, event_type: str | None = None) -> dict:
    """风险趋势（API-6.2 / FR-42）。按天聚合，**缺失日期补 0**（R-MA-05）。

    - `days`：统计窗口天数；兼容 `?days=7`（SPEC）与 `?hours=24`（旧契约）。
    - `event_type`：可选，按事件类型过滤（前端筛选区，join risk_event）。

    每个点位除五档决策计数外，**附带当日四档风险等级计数**
    （`low_cnt`/`medium_cnt`/`high_cnt`/`critical_cnt`），
    供前端「风险等级分布趋势」绘制真实日趋势（FR-43），而非退化为全量常数直线。
    """
    now = now_dt()
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)

    # R-MA-07：按 (日期, 决策) 与 (日期, 等级) 两次 SQL 聚合，不在 Python 里遍历评估表
    day_col = func.date(RiskAssessment.create_time).label("d")

    decision_q = db.query(day_col, RiskAssessment.decision,
                          func.count(RiskAssessment.assessment_id)).filter(
        RiskAssessment.create_time >= start)
    level_q = db.query(day_col, RiskAssessment.risk_level,
                       func.count(RiskAssessment.assessment_id)).filter(
        RiskAssessment.create_time >= start)
    if event_type:
        join_cond = RiskAssessment.event_id == RiskEvent.event_id
        decision_q = decision_q.join(RiskEvent, join_cond).filter(
            RiskEvent.event_type == event_type)
        level_q = level_q.join(RiskEvent, join_cond).filter(
            RiskEvent.event_type == event_type)

    buckets: dict[str, dict] = {}

    def _bucket(key) -> dict:
        return buckets.setdefault(str(key), dict(_EMPTY_BUCKET))

    _DECISION_FIELD = {"Reject": "reject_cnt", "Review": "review_cnt",
                       "Challenge": "challenge_cnt", "Mark": "mark_cnt",
                       "Pass": "pass_cnt"}
    for day, decision, count in decision_q.group_by(day_col, RiskAssessment.decision).all():
        b = _bucket(day)
        cnt = int(count or 0)
        b["assess_cnt"] += cnt
        field = _DECISION_FIELD.get(decision, "pass_cnt")
        b[field] += cnt
        if decision in ("Reject", "Review"):
            b["high_risk_cnt"] += cnt

    for day, level, count in level_q.group_by(day_col, RiskAssessment.risk_level).all():
        field = _LEVEL_FIELD.get(level)
        if field:
            _bucket(day)[field] += int(count or 0)

    points = []
    for i in range(days):
        day = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        b = buckets.get(day, dict(_EMPTY_BUCKET))
        cnt = b["assess_cnt"]
        points.append({
            "date": day,
            "time": day,
            "assess_cnt": cnt,
            "event_cnt": cnt,                    # 旧字段名兼容
            "high_risk_cnt": b["high_risk_cnt"],
            "reject_cnt": b["reject_cnt"],
            "review_cnt": b["review_cnt"],
            "challenge_cnt": b["challenge_cnt"],
            "mark_cnt": b["mark_cnt"],
            "pass_cnt": b["pass_cnt"],
            # 四档风险等级当日计数（FR-43 等级趋势）
            "low_cnt": b["low_cnt"],
            "medium_cnt": b["medium_cnt"],
            "high_cnt": b["high_cnt"],
            "critical_cnt": b["critical_cnt"],
            "reject_rate": round(b["reject_cnt"] / cnt, 4) if cnt else 0.0,
        })
    return {"days": days, "hours": days * 24, "event_type": event_type or "", "points": points}


def dashboard_risk_dist(db: Session) -> dict:
    """风险等级分布（API-6.3 / FR-43）—— **四档**：低 / 中 / 高 / 极高。

    R-MA-07：SQL `GROUP BY` 聚合，不把评估表读进内存。
    """
    counts = dict(db.query(RiskAssessment.risk_level,
                           func.count(RiskAssessment.assessment_id))
                  .group_by(RiskAssessment.risk_level).all())
    low = int(counts.get("低", 0))
    medium = int(counts.get("中", 0))
    high = int(counts.get("高", 0))
    critical = int(counts.get("极高", 0))
    total = low + medium + high + critical
    divisor = total or 1
    return {
        "total": total,
        "items": [
            {"level": "low", "name": "低风险", "count": low, "ratio": round(low / divisor, 4)},
            {"level": "medium", "name": "中风险", "count": medium, "ratio": round(medium / divisor, 4)},
            {"level": "high", "name": "高风险", "count": high, "ratio": round(high / divisor, 4)},
            {"level": "critical", "name": "极高风险", "count": critical, "ratio": round(critical / divisor, 4)},
        ],
    }


def dashboard_top_rules(db: Session, top: int = 5) -> dict:
    """规则命中排行（API-6.4 / FR-44）。

    用 JSON_TABLE 解析 risk_assessment.rule_results（Demo Doc-03 §5.1.5）。
    返回**分页对象**（含 items 键）。
    """
    try:
        rows = db.execute(text("""
            SELECT rr.rule_id, rr.rule_name, rr.rule_category, rr.risk_level,
                   COUNT(*) AS hit_count
            FROM risk_assessment ra
            CROSS JOIN JSON_TABLE(
                ra.rule_results, '$[*]'
                COLUMNS(rule_id VARCHAR(50) PATH '$.rule_id')
            ) jt
            JOIN risk_rule rr ON rr.rule_id = jt.rule_id
            GROUP BY rr.rule_id, rr.rule_name, rr.rule_category, rr.risk_level
            ORDER BY hit_count DESC
            LIMIT :top
        """), {"top": top}).all()
        items = [{"rule_id": r.rule_id, "rule_name": r.rule_name,
                  "rule_category": r.rule_category, "risk_level": r.risk_level,
                  "hit_count": int(r.hit_count)} for r in rows]
    except Exception as exc:  # noqa: BLE001  JSON_TABLE 不可用时降级为内存统计
        log.warning("JSON_TABLE 统计失败，降级为内存聚合：%s", exc)
        items = _top_rules_in_memory(db, top)

    return {"total": len(items), "page": 1, "page_size": top, "items": items}


def _top_rules_in_memory(db: Session, top: int) -> list[dict]:
    counter: dict[str, int] = {}
    meta: dict[str, RiskRule] = {r.rule_id: r for r in db.query(RiskRule).all()}
    for a in db.query(RiskAssessment).all():
        for hit in (from_json(a.rule_results) or []):
            rid = hit.get("rule_id")
            if rid:
                counter[rid] = counter.get(rid, 0) + 1
    ranked = sorted(counter.items(), key=lambda x: x[1], reverse=True)[:top]
    return [{"rule_id": rid, "rule_name": meta[rid].rule_name if rid in meta else rid,
             "rule_category": meta[rid].rule_category if rid in meta else "未知",
             "risk_level": meta[rid].risk_level if rid in meta else "未知",
             "hit_count": n} for rid, n in ranked]


def dashboard_realtime_events(db: Session, limit: int = 20) -> dict:
    """实时事件滚屏（API-6.5 / FR-45）。"""
    rows = (db.query(RiskEvent, RiskAssessment)
            .join(RiskAssessment, RiskAssessment.event_id == RiskEvent.event_id, isouter=True)
            .order_by(RiskEvent.create_time.desc()).limit(limit).all())
    return {
        "total": len(rows), "page": 1, "page_size": limit,
        "items": [{
            "event_id": e.event_id,
            "event_type": e.event_type,
            "user_id": e.user_id,
            "final_score": a.final_score if a else 0,
            "risk_level": a.risk_level if a else "低",
            "decision": a.decision if a else "Pass",
            "create_time": e.create_time.isoformat() if e.create_time else None,
        } for e, a in rows],
    }


def dashboard_loss_prevented(db: Session) -> dict:
    """资损防护统计（API-6.6 / FR-46），按场景拆分。

    R-MA-07：按 `event_type` 一次 SQL 聚合，不把 Reject 事件全读进内存。
    """
    rows = (db.query(RiskEvent.event_type, func.coalesce(func.sum(RiskEvent.amount), 0))
            .join(RiskAssessment, RiskAssessment.event_id == RiskEvent.event_id)
            .filter(RiskAssessment.decision == "Reject")
            .group_by(RiskEvent.event_type).all())
    by_scene: dict[str, float] = {str(t): to_float(v) for t, v in rows}

    items = [
        {"scene": "coupon_receive", "name": "营销 · 刷券拦截",
         "amount": round(by_scene.get("coupon_receive", 0.0) * settings.LOSS_DISCOUNT, 2)},
        {"scene": "order_create", "name": "交易 · 恶意下单拦截",
         "amount": round((by_scene.get("order_create", 0.0)
                          + by_scene.get("order_pay", 0.0)) * settings.LOSS_DISCOUNT, 2)},
        {"scene": "after_sale_apply", "name": "售后 · 恶意退款拦截",
         "amount": round(by_scene.get("after_sale_apply", 0.0) * settings.LOSS_DISCOUNT, 2)},
    ]
    return {"total": round(sum(i["amount"] for i in items), 2),
            "discount": settings.LOSS_DISCOUNT, "items": items}


def dashboard_rule_effectiveness(db: Session, days: int | None = None,
                                 event_type: str | None = None) -> dict:
    """规则效果分析（API-6.7 / FR-47）——命中次数、命中率、零命中识别。

    - `days`：可选统计窗口（近 N 天）；缺省为全量，保持向后兼容。
    - `event_type`：可选，按事件类型过滤（join risk_event）。

    命中率口径 = 窗口内该规则命中次数 ÷ 窗口内总评估数，二者窗口一致。
    """
    start = None
    if days:
        start = (now_dt() - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0)

    ast_q = db.query(func.count(RiskAssessment.assessment_id))
    if start is not None:
        ast_q = ast_q.filter(RiskAssessment.create_time >= start)
    if event_type:
        ast_q = ast_q.filter(RiskAssessment.event_id.in_(
            db.query(RiskEvent.event_id).filter(RiskEvent.event_type == event_type)))
    total_assess = ast_q.scalar() or 1

    if _json_table_available(db):
        where = ["1=1"]
        params: dict = {}
        join_ev = ""
        if event_type:
            join_ev = "JOIN risk_event re ON re.event_id = ra.event_id"
            where.append("re.event_type = :event_type")
            params["event_type"] = event_type
        if start is not None:
            where.append("ra.create_time >= :start")
            params["start"] = start
        hits = dict(db.execute(text(f"""
            SELECT jt.rule_id, COUNT(*) AS cnt
            FROM risk_assessment ra
            {join_ev}
            CROSS JOIN JSON_TABLE(
                ra.rule_results, '$[*]' COLUMNS(rule_id VARCHAR(50) PATH '$.rule_id')
            ) jt
            WHERE {' AND '.join(where)}
            GROUP BY jt.rule_id
        """), params).all())
    else:
        hits = _hit_counts_in_memory(db, start=start, event_type=event_type)

    items = []
    for rule in db.query(RiskRule).order_by(RiskRule.rule_id).all():
        count = int(hits.get(rule.rule_id, 0))
        rate = round(count / total_assess, 4) if total_assess else 0.0
        if rule.is_enabled != 1:
            effect = "已停用"
        elif count == 0:
            effect = "零命中"
        elif rate < 0.005:
            effect = "命中偏低"
        else:
            effect = "有效"
        items.append({
            "rule_id": rule.rule_id, "rule_name": rule.rule_name,
            "rule_category": rule.rule_category, "risk_level": rule.risk_level,
            "hit_count": count, "hit_rate": rate, "effect": effect,
            "is_enabled": rule.is_enabled,
        })

    zero = len([i for i in items if i["effect"] == "零命中"])
    return {"total": len(items), "page": 1, "page_size": len(items),
            "zero_hit_count": zero, "items": items}


def _json_table_available(db: Session) -> bool:
    try:
        db.execute(text("SELECT JSON_TABLE('[]', '$[*]' COLUMNS(a INT PATH '$')) AS t"))
        return True
    except Exception:  # noqa: BLE001
        return False


def _hit_counts_in_memory(db: Session, start=None, event_type: str | None = None) -> dict:
    """JSON_TABLE 不可用时的内存聚合降级路径（与主路径窗口口径保持一致）。"""
    query = db.query(RiskAssessment)
    if start is not None:
        query = query.filter(RiskAssessment.create_time >= start)
    if event_type:
        query = query.filter(RiskAssessment.event_id.in_(
            db.query(RiskEvent.event_id).filter(RiskEvent.event_type == event_type)))
    counter: dict[str, int] = {}
    for a in query.all():
        for hit in (from_json(a.rule_results) or []):
            rid = hit.get("rule_id")
            if rid:
                counter[rid] = counter.get(rid, 0) + 1
    return counter


# ============================================================================
# 系统健康度（API-6.10 / API-6.11）
# ============================================================================
_START_TS = now_dt()


def system_health(db: Session) -> dict:
    """吞吐、接口健康度与容量指标（仅 admin / ops）。

    ⚠ 接口健康度**必须**来自进程内真实计数器（R-MA-12）。服务刚启动、尚无请求时
       各项 `calls = 0`、`error_rate = 0`、`avg_latency_ms = 0` —— 这是正确的，
       不是「指标不可用」。v3.0 曾返回写死的示例常量，页面看起来正常但从不变化。
    """
    from .. import metrics

    now = now_dt()
    hour_ago = now - timedelta(hours=1)
    recent = (db.query(func.count(RiskAssessment.assessment_id))
              .filter(RiskAssessment.create_time >= hour_ago).scalar() or 0)

    pool_used = 0
    try:
        from ..database import engine
        pool_used = engine.pool.checkedout()
    except Exception:  # noqa: BLE001
        pool_used = 0

    snap = metrics.snapshot()
    total_calls = snap["total_calls"]
    total_errors = snap["total_errors"]
    # 全局平均耗时：按各项 calls 加权，避免「只有 1 次调用的慢接口」拉高整体均值
    weighted_ms = sum(i["avg_latency_ms"] * i["calls"] for i in snap["api_health"])
    api_avg_latency = round(weighted_ms / total_calls, 2) if total_calls else 0.0

    table_counts = _table_counts(db)
    db_size_mb = _db_size_mb(db)

    return {
        "event_qps": round(recent / 3600, 2),
        "api_error_rate": round(total_errors / total_calls, 6) if total_calls else 0.0,
        "api_avg_latency_ms": api_avg_latency,
        "api_total_calls": total_calls,
        "pool_used": pool_used,
        "pool_size": settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW,
        "pool_usage": round(pool_used / max(settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW, 1), 4),
        # 单进程同步处理，无消息队列组件（PRD §1.4 范围外已排除 Kafka / Celery），恒为 0
        "queue_backlog": 0,
        "queue_supported": False,
        "uptime_seconds": int((now - _START_TS).total_seconds()),
        "db_size_mb": db_size_mb,
        "business_table_count": len(BUSINESS_TABLES),
        "risk_table_count": len(RISK_TABLES),
        "table_rows": sum(table_counts.values()),
        "table_counts": table_counts,
        "api_health": snap["api_health"],
    }


def system_stats(db: Session) -> dict:
    """业务表与风控表行数统计（API-6.11 / FR-55）。"""
    counts = _table_counts(db)
    return {
        "business_table_count": len(BUSINESS_TABLES),
        "risk_table_count": len(RISK_TABLES),
        "total_rows": sum(counts.values()),
        "db_size_mb": _db_size_mb(db),
        "business_tables": [{"table": t, "rows": counts.get(t, 0)} for t in BUSINESS_TABLES],
        "risk_tables": [{"table": t, "rows": counts.get(t, 0)} for t in RISK_TABLES],
    }


def _table_counts(db: Session) -> dict[str, int]:
    """各表行数（信息模式快速估算，避免大表全扫）。"""
    names = BUSINESS_TABLES + RISK_TABLES
    try:
        rows = db.execute(text("""
            SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN :names
        """), {"names": tuple(names)}).all()
        return {r.TABLE_NAME: int(r.TABLE_ROWS or 0) for r in rows}
    except Exception:  # noqa: BLE001
        return {n: 0 for n in names}


def _db_size_mb(db: Session) -> float:
    try:
        size = db.execute(text("""
            SELECT ROUND(SUM(data_length + index_length) / 1024 / 1024, 2)
            FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()
        """)).scalar()
        return to_float(size)
    except Exception:  # noqa: BLE001
        return 0.0
