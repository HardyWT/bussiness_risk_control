"""模块 2：特征计算引擎 —— 33 个特征 / 4 个维度。

对应文档：
- SPEC §2.3.1 用户维度 14 个（SQL 聚合业务表）
- SPEC §2.3.2 订单维度 8 个
- SPEC §2.3.3 地址维度 3 个
- SPEC §2.3.4 设备与团伙维度 8 个（进程内滑动窗口）
- SPEC §2.5   业务规则 R-FE-01 ~ R-FE-08

⚠ 关键约束：
- 团伙维度窗口以**事件时间戳**为基准，不用服务器当前时间（R-FE-01）
- 计算顺序为「**先写入窗口 → 再计算特征**」，目标事件计入自身特征（R-FE-02）
- 单特征超时返回默认值并告警，不阻断决策（R-FE-03 降级）
"""

import logging
import threading
import time
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import FEATURE_WINDOWS, settings
from ..utils import now_dt, parse_iso, round4, safe_div, to_float

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 特征字典（33 条）
FEATURE_DICT: list[dict] = [
    # ===== 用户维度（14）=====
    {"key": "user_total_orders", "name": "历史订单总数", "dimension": "用户", "window": "全量", "dtype": "int",
     "desc": "COUNT(order_info) WHERE user_id", "source": "order_info"},
    {"key": "user_orders_30d", "name": "近30天订单数", "dimension": "用户", "window": "30d", "dtype": "int",
     "desc": "COUNT WHERE create_time >= NOW()-30d", "source": "order_info"},
    {"key": "user_orders_7d", "name": "近7天订单数", "dimension": "用户", "window": "7d", "dtype": "int",
     "desc": "COUNT WHERE create_time >= NOW()-7d", "source": "order_info"},
    {"key": "user_total_amount", "name": "历史总消费额", "dimension": "用户", "window": "全量", "dtype": "float",
     "desc": "SUM(final_amount) JOIN order_info", "source": "order_detail + order_info"},
    {"key": "user_avg_order_amount", "name": "平均订单金额", "dimension": "用户", "window": "全量", "dtype": "float",
     "desc": "派生：total_amount / max(total_orders,1)", "source": "派生"},
    {"key": "user_max_order_amount", "name": "最大单笔订单金额", "dimension": "用户", "window": "全量", "dtype": "float",
     "desc": "子查询按订单聚合明细后取 MAX（避免多明细重复计算）", "source": "order_detail + order_info"},
    {"key": "user_refund_count", "name": "退款总次数", "dimension": "用户", "window": "全量", "dtype": "int",
     "desc": "COUNT(postsale WHERE postsale_type='退款')", "source": "postsale + order_detail + order_info"},
    {"key": "user_postsale_count", "name": "售后总次数", "dimension": "用户", "window": "全量", "dtype": "int",
     "desc": "COUNT(postsale) 不限类型", "source": "postsale + order_detail + order_info"},
    {"key": "user_refund_rate", "name": "退款率", "dimension": "用户", "window": "全量", "dtype": "float",
     "desc": "派生：refund_count / max(total_orders,1)，保留 4 位小数", "source": "派生"},
    {"key": "user_postsale_rate", "name": "售后率", "dimension": "用户", "window": "全量", "dtype": "float",
     "desc": "派生：postsale_count / max(total_orders,1)，保留 4 位小数", "source": "派生"},
    {"key": "user_refund_amount", "name": "退款总金额", "dimension": "用户", "window": "全量", "dtype": "float",
     "desc": "SUM(postsale.refund_amount)", "source": "postsale + order_detail + order_info"},
    {"key": "user_cancel_count", "name": "取消订单次数", "dimension": "用户", "window": "全量", "dtype": "int",
     "desc": "COUNT WHERE order_status='已取消'", "source": "order_info"},
    {"key": "user_complaint_count", "name": "投诉次数", "dimension": "用户", "window": "全量", "dtype": "int",
     "desc": "COUNT WHERE user_id", "source": "logistics_complaints_record"},
    {"key": "user_address_count", "name": "收货地址数量", "dimension": "用户", "window": "全量", "dtype": "int",
     "desc": "COUNT(receive_info) WHERE user_id", "source": "receive_info"},
    # ===== 订单维度（8）=====
    {"key": "order_total_amount", "name": "订单总金额", "dimension": "订单", "window": "单笔", "dtype": "float",
     "desc": "SUM(final_amount)", "source": "order_detail"},
    {"key": "order_item_count", "name": "明细行数", "dimension": "订单", "window": "单笔", "dtype": "int",
     "desc": "COUNT(*)", "source": "order_detail"},
    {"key": "order_sku_count", "name": "商品总件数", "dimension": "订单", "window": "单笔", "dtype": "int",
     "desc": "SUM(sku_count)", "source": "order_detail"},
    {"key": "order_discount_amount", "name": "折扣总额", "dimension": "订单", "window": "单笔", "dtype": "float",
     "desc": "SUM(discount_amount)", "source": "order_detail"},
    {"key": "order_discount_rate", "name": "折扣率", "dimension": "订单", "window": "单笔", "dtype": "float",
     "desc": "discount / (total + discount)，保留 4 位小数", "source": "order_detail"},
    {"key": "order_pay_interval_sec", "name": "下单到支付间隔(秒)", "dimension": "订单", "window": "单笔", "dtype": "int",
     "desc": "payment_time - create_time；**未支付返回 -1**（哨兵值）", "source": "order_info"},
    {"key": "order_is_night", "name": "是否夜间下单", "dimension": "订单", "window": "单笔", "dtype": "float",
     "desc": "create_time.hour ∈ [0,6) → 1.0，否则 0.0", "source": "order_info"},
    {"key": "order_category_count", "name": "商品类别数", "dimension": "订单", "window": "单笔", "dtype": "int",
     "desc": "COUNT(DISTINCT sku_category)", "source": "order_detail + sku_info"},
    # ===== 地址维度（3）=====
    {"key": "addr_total_count", "name": "地址总数", "dimension": "地址", "window": "全量", "dtype": "int",
     "desc": "COUNT(receive_info) WHERE user_id", "source": "receive_info"},
    {"key": "addr_province_count", "name": "不同省份数", "dimension": "地址", "window": "全量", "dtype": "int",
     "desc": "COUNT(DISTINCT receive_province)", "source": "receive_info"},
    {"key": "addr_is_new", "name": "是否新地址", "dimension": "地址", "window": "单笔", "dtype": "float",
     "desc": "该地址使用次数 ≤ 1 → 1.0；receive_id 为空返回 0.0", "source": "order_info + receive_info"},
    # ===== 设备与团伙维度（8）=====
    {"key": "device_account_cnt_24h", "name": "同设备关联账号数", "dimension": "团伙", "window": "24h", "dtype": "int",
     "desc": "按 device_id 去重 user_id 计数", "source": "滑动窗口 + risk_event"},
    {"key": "device_order_cnt_1h", "name": "同设备下单次数", "dimension": "团伙", "window": "1h", "dtype": "int",
     "desc": "按 device_id 统计 order_create 事件数", "source": "滑动窗口 + risk_event"},
    {"key": "device_coupon_cnt_1h", "name": "同设备领券次数", "dimension": "团伙", "window": "1h", "dtype": "int",
     "desc": "按 device_id 统计 coupon_receive 事件数", "source": "滑动窗口 + risk_event"},
    {"key": "ip_account_cnt_24h", "name": "同IP注册账号数", "dimension": "团伙", "window": "24h", "dtype": "int",
     "desc": "按 ip 去重 user_id 计数", "source": "滑动窗口 + risk_event"},
    {"key": "ip_order_cnt_1h", "name": "同IP下单次数", "dimension": "团伙", "window": "1h", "dtype": "int",
     "desc": "按 ip 统计 order_create 事件数", "source": "滑动窗口 + risk_event"},
    {"key": "addr_account_cnt_7d", "name": "同地址关联账号数", "dimension": "团伙", "window": "7d", "dtype": "int",
     "desc": "按 address_id 去重 user_id 计数（**7 天窗口，与设备 24h 口径不同**）", "source": "滑动窗口 + risk_event"},
    {"key": "phone_refund_rate_24h", "name": "同手机号退款率", "dimension": "团伙", "window": "24h", "dtype": "float",
     "desc": "after_sale_apply 数 ÷ order_create 数（分母 0 → 0.0，上限封顶 1.0）", "source": "滑动窗口 + risk_event"},
    {"key": "user_event_cnt_1h", "name": "单用户1小时事件总数", "dimension": "团伙", "window": "1h", "dtype": "int",
     "desc": "按 user_id 统计全部事件数", "source": "滑动窗口 + risk_event"},
]

