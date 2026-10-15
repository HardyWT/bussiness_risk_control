"""模块 9：AI Agent 工具集 —— 8 个 LangChain 工具。

对应文档：
- Demo Doc-03 §5.1  工具集（4 风控决策 + 4 数据分析）
- SPEC §9.5        工具清单与参数
- SPEC §9.7        R-AG-02（工具异常返回友好错误）/ R-AG-03（独立管理 DB Session）

设计要点（Demo Doc-03 §5.1.3）：
- 每个工具**独立管理数据库会话**，不依赖 FastAPI 请求上下文，保证线程安全
- 工具异常一律捕获并返回友好字符串，**不中断 Agent 对话**（R-AG-02）
"""

import contextvars
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from langchain_core.tools import tool
from sqlalchemy import text

from ..database import SessionLocal
from ..models import RiskBlacklist
from ..utils import mask_phone, now_dt, to_float

log = logging.getLogger(__name__)

# 当前操作人（由 chat 层设置，供写操作审计使用，R-AG-06 / R-AU-08）
current_operator: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agent_operator", default="agent")


def _get_db():
    """每个工具独立会话（Demo Doc-03 §5.1.3）。"""
    return SessionLocal()


def _dump(data: Any) -> str:
    """统一 JSON 输出（中文不转义、缩进 2）。"""
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _days_ago(days: int) -> datetime:
    return now_dt() - timedelta(days=days)


# ============================================================================
# 一、风控决策工具（4 个）
# ============================================================================
@tool
def risk_check(user_id: str, event_type: str = "order_create", source_id: str = "") -> str:
    """对指定用户和事件执行实时风险检查，返回评分、风险等级、决策与命中规则。

    Args:
        user_id: 用户编号，如 U-88213
        event_type: 事件类型，可选 login / coupon_receive / order_create /
                    order_pay / after_sale_apply / logistics_complaint
        source_id: 业务单据号（订单号/券号/售后单号），可留空自动生成
    """
    db = _get_db()
    try:
        from ..schemas import RiskCheckRequest
        from ..services import step1_event as event_svc

        request = RiskCheckRequest(
            event_type=event_type,
            source_id=source_id or f"AGT-{now_dt().strftime('%Y%m%d%H%M%S')}",
            user_id=user_id,
        )
        result = event_svc.process_event(db, request, dry_run=True)
        return _dump({
            "final_score": result["final_score"],
            "risk_level": result["risk_level"],
            "decision": result["decision"],
            "is_veto": result["is_veto"],
            "rule_count": result["rule_count"],
            "triggered_rules": [
                {"rule_id": r["rule_id"], "rule_name": r["rule_name"],
                 "risk_score": r["risk_score"], "risk_level": r["risk_level"],
                 "trigger_reason": r["trigger_reason"]}
                for r in result["triggered_rules"]
            ],
            "note": "本次为仿真评估，未落库",
        })
    except Exception as exc:  # noqa: BLE001  R-AG-02
        return f"风险检查失败: {exc}"
    finally:
        db.close()


@tool
def query_cases(case_status: str = "", page: int = 1) -> str:
    """查询风控案件列表，可按状态筛选。

    Args:
        case_status: 案件状态，可选 待审核 / 审核中 / 已处置 / 已关闭 / 已归档；留空查全部
        page: 页码，从 1 开始
    """
    db = _get_db()
    try:
        from ..services import step4_case as case_svc

        data = case_svc.get_case_list(db, case_status=case_status or None,
                                      page=max(page, 1), page_size=10)
        return _dump({
            "total": data["total"],
            "statistics": data["statistics"],
            "items": [{
                "case_id": c["case_id"], "user_id": c["user_id"],
                "case_status": c["case_status"], "case_category": c["case_category"],
                "final_score": c["final_score"], "risk_level": c["risk_level"],
                "create_time": c["create_time"],
            } for c in data["items"]],
        })
    except Exception as exc:  # noqa: BLE001
        return f"案件查询失败: {exc}"
    finally:
        db.close()


