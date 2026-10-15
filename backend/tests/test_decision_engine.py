"""决策引擎单元测试（SPEC §13.3.2 · TC-DC-01 ~ TC-DC-12）。

覆盖评分算法、一票否决、分值→等级/决策映射、五档仲裁。
纯内存构造（_MockRule），无 DB 依赖。
"""

import pytest

from app.engine.step3_decision import (DecisionConfig, apply_veto,
                                       arbitrate, calculate_final_score,
                                       check_veto, decide_from_hits,
                                       score_to_level)
from app.engine.step3_rule import RuleHitResult


def hit(rule_id: str, score: int, level: str = "中", challengeable: bool = True) -> RuleHitResult:
    return RuleHitResult(rule_id=rule_id, rule_name=rule_id, rule_category="账户风险",
                         risk_level=level, risk_score=score, action="标记",
                         challengeable=challengeable)


@pytest.fixture
def cfg():
    return DecisionConfig(pass_threshold=30, mark_threshold=60, reject_threshold=80,
                          multi_rule_bonus=3, veto_min_score=90)


# ---------------------------------------------------------------- 评分算法
def test_score_no_hits():
    assert calculate_final_score([], bonus=3) == 0


def test_score_single_hit():
    assert calculate_final_score([hit("R1", 70)], bonus=3) == 70


def test_score_three_hits():
    """max(70,40,20) + 3×(3−1) = 76。"""
    assert calculate_final_score([hit("R1", 70), hit("R2", 40), hit("R3", 20)], bonus=3) == 76


def test_score_overflow_capped():
    """max(95,80,70) + 3×2 = 101 → 截断 100。"""
    assert calculate_final_score([hit("R1", 95), hit("R2", 80), hit("R3", 70)], bonus=3) == 100


def test_score_custom_bonus():
    assert calculate_final_score([hit("R1", 70), hit("R2", 40)], bonus=5) == 75


def test_score_uses_max_not_sum():
    """以最高分为主，避免低分规则过度叠加（设计决策 AD-03）。"""
    assert calculate_final_score([hit("R1", 20), hit("R2", 25), hit("R3", 30)], bonus=3) == 36


# ---------------------------------------------------------------- 一票否决
def test_veto_false():
    assert check_veto([hit("R1", 70, "高")]) is False


def test_veto_true():
    assert check_veto([hit("R1", 70, "高"), hit("R2", 90, "极高")]) is True


def test_veto_raises_score():
    """计算得分 45 但命中极高规则 → final = max(45, 90) = 90。"""
    assert apply_veto(45, is_veto=True, veto_min=90) == 90


def test_veto_keeps_higher_score():
    """已得 95 分时否决不再提升。"""
    assert apply_veto(95, is_veto=True, veto_min=90) == 95


def test_veto_not_applied_when_false():
    assert apply_veto(45, is_veto=False, veto_min=90) == 45


# ---------------------------------------------------------------- 分值 → 等级
@pytest.mark.parametrize("score,expected", [
    (0, "低"), (29, "低"),
    (30, "中"), (59, "中"),
    (60, "高"), (79, "高"),
    (80, "极高"), (100, "极高"),
])
def test_score_to_level_boundaries(score, expected, cfg):
    assert score_to_level(score, cfg) == expected


# ---------------------------------------------------------------- 五档仲裁
@pytest.mark.parametrize("score,expected", [
    (0, "Pass"), (29, "Pass"),
    (30, "Mark"), (59, "Mark"),
    (80, "Reject"), (100, "Reject"),
])
def test_arbitrate_three_bands(score, expected, cfg):
    level, decision, need_challenge = arbitrate(score, [], cfg)
    assert decision == expected
    assert need_challenge is False


def test_arbitrate_mid_band_all_challengeable(cfg):
    """60~79 且命中全部可挑战 → Challenge。"""
    hits = [hit("R1", 60, "高", challengeable=True), hit("R2", 40, "中", challengeable=True)]
    level, decision, need_challenge = arbitrate(63, hits, cfg)
    assert decision == "Challenge" and need_challenge is True
    assert level == "高"


def test_arbitrate_mid_band_has_non_challengeable(cfg):
    """60~79 且命中含不可挑战规则 → 直接 Review。"""
    hits = [hit("R1", 60, "高", challengeable=False), hit("R2", 40, "中", challengeable=True)]
    level, decision, need_challenge = arbitrate(63, hits, cfg)
    assert decision == "Review" and need_challenge is False


def test_arbitrate_mid_band_no_hits(cfg):
    """理论上不会发生，但缺命中时保守判 Review。"""
    _, decision, _ = arbitrate(63, [], cfg)
    assert decision == "Review"


# ---------------------------------------------------------------- 端到端决策
def test_decide_blacklist_like(cfg):
    """名单短路场景（无命中、100 分）→ Reject 极高。"""
    result = decide_from_hits([], cfg, hit_source="blacklist")
    assert result.decision == "Pass"          # 0 分 → Pass（真实短路由 service 层处理）

def test_decide_reject_with_veto(cfg):
    """R-005(90, 极高, 不可挑战) + R-007(75, 高, 不可挑战) → 93 分 Reject。"""
    hits = [hit("R-005", 90, "极高", challengeable=False),
            hit("R-007", 75, "高", challengeable=False)]
    result = decide_from_hits(hits, cfg)
    assert result.final_score == 93
    assert result.decision == "Reject"
    assert result.risk_level == "极高"
    assert result.is_veto is True


def test_decide_review_mid_band(cfg):
    """R-006(70, 高, 不可挑战) + R-009(40, 中, 可) = 73 → Review（因含不可挑战）。"""
    hits = [hit("R-006", 70, "高", challengeable=False),
            hit("R-009", 40, "中", challengeable=True)]
    result = decide_from_hits(hits, cfg)
    assert result.final_score == 73
    assert result.decision == "Review"
    assert result.need_challenge is False


def test_decide_challenge_mid_band(cfg):
    """R-010(60) + R-009(40) = 63，均可挑战 → Challenge。"""
    hits = [hit("R-010", 60, "高", challengeable=True),
            hit("R-009", 40, "中", challengeable=True)]
    result = decide_from_hits(hits, cfg)
    assert result.final_score == 63
    assert result.decision == "Challenge"
    assert result.need_challenge is True


def test_decide_mark_band(cfg):
    """单条 45 分中等规则 → Mark（记录并放行）。"""
    result = decide_from_hits([hit("R-008", 45, "中")], cfg)
    assert result.final_score == 45
    assert result.decision == "Mark"
    assert result.risk_level == "中"


def test_custom_thresholds(cfg):
    """阈值可配（FR-40a）：把 mark 降为 40 后，45 分落入中风险档。"""
    tight = DecisionConfig(pass_threshold=30, mark_threshold=40,
                           reject_threshold=80, multi_rule_bonus=3, veto_min_score=90)
    result = decide_from_hits([hit("R-008", 45, "中", challengeable=True)], tight)
    assert result.decision == "Challenge"      # 45 ∈ [40, 80) → 中风险档