FEATURE_KEYS = [f["key"] for f in FEATURE_DICT]
DIMENSION_OF = {f["key"]: f["dimension"] for f in FEATURE_DICT}

MAX_WINDOW_SECONDS = max(FEATURE_WINDOWS.values())


# ============================================================================
# 滑动窗口（团伙维度）
# ============================================================================
_lock = threading.Lock()
# { 维度: { 实体值: [(ts, event_type, user_id, phone)] } }
_windows: dict[str, dict[str, list[tuple]]] = {
    "device": {}, "ip": {}, "address": {}, "phone": {}, "user": {},
}
_max_ts: list[float] = [0.0]
_record_count = [0]


def reset_windows() -> None:
    """清空全部窗口（数据重置 / 测试用）。"""
    with _lock:
        for dim in _windows:
            _windows[dim].clear()
        _max_ts[0] = 0.0
        _record_count[0] = 0


def _event_ts(raw: dict) -> float:
    """事件时间戳（R-FE-01：以事件时间为准，缺省用当前时间）。"""
    dt = parse_iso(raw.get("event_time")) or now_dt()
    return dt.timestamp()


def record_event(raw: dict) -> None:
    """把事件写入各维度滑动窗口。**必须在计算特征之前调用**（R-FE-02）。"""
    ts = _event_ts(raw)
    row = (ts, raw.get("event_type"), raw.get("user_id"), raw.get("phone"))

    with _lock:
        for dim, value in (("device", raw.get("device_id")),
                           ("ip", raw.get("ip")),
                           ("address", raw.get("receive_id") or raw.get("address_id")),
                           ("phone", raw.get("phone")),
                           ("user", raw.get("user_id"))):
            if value:
                _windows[dim].setdefault(str(value), []).append(row)
        if ts > _max_ts[0]:
            _max_ts[0] = ts
        _record_count[0] += 1
        should_compact = _record_count[0] % 1000 == 0

    if should_compact:
        compact()