@tool
def query_user_profile(user_id: str) -> str:
    """查询指定用户的风险画像：综合评分、风险等级、订单/退款统计与风险标签。

    Args:
        user_id: 用户编号，如 U-88213
    """
    db = _get_db()
    try:
        from ..services import step4_case as case_svc

        profile = case_svc.get_user_profile(db, user_id)
        if profile is None:
            return _dump({"user_id": user_id, "has_profile": False,
                          "message": "该用户暂无画像数据"})
        return _dump(profile)
    except Exception as exc:  # noqa: BLE001
        return f"画像查询失败: {exc}"
    finally:
        db.close()


@tool
def manage_blacklist(action: str, entity_type: str = "user", value: str = "",
                     reason: str = "") -> str:
    """名单库管理：添加 / 移除 / 查询 / 列出。

    Args:
        action: 操作类型，可选 add / remove / check / list
        entity_type: 维度，可选 user / phone / ip / device / address
        value: 名单值（add / remove / check 时必填）
        reason: 加入原因（仅 add 时使用）
    """
    db = _get_db()
    try:
        from ..services import step4_case as case_svc

        operator = current_operator.get()

        if action == "add":
            if not value:
                return "添加失败：value 不能为空"
            row, err = case_svc.add_blacklist(db, "black", entity_type, value,
                                              reason or f"AI 助手添加（{operator}）",
                                              None, operator)
            if err:
                return f"添加失败：code={err}（可能已存在）"
            return _dump({"action": "add", "ok": True, "blacklist_id": row["blacklist_id"],
                          "entity_type": entity_type, "entity_value": value})

        if action == "remove":
            # ⚠ 必须限定 list_type='black'（SPEC §9.5.1）。三个名单类型共享
            #   (entity_type, entity_value)，唯一约束是三元组 ——
            #   只按 entity_type + value 查会命中同值的白名单 / 灰名单条目，
            #   把「移除黑名单」做成「删除白名单」。
            target = (db.query(RiskBlacklist)
                      .filter(RiskBlacklist.list_type == "black")
                      .filter(RiskBlacklist.entity_type == entity_type)
                      .filter(RiskBlacklist.entity_value == str(value)).first())
            if target is None:
                return f"移除失败：黑名单中未找到 {entity_type}={value}"
            _, err = case_svc.remove_blacklist(db, target.blacklist_id)
            return _dump({"action": "remove", "ok": err is None, "code": err})

        if action == "check":
            from ..services.step1_event import check_blacklist
            return _dump({"action": "check", "entity_type": entity_type,
                          "entity_value": value,
                          "in_blacklist": check_blacklist(db, entity_type, value)})

        # list
        data = case_svc.get_blacklist(db, list_type="black", entity_type=entity_type or None,
                                      page=1, page_size=20)
        return _dump({"action": "list", "total": data["total"], "counts": data["counts"],
                      "items": data["items"]})
    except Exception as exc:  # noqa: BLE001
        return f"名单管理失败: {exc}"
    finally:
        db.close()


# ============================================================================
# 二、数据分析工具（4 个）
# ============================================================================
@tool
def query_dashboard_stats() -> str:
    """查询风控仪表盘统计：今日评估量、高风险数、待审案件、通过率、决策分布与规则 TOP5。"""
    db = _get_db()
    try:
        from ..services import step6_audit as audit_svc

        overview = audit_svc.dashboard_overview(db)
        trend = audit_svc.dashboard_trend(db, days=7)
        top = audit_svc.dashboard_top_rules(db, top=5)
        return _dump({
            "overview": overview,
            "risk_trend": trend["points"],
            "top_rules": top["items"],
        })
    except Exception as exc:  # noqa: BLE001
        return f"仪表盘统计查询失败: {exc}"
    finally:
        db.close()


