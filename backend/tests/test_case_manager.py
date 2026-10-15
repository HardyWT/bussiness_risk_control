"""案件与名单管理集成测试（SPEC §13.3.5 · TC-CM-01 ~ TC-CM-09）。"""

from datetime import timedelta

import pytest

from app.models import RiskAssessment, RiskBlacklist, RiskCase
from app.services import step4_case as case_svc
from app.utils import now_dt


# ================================================================ 名单库（FR-37~40）
def test_add_and_check_blacklist(db_session):
    row, err = case_svc.add_blacklist(db_session, "black", "user", "U-CM-1",
                                      "测试拉黑", None, "tester")
    assert err is None and row["blacklist_id"] > 0

    from app.services.step1_event import check_blacklist
    assert check_blacklist(db_session, "user", "U-CM-1") is True


def test_check_nonexistent_blacklist(db_session):
    from app.services.step1_event import check_blacklist
    assert check_blacklist(db_session, "user", "U-NOT-EXIST-XX") is False


def test_duplicate_blacklist_rejected(db_session):
    case_svc.add_blacklist(db_session, "black", "device", "DEV-CM-DUP", "r", None, "t")
    _, err = case_svc.add_blacklist(db_session, "black", "device", "DEV-CM-DUP", "r", None, "t")
    assert err == "3004"        # UNIQUE 冲突


def test_expired_blacklist_not_matched(db_session):
    """FR-39 / R-RE-03：过期记录不触发拦截。"""
    yesterday = (now_dt() - timedelta(days=1)).isoformat()
    case_svc.add_blacklist(db_session, "black", "phone", "13900001111",
                           "已过期", yesterday, "tester")

    from app.services.step1_event import find_blacklist
    assert find_blacklist(db_session, "phone", "13900001111") is None


def test_active_blacklist_matched(db_session):
    tomorrow = (now_dt() + timedelta(days=1)).isoformat()
    case_svc.add_blacklist(db_session, "black", "phone", "13900002222",
                           "生效中", tomorrow, "tester")
    from app.services.step1_event import find_blacklist
    assert find_blacklist(db_session, "phone", "13900002222") is not None


def test_remove_blacklist(db_session):
    row, _ = case_svc.add_blacklist(db_session, "black", "ip", "10.1.2.3", "r", None, "t")
    data, err = case_svc.remove_blacklist(db_session, row["blacklist_id"])
    assert err is None and data["deleted"] is True

    from app.services.step1_event import check_blacklist
    assert check_blacklist(db_session, "ip", "10.1.2.3") is False


def test_whitelist_priority_over_blacklist(db_session):
    """R-RE-08：同一实体同时在白/黑名单时按白名单处理。"""
    case_svc.add_blacklist(db_session, "black", "user", "U-CM-BOTH", "黑", None, "t")
    case_svc.add_blacklist(db_session, "white", "user", "U-CM-BOTH", "白", None, "t")

    from app.services.step1_event import match_watchlist
    hit = match_watchlist(db_session, {"user_id": "U-CM-BOTH"})
    assert hit is not None
    assert hit[0] == "white"        # 白名单优先


def test_blacklist_list_with_counts(db_session):
    case_svc.add_blacklist(db_session, "black", "user", "U-CM-L1", "r", None, "t")
    data = case_svc.get_blacklist(db_session, list_type="black")
    assert "counts" in data and data["counts"]["black"] >= 1
    assert isinstance(data["items"], list)


def test_blacklist_import_row_validation(db_session):
    """FR-39：逐行预校验，返回成功/失败行与原因。"""
    class _Item:
        def __init__(self, v, r=None):
            self.entity_value = v
            self.reason = r

    result = case_svc.import_blacklist(
        db_session, "black", "phone",
        [_Item("13700001111", "ok"), _Item("123", "bad"), _Item("", "empty")],
        "tester")
    assert result["success"] == 1
    assert result["failed"] == 2
    reasons = " ".join(e["reason"] for e in result["errors"])
    assert "格式非法" in reasons and "为空" in reasons


# ================================================================ 案件统计与查询
def test_case_statistics_has_all_statuses(db_session):
    """R-CF-08：每种状态补零，返回固定 5 个键。"""
    stats = case_svc.get_case_statistics(db_session)
    assert set(stats["by_status"].keys()) == {"待审核", "审核中", "已处置", "已关闭", "已归档"}
    assert stats["total"] >= 0


def test_case_list_empty_is_safe(db_session):
    data = case_svc.get_case_list(db_session, page=1, page_size=10)
    assert data["total"] >= 0
    assert isinstance(data["items"], list)
    assert "statistics" in data


def test_case_list_filter_by_status(db_session):
    data = case_svc.get_case_list(db_session, case_status="待审核")
    assert all(c["case_status"] == "待审核" for c in data["items"])


def test_case_detail_not_found(db_session):
    data, err = case_svc.get_case_detail(db_session, "cas-not-exist")
    assert data is None and err == "2001"


def test_hold_nonexistent_case(db_session):
    data, err = case_svc.hold_case(db_session, "cas-not-exist", "tester")
    assert data is None and err == "2001"


def test_dispose_nonexistent_case(db_session):
    data, err = case_svc.dispose_case(db_session, "cas-not-exist", "reject", "r", "tester")
    assert data is None and err == "2001"


def test_dispose_invalid_conclusion(db_session):
    data, err = case_svc.dispose_case(db_session, "cas-any", "invalid", "r", "tester")
    assert err == "2001"        # 案件不存在优先返回