def _scan(dim: str, value, window_seconds: int, ref_ts: float,
          event_types: tuple[str, ...] | None = None) -> list[tuple]:
    """读取指定维度 + 窗口内的事件（**读时裁剪，不回写**）。

    窗口基准是被评估事件的 event_time（ref_ts），保证仿真 / 回放结果可复现。
    """
    if not value:
        return []
    since = ref_ts - window_seconds
    with _lock:
        rows = list(_windows[dim].get(str(value), []))
    rows = [r for r in rows if r[0] >= since]
    if event_types:
        rows = [r for r in rows if r[1] in event_types]
    return rows


def compact(max_ts: float | None = None) -> None:
    """按「最新事件时间 - 最大窗口」物理清理，抑制内存增长（R-FE-02 配套）。"""
    cutoff = (max_ts if max_ts is not None else _max_ts[0]) - MAX_WINDOW_SECONDS
    with _lock:
        for buckets in _windows.values():
            for key, rows in list(buckets.items()):
                kept = [r for r in rows if r[0] >= cutoff]
                if kept:
                    buckets[key] = kept
                else:
                    buckets.pop(key, None)


def purge(entries: dict[str, list[str]]) -> int:
    """清除指定维度下某些实体的窗口数据（仿真模板沙箱，R-SM-07）。"""
    removed = 0
    with _lock:
        for dim, values in entries.items():
            buckets = _windows.get(dim)
            if not buckets:
                continue
            for value in values:
                if buckets.pop(str(value), None) is not None:
                    removed += 1
    return removed