@tool
def analyze_risk_trend(days: int = 30) -> str:
    """分析最近若干天的风控趋势：每日评估量、风险等级分布、决策分布。

    Args:
        days: 分析天数，默认 30
    """
    db = _get_db()
    try:
        from ..services import step6_audit as audit_svc

        trend = audit_svc.dashboard_trend(db, days=days)
        dist = audit_svc.dashboard_risk_dist(db)
        totals = {"Pass": 0, "Mark": 0, "Challenge": 0, "Review": 0, "Reject": 0}
        for p in trend["points"]:
            totals["Pass"] += p["pass_cnt"]
            totals["Mark"] += p["mark_cnt"]
            totals["Challenge"] += p["challenge_cnt"]
            totals["Review"] += p["review_cnt"]
            totals["Reject"] += p["reject_cnt"]
        return _dump({"days": days, "daily": trend["points"],
                      "risk_level_distribution": dist["items"],
                      "decision_distribution": totals})
    except Exception as exc:  # noqa: BLE001
        return f"趋势分析失败: {exc}"
    finally:
        db.close()


@tool
def analyze_rule_effectiveness() -> str:
    """分析每条规则的命中效果：命中次数、命中率，并识别零命中（失效）规则。"""
    db = _get_db()
    try:
        from ..services import step6_audit as audit_svc

        data = audit_svc.dashboard_rule_effectiveness(db)
        zero = [i for i in data["items"] if i["effect"] == "零命中"]
        low = [i for i in data["items"] if i["effect"] == "命中偏低"]
        return _dump({
            "total_rules": data["total"],
            "zero_hit_count": data["zero_hit_count"],
            "zero_hit_rules": [{"rule_id": i["rule_id"], "rule_name": i["rule_name"]} for i in zero],
            "low_hit_rules": [{"rule_id": i["rule_id"], "rule_name": i["rule_name"],
                               "hit_rate": i["hit_rate"]} for i in low],
            "items": data["items"],
        })
    except Exception as exc:  # noqa: BLE001
        return f"规则效果分析失败: {exc}"
    finally:
        db.close()


