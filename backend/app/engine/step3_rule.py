"""模块 3：规则引擎 —— JSON 条件树求值。

对应文档：
- Demo Doc-03 §3.2.1  JSON 条件表达式设计（单条件 / 组合 / 嵌套）
- SPEC §3.5          10 种运算符
- SPEC §3.7          R-RE-04/07/09（求值容错、规则加载）

安全说明：本模块只做「结构化 JSON 递归求值」，不做字符串表达式 eval，
         从设计上杜绝注入风险（Demo Doc-03 §3.2.2）。
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import RiskRule

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 运算符白名单（10 种）
# 叶子运算符 9 种 + 逻辑组合 and / or = 10（SPEC §3.5 / PRD FR-13）
_LEAF_OPERATORS = {">", ">=", "<", "<=", "==", "!=", "in", "not_in", "between"}
LOGIC_OPERATORS = ("and", "or")


class ConditionError(ValueError):
    """条件树格式错误 → 接口返回 3002（SPEC §3.8）。"""

    def __init__(self, message: str, path: str = ""):
        self.path = path
        super().__init__(f"条件树格式错误：{message}" + (f"（位置 {path}）" if path else ""))


# ============================================================================
# 求值
# ============================================================================
def evaluate_condition(condition: dict | None, features: dict[str, float]) -> bool:
    """递归求值（Demo Doc-03 §3.2.2）。

    - `{"and": [...]}` → all() 短路求值
    - `{"or":  [...]}` → any() 短路求值
    - `{"field","op","value"}` → 单条件比较
    - 特征不存在视为**不命中**（不抛异常）
    """
    if not isinstance(condition, dict):
        return False

    if "and" in condition:
        children = condition.get("and") or []
        if not isinstance(children, list):
            return False
        return all(evaluate_condition(sub, features) for sub in children)

    if "or" in condition:
        children = condition.get("or") or []
        if not isinstance(children, list):
            return False
        return any(evaluate_condition(sub, features) for sub in children)

    field_name = condition.get("field")
    op = condition.get("op")
    value = condition.get("value")

    if field_name is None or op is None:
        return False

    actual = features.get(field_name)
    if actual is None:
        return False                       # 特征不存在视为不命中
    return compare(actual, op, value)


def compare(actual: float, op: str, value: Any) -> bool:
    """10 种运算符比较（Demo Doc-03 §3.2.3）。所有数值统一 float() 转换。"""
    try:
        if op == ">":
            return actual > float(value)
        if op == ">=":
            return actual >= float(value)
        if op == "<":
            return actual < float(value)
        if op == "<=":
            return actual <= float(value)
        if op == "==":
            return actual == float(value)
        if op == "!=":
            return actual != float(value)
        if op == "in":
            return actual in [float(v) for v in value]
        if op == "not_in":
            return actual not in [float(v) for v in value]
        if op == "between":
            low, high = float(value[0]), float(value[1])
            return low <= actual <= high
    except (TypeError, ValueError, IndexError, KeyError):
        return False
    return False                            # 不支持的运算符视为不命中


# ============================================================================
# 校验（FR-36 / 返回具体错误位置）
# ============================================================================
def validate_condition(condition: Any, path: str = "$") -> tuple[bool, str | None]:
    """递归校验条件树结构，返回 (是否合法, 错误信息)。"""
    try:
        _validate(condition, path)
        return True, None
    except ConditionError as exc:
        return False, str(exc)


def _validate(node: Any, path: str) -> None:
    if not isinstance(node, dict):
        raise ConditionError("节点必须是 JSON 对象", path)

    if "and" in node or "or" in node:
        key = "and" if "and" in node else "or"
        children = node.get(key)
        if not isinstance(children, list) or not children:
            raise ConditionError(f"「{key}」必须是**非空数组**", f"{path}.{key}")
        for idx, child in enumerate(children):
            _validate(child, f"{path}.{key}[{idx}]")
        return

    field_name = node.get("field")
    op = node.get("op")
    value = node.get("value")

    if not field_name or not isinstance(field_name, str):
        raise ConditionError("叶子节点缺少 field 或类型非字符串", path)
    if op not in _LEAF_OPERATORS:
        raise ConditionError(
            f"不支持的运算符 {op!r}，可选 {sorted(_LEAF_OPERATORS)}", f"{path}.op")
    if "value" not in node:
        raise ConditionError("叶子节点缺少 value", path)

    if op in ("in", "not_in"):
        if not isinstance(value, list) or not value:
            raise ConditionError(f"运算符「{op}」的 value 必须是非空数组", f"{path}.value")
    elif op == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise ConditionError("运算符「between」的 value 必须是长度为 2 的数组", f"{path}.value")
    else:
        if isinstance(value, (list, dict)) or value is None:
            raise ConditionError(f"运算符「{op}」的 value 必须是标量", f"{path}.value")
        try:
            float(value)
        except (TypeError, ValueError):
            raise ConditionError(
                f"运算符「{op}」的 value 必须是数值，实际为 {value!r}", f"{path}.value") from None


# ============================================================================
# 命中结果与规则加载
# ============================================================================
@dataclass
class RuleHitResult:
    """命中规则结果（Demo Doc-03 §3.2.4），可直接序列化进 risk_assessment.rule_results。"""
    rule_id: str
    rule_name: str
    rule_category: str
    risk_level: str
    risk_score: int
    action: str
    challengeable: bool = True
    trigger_reason: str = ""
    description: str | None = None

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "rule_category": self.rule_category,
            "risk_level": self.risk_level,
            "risk_score": self.risk_score,
            "action": self.action,
            "challengeable": int(self.challengeable),
            "trigger_reason": self.trigger_reason,
            "description": self.description,
        }


# 规则缓存（R-RE-05：TTL ≤ 5s，改规则后清缓存立即生效）
#
# ⚠ 必须缓存**轻量不可变对象**而不是 ORM 实例：
#    ORM 实例与创建它的 Session 绑定，请求结束 Session 关闭后对象 detach，
#    下一个请求命中缓存再访问属性就会抛 DetachedInstanceError。
@dataclass(frozen=True)
class CachedRule:
    """规则快照（脱离 Session 后可安全跨请求复用）。"""
    rule_id: str
    rule_name: str
    rule_category: str
    event_type: str
    rule_condition: dict
    risk_level: str
    risk_score: int
    action: str
    challengeable: bool
    priority: int
    description: str | None = None

    @classmethod
    def from_orm(cls, rule: RiskRule) -> "CachedRule":
        condition = rule.rule_condition
        if isinstance(condition, str):
            import json
            try:
                condition = json.loads(condition)
            except (ValueError, TypeError):
                condition = {}
        return cls(
            rule_id=rule.rule_id,
            rule_name=rule.rule_name,
            rule_category=rule.rule_category,
            event_type=rule.event_type,
            rule_condition=condition or {},
            risk_level=rule.risk_level,
            risk_score=rule.risk_score,
            action=rule.action,
            challengeable=bool(rule.challengeable),
            priority=rule.priority or 0,
            description=rule.description,
        )


_cache_lock = threading.Lock()
_rule_cache: dict[str, tuple[float, list[CachedRule]]] = {}


def clear_rule_cache() -> None:
    with _cache_lock:
        _rule_cache.clear()


def load_enabled_rules(db: Session, event_type: str | None = None) -> list[CachedRule]:
    """加载「本场景 + 通用」的启用规则，按 priority 降序（R-RE-09）。

    返回的是 `CachedRule` 快照（非 ORM 实例），可安全跨请求缓存与复用。
    """
    cache_key = event_type or "*"
    now = time.monotonic()
    with _cache_lock:
        cached = _rule_cache.get(cache_key)
        if cached and now - cached[0] < settings.RULE_CACHE_TTL_SECONDS:
            return cached[1]

    stmt = select(RiskRule).where(RiskRule.is_enabled == 1)
    if event_type:
        stmt = stmt.where(RiskRule.event_type.in_([event_type, "通用"]))
    rows = list(db.scalars(stmt.order_by(RiskRule.priority.desc(), RiskRule.rule_id)).all())
    rules = [CachedRule.from_orm(r) for r in rows]      # ← 转快照，切断 Session 绑定

    with _cache_lock:
        _rule_cache[cache_key] = (now, rules)
    return rules


def build_trigger_reason(rule: CachedRule, features: dict[str, float]) -> str:
    """把条件树还原成一句可读的触发原因（FR-17）。

    ⚠ 连接词必须**逐层取自各自节点**（`and` → 「且」、`or` → 「或」），
    不能只看顶层。v3.0 用 `"and" in rule.rule_condition` 决定全文连接词，
    对嵌套条件（如 `{"and":[…, {"or":[…]}]}`）会把 `or` 分支里的条件
    也用「且」连起来 —— 输出的是**语义错误的**触发原因，
    审核员据此判断会得出相反结论。本版按递归层级各自拼接。
    """
    node = rule.rule_condition

    def render(node: Any) -> str:
        if not isinstance(node, dict):
            return ""
        if "and" in node or "or" in node:
            key = "and" if "and" in node else "or"
            joiner = " 且 " if key == "and" else " 或 "
            children = [render(c) for c in (node.get(key) or [])]
            children = [c for c in children if c]
            if not children:
                return ""
            # 子节点本身是多条件组合时加括号，避免 a 且 b 或 c 的歧义
            parts = [f"（{c}）" if (" 且 " in c or " 或 " in c) else c for c in children]
            return joiner.join(parts)

        field_name = node.get("field")
        op = node.get("op")
        value = node.get("value")
        actual = features.get(field_name)
        if actual is None:
            return f"{field_name} {op} {value}"
        return f"{field_name}={_fmt(actual)} {op} {value}"

    return render(node) or rule.rule_name


def _fmt(value: Any) -> str:
    try:
        number = float(value)
        return str(int(number)) if number.is_integer() else f"{number:.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return str(value)


def match_rules(rules: list[CachedRule], features: dict[str, float]) -> list[RuleHitResult]:
    """逐条求值（R-RE-07：单条异常跳过并记 warn，不影响其他规则）。

    入参是 `load_enabled_rules()` 返回的 **`CachedRule` 快照**，不是 ORM 实例
    —— 规则缓存跨请求复用，存 ORM 实例会在 Session 关闭后抛 `DetachedInstanceError`。
    """
    hits: list[RuleHitResult] = []
    for rule in rules:
        try:
            condition = rule.rule_condition
            if isinstance(condition, str):
                import json
                condition = json.loads(condition)
            if evaluate_condition(condition, features):
                hits.append(RuleHitResult(
                    rule_id=rule.rule_id,
                    rule_name=rule.rule_name,
                    rule_category=rule.rule_category,
                    risk_level=rule.risk_level,
                    risk_score=rule.risk_score,
                    action=rule.action,
                    challengeable=bool(rule.challengeable),
                    trigger_reason=build_trigger_reason(rule, features),
                    description=rule.description,
                ))
        except Exception as exc:  # noqa: BLE001
            log.warning("规则 %s 求值失败，跳过：%s", rule.rule_id, exc)
    return hits


# ============================================================================
# 辅助
# ============================================================================
def next_rule_id(db: Session) -> str:
    """下一个规则编码 R-0NN（取库中最大序号 +1）。"""
    rows = db.scalars(select(RiskRule.rule_id)).all()
    seq = [int(r.split("-")[1]) for r in rows if "-" in r and r.split("-")[1].isdigit()]
    return f"R-{(max(seq) + 1 if seq else 1):03d}"


def rule_to_dict(rule: RiskRule) -> dict:
    """ORM → 响应字典。"""
    return {
        "rule_id": rule.rule_id,
        "rule_name": rule.rule_name,
        "rule_category": rule.rule_category,
        "event_type": rule.event_type,
        "rule_condition": rule.rule_condition,
        "risk_level": rule.risk_level,
        "risk_score": rule.risk_score,
        "action": rule.action,
        "challengeable": rule.challengeable,
        "priority": rule.priority,
        "is_enabled": rule.is_enabled,
        "version": rule.version,
        "description": rule.description,
        "updated_by": rule.updated_by,
        "update_time": rule.update_time.isoformat() if rule.update_time else None,
    }