# ============================================================================
# 用户维度（14）
# ============================================================================
def compute_user_features(db: Session, user_id: str) -> dict[str, float]:
    """用户维度特征（Demo Doc-03 §3.1.2）。"""
    f: dict[str, float] = {k: 0.0 for k in FEATURE_KEYS if k.startswith("user_")}

    row = db.execute(text("""
        SELECT
            COUNT(*) AS total_orders,
            SUM(CASE WHEN create_time >= NOW() - INTERVAL 30 DAY THEN 1 ELSE 0 END) AS orders_30d,
            SUM(CASE WHEN create_time >= NOW() - INTERVAL 7 DAY  THEN 1 ELSE 0 END) AS orders_7d,
            SUM(CASE WHEN order_status = '已取消' THEN 1 ELSE 0 END) AS cancel_count
        FROM order_info WHERE user_id = :uid
    """), {"uid": user_id}).first()
    if row:
        f["user_total_orders"] = to_float(row.total_orders)
        f["user_orders_30d"] = to_float(row.orders_30d)
        f["user_orders_7d"] = to_float(row.orders_7d)
        f["user_cancel_count"] = to_float(row.cancel_count)

    row = db.execute(text("""
        SELECT COALESCE(SUM(od.final_amount), 0) AS total_amount
        FROM order_detail od JOIN order_info oi ON od.order_id = oi.order_id
        WHERE oi.user_id = :uid
    """), {"uid": user_id}).first()
    total_amount = to_float(row.total_amount) if row else 0.0
    f["user_total_amount"] = total_amount

    # 最大单笔订单金额：先按订单聚合明细，再取 MAX（易错点）
    row = db.execute(text("""
        SELECT COALESCE(MAX(order_total), 0) AS max_amount FROM (
            SELECT od.order_id, SUM(od.final_amount) AS order_total
            FROM order_detail od JOIN order_info oi ON od.order_id = oi.order_id
            WHERE oi.user_id = :uid GROUP BY od.order_id
        ) t
    """), {"uid": user_id}).first()
    f["user_max_order_amount"] = to_float(row.max_amount) if row else 0.0

    row = db.execute(text("""
        SELECT
            SUM(CASE WHEN p.postsale_type = '退款' THEN 1 ELSE 0 END) AS refund_count,
            COUNT(*) AS postsale_count,
            COALESCE(SUM(p.refund_amount), 0) AS refund_amount
        FROM postsale p
        JOIN order_detail od ON p.order_detail_id = od.order_detail_id
        JOIN order_info   oi ON od.order_id = oi.order_id
        WHERE oi.user_id = :uid
    """), {"uid": user_id}).first()
    if row:
        f["user_refund_count"] = to_float(row.refund_count)
        f["user_postsale_count"] = to_float(row.postsale_count)
        f["user_refund_amount"] = to_float(row.refund_amount)

    row = db.execute(text(
        "SELECT COUNT(*) AS c FROM logistics_complaints_record WHERE user_id = :uid"
    ), {"uid": user_id}).first()
    f["user_complaint_count"] = to_float(row.c) if row else 0.0

    row = db.execute(text(
        "SELECT COUNT(*) AS c FROM receive_info WHERE user_id = :uid"
    ), {"uid": user_id}).first()
    f["user_address_count"] = to_float(row.c) if row else 0.0

    # 派生特征（R-FE-04：分母为 0 返回 0.0）
    f["user_avg_order_amount"] = round4(safe_div(total_amount, f["user_total_orders"]))
    f["user_refund_rate"] = round4(safe_div(f["user_refund_count"], f["user_total_orders"]))
    f["user_postsale_rate"] = round4(safe_div(f["user_postsale_count"], f["user_total_orders"]))
    return f