@tool
def query_business_data(query_type: str, user_id: str = "", order_id: str = "",
                        limit: int = 10) -> str:
    """查询业务库数据（订单 / 售后 / 地址 / 明细 / 最近订单 / 高价值订单）。

    Args:
        query_type: 查询类型，可选 user_orders / user_postsales / user_addresses /
                    order_detail / recent_orders / high_value_orders
        user_id: 用户编号（user_orders / user_postsales / user_addresses 必填）
        order_id: 订单编号（order_detail 必填）
        limit: 返回条数上限，默认 10
    """
    db = _get_db()
    try:
        limit = max(1, min(int(limit or 10), 50))

        if query_type == "user_orders":
            rows = db.execute(text("""
                SELECT oi.order_id, oi.order_status, oi.create_time,
                       COALESCE(SUM(od.final_amount), 0) AS amount
                FROM order_info oi LEFT JOIN order_detail od ON oi.order_id = od.order_id
                WHERE oi.user_id = :uid
                GROUP BY oi.order_id ORDER BY oi.create_time DESC LIMIT :lim
            """), {"uid": user_id, "lim": limit}).all()
            return _dump({"query_type": query_type, "user_id": user_id,
                          "total_returned": len(rows),
                          "items": [{"order_id": r.order_id, "order_status": r.order_status,
                                     "amount": to_float(r.amount),
                                     "create_time": r.create_time} for r in rows]})

        if query_type == "user_postsales":
            rows = db.execute(text("""
                SELECT p.postsale_id, p.postsale_type, p.postsale_status,
                       p.refund_amount, p.postsale_reason, p.create_time
                FROM postsale p
                JOIN order_detail od ON p.order_detail_id = od.order_detail_id
                JOIN order_info   oi ON od.order_id = oi.order_id
                WHERE oi.user_id = :uid ORDER BY p.create_time DESC LIMIT :lim
            """), {"uid": user_id, "lim": limit}).all()
            return _dump({"query_type": query_type, "user_id": user_id,
                          "total_returned": len(rows),
                          "items": [{"postsale_id": r.postsale_id, "type": r.postsale_type,
                                     "status": r.postsale_status,
                                     "refund_amount": to_float(r.refund_amount),
                                     "reason": r.postsale_reason} for r in rows]})

        if query_type == "user_addresses":
            rows = db.execute(text("""
                SELECT receive_id, receiver_name, receiver_phone, receive_province,
                       receive_city, receive_district, receive_street_address
                FROM receive_info WHERE user_id = :uid LIMIT :lim
            """), {"uid": user_id, "lim": limit}).all()
            return _dump({"query_type": query_type, "user_id": user_id,
                          "total_returned": len(rows),
                          "items": [{"receive_id": r.receive_id,
                                     "receiver": r.receiver_name,
                                     "phone": mask_phone(r.receiver_phone),
                                     "province": r.receive_province,
                                     "city": r.receive_city,
                                     "district": r.receive_district,
                                     "address": r.receive_street_address} for r in rows]})

        if query_type == "order_detail":
            rows = db.execute(text("""
                SELECT od.order_detail_id, od.sku_id, od.sku_name, od.sku_count,
                       od.total_amount, od.discount_amount, od.final_amount
                FROM order_detail od WHERE od.order_id = :oid LIMIT :lim
            """), {"oid": order_id, "lim": limit}).all()
            return _dump({"query_type": query_type, "order_id": order_id,
                          "total_returned": len(rows),
                          "items": [{"order_detail_id": r.order_detail_id,
                                     "sku_name": r.sku_name, "count": r.sku_count,
                                     "total_amount": to_float(r.total_amount),
                                     "discount": to_float(r.discount_amount),
                                     "final_amount": to_float(r.final_amount)} for r in rows]})

        if query_type == "recent_orders":
            rows = db.execute(text("""
                SELECT oi.order_id, oi.user_id, oi.order_status, oi.create_time,
                       COALESCE(SUM(od.final_amount), 0) AS amount
                FROM order_info oi LEFT JOIN order_detail od ON oi.order_id = od.order_id
                GROUP BY oi.order_id ORDER BY oi.create_time DESC LIMIT :lim
            """), {"lim": limit}).all()
            return _dump({"query_type": query_type, "total_returned": len(rows),
                          "items": [{"order_id": r.order_id, "user_id": r.user_id,
                                     "order_status": r.order_status,
                                     "amount": to_float(r.amount)} for r in rows]})

        if query_type == "high_value_orders":
            rows = db.execute(text("""
                SELECT oi.order_id, oi.user_id, oi.order_status,
                       COALESCE(SUM(od.final_amount), 0) AS amount
                FROM order_info oi JOIN order_detail od ON oi.order_id = od.order_id
                GROUP BY oi.order_id HAVING amount >= 1000
                ORDER BY amount DESC LIMIT :lim
            """), {"lim": limit}).all()
            return _dump({"query_type": query_type, "total_returned": len(rows),
                          "items": [{"order_id": r.order_id, "user_id": r.user_id,
                                     "order_status": r.order_status,
                                     "amount": to_float(r.amount)} for r in rows]})

        return (f"不支持的 query_type: {query_type}。"
                "可选：user_orders / user_postsales / user_addresses / "
                "order_detail / recent_orders / high_value_orders")
    except Exception as exc:  # noqa: BLE001
        return f"业务数据查询失败: {exc}"
    finally:
        db.close()


# ============================================================================
# 工具分组导出（Demo Doc-03 §5.1.2）
# ============================================================================
RISK_TOOLS = [risk_check, query_cases, query_user_profile, manage_blacklist]
DATA_TOOLS = [query_dashboard_stats, analyze_risk_trend,
              analyze_rule_effectiveness, query_business_data]
ALL_TOOLS = RISK_TOOLS + DATA_TOOLS


def tool_catalog() -> list[dict]:
    """工具清单（GET /agent/tools，供前端展示）。"""
    groups = [("风控决策工具组", RISK_TOOLS), ("数据分析工具组", DATA_TOOLS)]
    catalog = []
    for group_name, tools in groups:
        for t in tools:
            catalog.append({
                "group": group_name,
                "name": t.name,
                "description": (t.description or "").strip().split("\n")[0],
                "args_schema": list((t.args or {}).keys()) if isinstance(t.args, dict) else [],
            })
    return catalog