# ================================================================ 用户画像（含降级）
def test_profile_fallback_when_missing(db_session):
    """无画像时降级为实时计算（Demo Doc-03 §5.1.4）。

    降级返回的画像字段与正常画像一致（total_orders/refund_rate…），
    并把原始特征放在 computed_features 里。
    """
    profile = case_svc.get_user_profile(db_session, "U-NO-PROFILE-XYZ")
    assert profile is not None
    assert profile["has_profile"] is False
    assert "total_orders" in profile
    assert "computed_features" in profile
    assert profile["total_orders"] == 0


def test_profile_returns_tags_field(db_session):
    profile = case_svc.get_user_profile(db_session, "U-NO-PROFILE-2")
    assert "risk_tags" in profile


# ================================================================ 列表筛选口径（回归）
@pytest.fixture
def seeded_cases(db_session):
    """造 3 条案件（2 待审核 + 1 已处置），覆盖状态 / 等级 / 分类 / top_rule_id。"""
    now = now_dt()
    rows = [
        ("cas-t1", "ast-t1", "待审核", "账户风险", "高", 70,
         [{"rule_id": "R-001", "rule_category": "账户风险", "risk_score": 70}]),
        ("cas-t2", "ast-t2", "待审核", "订单欺诈", "极高", 95,
         [{"rule_id": "R-002", "rule_category": "订单欺诈", "risk_score": 95},
          {"rule_id": "R-003", "rule_category": "账户风险", "risk_score": 40}]),
        ("cas-t3", "ast-t3", "已处置", "账户风险", "极高", 90, []),
    ]
    for cid, aid, status, cat, level, score, rules in rows:
        db_session.add(RiskAssessment(
            assessment_id=aid, event_id=f"evt-{cid}", user_id="U-SEL",
            rule_results=rules, rule_count=len(rules), final_score=score,
            risk_level=level, decision="Review", is_veto=0, hit_source="rule",
            cost_ms=1.0, create_time=now,
        ))
        db_session.add(RiskCase(
            case_id=cid, assessment_id=aid, event_id=f"evt-{cid}", user_id="U-SEL",
            case_status=status, case_category=cat, final_score=score,
            risk_level=level, create_time=now,
        ))
    db_session.commit()
    try:
        yield [r[0] for r in rows]
    finally:
        for cid, aid, *_ in rows:
            db_session.query(RiskCase).filter(RiskCase.case_id == cid).delete()
            db_session.query(RiskAssessment) \
                .filter(RiskAssessment.assessment_id == aid).delete()
        db_session.commit()


def test_case_list_accepts_english_status_filter(db_session, seeded_cases):
    """回归：前端下拉发英文 pending，必须命中库内中文「待审核」。"""
    data = case_svc.get_case_list(db_session, case_status="pending", page_size=100)
    mine = [c for c in data["items"] if c["case_id"] in seeded_cases]
    assert len(mine) == 2
    assert all(c["case_status"] == "待审核" for c in mine)


def test_case_list_accepts_english_level_filter(db_session, seeded_cases):
    """回归：critical → 极高、high → 高；大小写与首尾空白同样容错。"""
    crit = case_svc.get_case_list(db_session, risk_level="critical", page_size=100)
    mine = [c for c in crit["items"] if c["case_id"] in seeded_cases]
    assert len(mine) == 2 and all(c["risk_level"] == "极高" for c in mine)

    high = case_svc.get_case_list(db_session, risk_level="  HIGH  ", page_size=100)
    mine = [c for c in high["items"] if c["case_id"] in seeded_cases]
    assert len(mine) == 1 and mine[0]["risk_level"] == "高"


def test_case_list_chinese_filter_still_works(db_session, seeded_cases):
    """中文枚举口径不被别名层破坏。"""
    data = case_svc.get_case_list(db_session, case_status="待审核", risk_level="高",
                                  page_size=100)
    mine = [c for c in data["items"] if c["case_id"] in seeded_cases]
    assert len(mine) == 1 and mine[0]["case_id"] == "cas-t1"


def test_case_list_statistics_total_is_full_scope(db_session, seeded_cases):
    """顶部统计卡要求全量口径：statistics.total 不随筛选缩水。"""
    full = case_svc.get_case_statistics(db_session)["total"]
    data = case_svc.get_case_list(db_session, case_status="已处置", page_size=100)
    assert data["statistics"]["total"] == full
    assert data["total"] <= full
    assert data["total"] >= 1        # 至少 cas-t3


def test_case_list_category_filter_and_by_category(db_session, seeded_cases):
    """分类筛选可命中；by_category 返回全量分布，供前端动态生成选项。"""
    data = case_svc.get_case_list(db_session, case_category="订单欺诈", page_size=100)
    mine = [c for c in data["items"] if c["case_id"] in seeded_cases]
    assert len(mine) == 1 and mine[0]["case_id"] == "cas-t2"
    assert isinstance(data["by_category"], dict)
    assert data["by_category"].get("账户风险", 0) >= 2


def test_case_brief_exposes_top_rule_id(db_session, seeded_cases):
    """列表项回填最高分命中规则号（前端模板已引用 top_rule_id）。"""
    data = case_svc.get_case_list(db_session, page_size=100)
    by_id = {c["case_id"]: c for c in data["items"]}
    assert by_id["cas-t2"]["top_rule_id"] == "R-002"   # 95 分 > 40 分
    assert by_id["cas-t1"]["top_rule_id"] == "R-001"
    assert by_id["cas-t3"]["top_rule_id"] is None      # 无命中规则