# ============================================================================
# 订单维度（8）
# ============================================================================
def compute_order_features(db: Session, order_id: str) -> dict[str, float]:
    """订单维度特征（Demo Doc-03 §3.1.3）。特征 1~5 用一条 SQL 批量算。"""
    f: dict[str, float] = {k: 0.0 for k in FEATURE_KEYS if k.startswith("order_")}
    # ⚠ 哨兵值：订单缺失或未支付时 order_pay_interval_sec 必须是 **-1**（SPEC §2.3.2）。
    # 若默认成 0.0，会满足规则 R-014「支付间隔过短」(0 < 10 且 >= 0) 造成大规模误命中。
    f["order_pay_interval_sec"] = -1.0
    if not order_id:
        return f

    row = db.execute(text("""
        SELECT
            COALESCE(SUM(od.final_amount), 0)     AS total_amount,
            COUNT(*)                               AS item_count,
            COALESCE(SUM(od.sku_count), 0)         AS sku_count,
            COALESCE(SUM(od.discount_amount), 0)   AS discount_amount,
            COUNT(DISTINCT s.sku_category)         AS category_count
        FROM order_detail od
        LEFT JOIN sku_info s ON od.sku_id = s.sku_id
        WHERE od.order_id = :oid
    """), {"oid": order_id}).first()
    if row:
        f["order_total_amount"] = to_float(row.total_amount)
        f["order_item_count"] = to_float(row.item_count)
        f["order_sku_count"] = to_float(row.sku_count)
        f["order_discount_amount"] = to_float(row.discount_amount)
        f["order_category_count"] = to_float(row.category_count)
        original = to_float(row.total_amount) + to_float(row.discount_amount)
        f["order_discount_rate"] = round4(safe_div(to_float(row.discount_amount), original))

    info = db.execute(text(
        "SELECT create_time, payment_time FROM order_info WHERE order_id = :oid"
    ), {"oid": order_id}).first()
    if info:
        create_time = info.create_time
        # 未支付返回 -1 哨兵值
        f["order_pay_interval_sec"] = (
            float((info.payment_time - create_time).total_seconds())
            if info.payment_time else -1.0
        )
        # 夜间定义 0:00–5:59
        f["order_is_night"] = 1.0 if create_time and 0 <= create_time.hour < 6 else 0.0
    return f


# ============================================================================
# 地址维度（3）
# ============================================================================
def compute_address_features(db: Session, user_id: str, receive_id: str | None) -> dict[str, float]:
    """地址维度特征（Demo Doc-03 §3.1.4）。"""
    f: dict[str, float] = {k: 0.0 for k in FEATURE_KEYS if k.startswith("addr_")}

    row = db.execute(text("""
        SELECT COUNT(*) AS total_count,
               COUNT(DISTINCT receive_province) AS province_count
        FROM receive_info WHERE user_id = :uid
    """), {"uid": user_id}).first()
    if row:
        f["addr_total_count"] = to_float(row.total_count)
        f["addr_province_count"] = to_float(row.province_count)

    if receive_id:
        row = db.execute(text(
            "SELECT COUNT(*) AS c FROM order_info WHERE user_id = :uid AND receive_id = :rid"
        ), {"uid": user_id, "rid": receive_id}).first()
        f["addr_is_new"] = 1.0 if (row and to_float(row.c) <= 1) else 0.0
    else:
        f["addr_is_new"] = 0.0
    return f


# ============================================================================
# 设备与团伙维度（8）
# ============================================================================
def compute_group_features(raw: dict) -> dict[str, float]:
    """团伙维度特征（SPEC §2.3.4）。⚠ 调用前必须已 record_event。"""
    ref_ts = _event_ts(raw)
    device_id = raw.get("device_id")
    ip = raw.get("ip")
    address = raw.get("receive_id") or raw.get("address_id")
    phone = raw.get("phone")
    user_id = raw.get("user_id")

    dev_24h = _scan("device", device_id, 24 * 3600, ref_ts)
    ip_24h = _scan("ip", ip, 24 * 3600, ref_ts)
    addr_7d = _scan("address", address, 7 * 86400, ref_ts)
    phone_24h = _scan("phone", phone, 24 * 3600, ref_ts)
    user_1h = _scan("user", user_id, 3600, ref_ts)

    sold = len([r for r in phone_24h if r[1] == "order_create"])
    refunded = len([r for r in phone_24h if r[1] == "after_sale_apply"])

    return {
        "device_account_cnt_24h": float(len({r[2] for r in dev_24h if r[2]})),
        "device_order_cnt_1h": float(len(_scan("device", device_id, 3600, ref_ts, ("order_create",)))),
        "device_coupon_cnt_1h": float(len(_scan("device", device_id, 3600, ref_ts, ("coupon_receive",)))),
        "ip_account_cnt_24h": float(len({r[2] for r in ip_24h if r[2]})),
        "ip_order_cnt_1h": float(len(_scan("ip", ip, 3600, ref_ts, ("order_create",)))),
        "addr_account_cnt_7d": float(len({r[2] for r in addr_7d if r[2]})),
        # 分母为 0 → 0.0；上限封顶 1.0（避免 >100% 的退款率）
        "phone_refund_rate_24h": round4(min(safe_div(refunded, sold), 1.0)),
        "user_event_cnt_1h": float(len(user_1h)),
    }


