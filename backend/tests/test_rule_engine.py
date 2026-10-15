"""规则引擎单元测试（SPEC §13.3.1 · TC-RE-01 ~ TC-RE-15）。

覆盖 10 种运算符、and/or 嵌套、缺失特征、条件树校验、触发原因生成。
纯内存构造，无外部依赖。
"""

import pytest

from app.engine.step3_rule import (CachedRule, build_trigger_reason, compare,
                                   evaluate_condition, validate_condition)


@pytest.fixture
def f():
    return {"x": 10.0, "y": 3.0, "a": 60.0, "b": 1.0, "c": 200.0}


# ---------------------------------------------------------------- 10 种运算符
def test_gt(f):
    assert evaluate_condition({"field": "x", "op": ">", "value": 5}, f) is True
    assert evaluate_condition({"field": "y", "op": ">", "value": 5}, f) is False


def test_gte(f):
    assert evaluate_condition({"field": "b", "op": ">=", "value": 1}, f) is True
    assert evaluate_condition({"field": "y", "op": ">=", "value": 5}, f) is False


def test_lt(f):
    assert evaluate_condition({"field": "y", "op": "<", "value": 5}, f) is True
    assert evaluate_condition({"field": "x", "op": "<", "value": 5}, f) is False


def test_lte(f):
    assert evaluate_condition({"field": "x", "op": "<=", "value": 10}, f) is True
    assert evaluate_condition({"field": "x", "op": "<=", "value": 6}, f) is False


def test_eq(f):
    assert evaluate_condition({"field": "b", "op": "==", "value": 1}, f) is True
    assert evaluate_condition({"field": "b", "op": "==", "value": 2}, f) is False


def test_ne(f):
    assert evaluate_condition({"field": "b", "op": "!=", "value": 1}, f) is False
    assert compare(2.0, "!=", 1) is True


def test_between(f):
    assert evaluate_condition({"field": "y", "op": "between", "value": [0, 5]}, f) is True
    assert evaluate_condition({"field": "y", "op": "between", "value": [0, 3]}, f) is True   # 闭区间
    assert evaluate_condition({"field": "x", "op": "between", "value": [0, 5]}, f) is False
    assert evaluate_condition({"field": "y", "op": "between", "value": [6, 9]}, f) is False


def test_in(f):
    assert evaluate_condition({"field": "y", "op": "in", "value": [1, 2, 3]}, f) is True
    assert evaluate_condition({"field": "x", "op": "in", "value": [1, 2, 3]}, f) is False


def test_not_in(f):
    assert evaluate_condition({"field": "x", "op": "not_in", "value": [1, 2]}, f) is True
    assert evaluate_condition({"field": "y", "op": "not_in", "value": [1, 2, 3]}, f) is False


# ---------------------------------------------------------------- 逻辑组合
def test_and(f):
    cond = {"and": [{"field": "x", "op": ">", "value": 5},
                    {"field": "y", "op": "<", "value": 10}]}
    assert evaluate_condition(cond, f) is True
    cond2 = {"and": [{"field": "x", "op": ">", "value": 5},
                     {"field": "y", "op": "<", "value": 1}]}
    assert evaluate_condition(cond2, f) is False


def test_or(f):
    cond = {"or": [{"field": "x", "op": ">", "value": 100},
                   {"field": "b", "op": "==", "value": 1}]}
    assert evaluate_condition(cond, f) is True


def test_nested_and_or(f):
    cond = {"and": [
        {"field": "a", "op": ">=", "value": 50},
        {"or": [{"field": "b", "op": "==", "value": 1},
                {"field": "c", "op": ">", "value": 1000}]},
    ]}
    assert evaluate_condition(cond, f) is True


# ---------------------------------------------------------------- 异常与容错
def test_missing_field(f):
    """特征不存在 → 不命中（不抛异常），对应 SPEC §3.5。"""
    assert evaluate_condition({"field": "missing", "op": ">", "value": 1}, f) is False


def test_unsupported_op(f):
    """不支持的运算符 → 不命中。"""
    assert evaluate_condition({"field": "x", "op": "magic", "value": 1}, f) is False


def test_empty_condition():
    assert evaluate_condition({}, {}) is False
    assert evaluate_condition(None, {}) is False


def test_type_mismatch_safe(f):
    """value 非数值时比较返回 False 而不是抛异常。"""
    assert compare(10.0, ">", "abc") is False
    assert compare(10.0, "between", [1]) is False


# ---------------------------------------------------------------- 条件树校验（FR-36）
def test_validate_ok():
    ok, err = validate_condition({"field": "x", "op": ">", "value": 5})
    assert ok is True and err is None


def test_validate_unsupported_op():
    ok, err = validate_condition({"field": "x", "op": "magic", "value": 1})
    assert ok is False and "不支持的运算符" in err


def test_validate_between_arity():
    ok, err = validate_condition({"field": "x", "op": "between", "value": [1, 2, 3]})
    assert ok is False and "between" in err


def test_validate_in_requires_list():
    ok, err = validate_condition({"field": "x", "op": "in", "value": 5})
    assert ok is False and "数组" in err


def test_validate_empty_and():
    ok, err = validate_condition({"and": []})
    assert ok is False and "非空数组" in err


def test_validate_nested_reports_path():
    ok, err = validate_condition({"and": [{"field": "x", "op": ">", "value": 1},
                                          {"field": "y", "op": "bad", "value": 1}]})
    assert ok is False and "$.and[1]" in err


# ---------------------------------------------------------------- 触发原因（FR-17）
def test_trigger_reason_renders_actual_value():
    rule = CachedRule(
        rule_id="R-001", rule_name="高退款率用户", rule_category="售后滥用",
        event_type="after_sale_apply",
        rule_condition={"field": "user_refund_rate", "op": ">", "value": 0.5},
        risk_level="高", risk_score=70, action="人工审核", challengeable=True, priority=100,
    )
    reason = build_trigger_reason(rule, {"user_refund_rate": 0.83})
    assert "user_refund_rate=0.83" in reason
    assert "0.5" in reason


# ---------------------------------------------------------------- 规则快照（跨 Session 安全）
def test_cached_rule_from_orm_parses_json_string():
    class _FakeRule:
        rule_id = "R-X"
        rule_name = "x"
        rule_category = "账户风险"
        event_type = "通用"
        rule_condition = '{"field": "x", "op": ">", "value": 1}'
        risk_level = "中"
        risk_score = 40
        action = "标记"
        challengeable = 1
        priority = 10
        description = None

    rule = CachedRule.from_orm(_FakeRule())
    assert isinstance(rule.rule_condition, dict)
    assert rule.challengeable is True
