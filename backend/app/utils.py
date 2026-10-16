"""公共工具：ULID 生成、时间处理、脱敏、JSON 序列化。

对应文档：
- Demo Doc-03 §3.3.2 / §8.4  ID 生成策略（ULID + 前缀）
- SPEC R-EV-05 / R-FE2-03    手机号脱敏

⚠ 本模块只保留**有调用方**的工具函数。v3.1 删除了 8 个从未被引用的导出
（`today_str` / `bump_rule_seq` / `_next_seq` 及其日期计数器、`mask_value` /
`json_text` / `to_int` / `to_json`）—— 它们是为已删除的平行实现预留的，
留着会让人误以为系统里有「当日事件序号」这类并不存在的机制。
"""

import json
import re
import threading
from datetime import datetime, timedelta, timezone

import ulid

CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 时间
def now_dt() -> datetime:
    """当前时间（东八区，naive，与 MySQL DATETIME 对齐）。"""
    return datetime.now(CST).replace(tzinfo=None)


def now_iso() -> str:
    """ISO8601 字符串（秒精度），用于响应体。"""
    return datetime.now(CST).replace(microsecond=0).isoformat()


def to_iso(dt: datetime | None) -> str | None:
    """naive datetime → 带 +08:00 的 ISO8601 字符串。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    """解析 ISO8601 字符串为 naive datetime（用于 DB 查询）。

    ⚠ 解析失败返回 `None`。调用方在拼 `WHERE` 条件时**必须**对 `None` 兜底
    （如 `parse_iso(x) or datetime.min`），否则会把 `None` 拼进 SQL 得到
    `create_time >= NULL`，求值恒为 NULL → 静默返回空列表，看起来像
    「这段时间确实没有数据」。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(CST).replace(tzinfo=None) if dt.tzinfo else dt
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- ULID
def new_id(prefix: str = "") -> str:
    """`{prefix}{ulid小写}`（Demo Doc-03 §3.3.2）。

    前缀约定：evt 事件 / ast 评估 / cas 案件 / sess 会话 / msg 消息 / dsp 处置
    """
    return f"{prefix}{ulid.new().str.lower()}"


def new_event_id() -> str:
    """事件编号：evt + ULID。"""
    return new_id("evt")


def new_assessment_id() -> str:
    """评估编号：ast + ULID。"""
    return new_id("ast")


def new_case_id() -> str:
    """案件编号：cas + ULID。"""
    return new_id("cas")


def new_session_id() -> str:
    """AI 会话编号：sess + ULID。"""
    return new_id("sess")


# ---------------------------------------------------------------- 案件短号
_case_no_lock = threading.Lock()
_case_no_cache: list[int] = [0]


def next_case_no(db) -> int:
    """分配案件对外短号（`C-1024` 里的那个 1024）。

    ⚠ 实现要点：用**进程内缓存 + 数据库兜底**，而不是每建一件都 `SELECT MAX()`。
      回放一次要建 300+ 件案件，每件一次 `MAX()` 就是 300+ 次额外查询。
      缓存为空时（进程刚启动 / 首次调用）回库取一次最大值。
      多进程部署会各自分配，短号可能重复 —— 演示环境单进程，且短号只用于展示，
      真要有冲突也不影响任何业务逻辑（唯一性由 ULID 主键保证）。
    """
    with _case_no_lock:
        if _case_no_cache[0] == 0:
            from sqlalchemy import func

            from .models import RiskCase

            _case_no_cache[0] = int(db.query(func.max(RiskCase.case_no)).scalar() or 0)
        _case_no_cache[0] += 1
        return _case_no_cache[0]


def reset_case_no_cache() -> None:
    """清空短号缓存（数据重置、测试隔离用）。"""
    with _case_no_lock:
        _case_no_cache[0] = 0


def case_no_label(case_no: int | None, fallback: str = "") -> str:
    """短号 → 展示串；没有短号时回退（历史数据 / 兜底）。"""
    return f"C-{case_no}" if case_no else fallback


# ---------------------------------------------------------------- 脱敏
def mask_phone(phone: str | None) -> str | None:
    """手机号脱敏：138****1234（R-EV-05 / R-FE2-03）。"""
    if not phone:
        return phone
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) == 11:
        return f"{digits[:3]}****{digits[7:]}"
    if len(digits) > 7:
        return f"{digits[:3]}****{digits[-4:]}"
    return "****"


# ---------------------------------------------------------------- JSON
def from_json(value):
    """把 JSON 列的值统一成 dict / list（列本身已是 dict/list 时原样返回）。

    MySQL 的 JSON 列经 SQLAlchemy 读出来通常已是 Python 对象；
    本函数同时兼容「存的是字符串」的历史数据与旧驱动的返回。
    """
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- 数值
def to_float(value, default: float = 0.0) -> float:
    """安全转 float，失败返回 default。"""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def round4(value: float | None) -> float:
    """比率类特征保留 4 位小数（Demo Doc-03 §3.1.2）。"""
    return round(to_float(value), 4)


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    """安全除法：分母为 0 返回默认值（R-FE-04）。"""
    return numerator / denominator if denominator else default