# ============================================================================
# 统一编排
# ============================================================================
def compute_all_features(db: Session, raw: dict) -> dict[str, float]:
    """计算全部 33 个特征（SPEC §2.3 编排函数）。

    ⚠ 调用前必须已 `record_event(raw)`，否则团伙维度不含当前事件。
    """
    t0 = time.perf_counter()
    user_id = raw.get("user_id")
    order_id = raw.get("order_id") or (
        raw.get("source_id") if raw.get("event_type") in ("order_create", "order_pay") else None
    )
    receive_id = raw.get("receive_id")

    features: dict[str, float] = {}
    for name, fn in (
        ("用户", lambda: compute_user_features(db, user_id)),
        ("地址", lambda: compute_address_features(db, user_id, receive_id)),
        ("团伙", lambda: compute_group_features(raw)),
    ):
        section_t0 = time.perf_counter()
        try:
            features.update(fn())
        except Exception as exc:  # noqa: BLE001  R-FE-03 降级：单维度失败不阻断
            log.warning("特征维度「%s」计算失败，降级为空值：%s", name, exc)
        elapsed_ms = (time.perf_counter() - section_t0) * 1000
        if elapsed_ms > settings.FEATURE_TIMEOUT_MS:
            log.warning("特征维度「%s」计算耗时 %.0fms 超过阈值 %dms",
                        name, elapsed_ms, settings.FEATURE_TIMEOUT_MS)

    if order_id:
        try:
            features.update(compute_order_features(db, order_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("订单维度特征计算失败：%s", exc)

    # 补齐缺失键，保证 33 个键齐全（缺失会导致规则求值静默不命中）
    for key in FEATURE_KEYS:
        features.setdefault(key, 0.0)

    cost_ms = (time.perf_counter() - t0) * 1000
    if cost_ms > settings.FEATURE_TOTAL_BUDGET_MS:
        log.warning("特征计算总耗时 %.0fms 超过预算 %dms（G-05）",
                    cost_ms, settings.FEATURE_TOTAL_BUDGET_MS)
    return features


# ============================================================================
# 历史基线（FR-26 对比视图）
# ============================================================================
def baseline_features(db: Session, exclude_event_id: str | None = None, days: int = 7) -> dict[str, float]:
    """近 N 天全量特征快照的均值（排除当前事件，避免基线被异常值抬高）。"""
    since = now_dt() - timedelta(days=days)
    rows = db.execute(text("""
        SELECT feature_name, AVG(feature_value) AS avg_value
        FROM risk_feature
        WHERE compute_time >= :since AND (:eid IS NULL OR event_id <> :eid)
        GROUP BY feature_name
    """), {"since": since, "eid": exclude_event_id}).all()
    out = {r.feature_name: round4(r.avg_value) for r in rows}
    for key in FEATURE_KEYS:
        out.setdefault(key, 0.0)
    return out


def get_feature_dict(dimension: str | None = None) -> list[dict]:
    """特征字典（GET /features/dict，FR-10）。"""
    if dimension:
        return [f for f in FEATURE_DICT if f["dimension"] == dimension]
    return list(FEATURE_DICT)


def warmup_from_db(db: Session) -> int:
    """启动预热：把库中历史事件回灌进滑动窗口，保证重启后特征连续。"""
    from ..models import RiskEvent

    reset_windows()
    horizon = now_dt() - timedelta(seconds=MAX_WINDOW_SECONDS)
    rows = db.query(RiskEvent).filter(RiskEvent.create_time >= horizon).all()
    for r in rows:
        record_event({
            "event_type": r.event_type,
            "user_id": r.user_id,
            "device_id": r.device_id,
            "ip": r.ip,
            "receive_id": r.address_id,
            "phone": r.phone,
            "event_time": (r.event_time or r.create_time).isoformat() if (r.event_time or r.create_time) else None,
        })
    return len(rows)
