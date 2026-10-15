"""特征工程测试（SPEC §13.3.3 · TC-FE-01 ~ TC-FE-10）。

覆盖 33 个特征键完整性、四维度分布、团伙维度滑动窗口、哨兵值、边界条件。
"""

import pytest

from app.config import FEATURE_DIMENSIONS
from app.engine import step2_feature as fe


@pytest.fixture(autouse=True)
def _clean_windows():
    """每个用例前清空滑动窗口，保证相互独立。"""
    fe.reset_windows()
    yield
    fe.reset_windows()


# ---------------------------------------------------------------- 特征字典
def test_feature_dict_schema():
    """每条特征含 key/name/dimension/window/dtype/desc/source。"""
    required = {"key", "name", "dimension", "window", "dtype", "desc", "source"}
    assert all(required.issubset(set(f.keys())) for f in fe.FEATURE_DICT)


def test_feature_dict_count():
    assert len(fe.FEATURE_DICT) == 33


def test_feature_dict_dimensions():
    """四维度分布必须为 用户 14 / 订单 8 / 地址 3 / 团伙 8。"""
    counts = {}
    for f in fe.FEATURE_DICT:
        counts[f["dimension"]] = counts.get(f["dimension"], 0) + 1
    assert counts == {"用户": 14, "订单": 8, "地址": 3, "团伙": 8}


def test_feature_keys_unique():
    keys = [f["key"] for f in fe.FEATURE_DICT]
    assert len(keys) == len(set(keys))


def test_get_feature_dict_filter():
    assert len(fe.get_feature_dict("团伙")) == 8
    assert len(fe.get_feature_dict("用户")) == 14
    assert len(fe.get_feature_dict()) == 33


# ---------------------------------------------------------------- 团伙维度滑动窗口
def _event(minute_ago: float, **kw) -> dict:
    from datetime import timedelta

    from app.utils import now_dt, to_iso
    base = {
        "event_type": "order_create", "user_id": "U-1",
        "device_id": "DEV-1", "ip": "10.0.0.1",
        "receive_id": "RCV-1", "phone": "13800000001",
        "event_time": to_iso(now_dt() - timedelta(minutes=minute_ago)),
    }
    base.update(kw)
    return base


def test_device_account_cnt_counts_self():
    """SPEC §2.8 #1：目标事件计入自身特征（同设备第 3 个账号时计数为 3）。"""
    for i in range(3):
        fe.record_event(_event(30, user_id=f"U-{i}"))
    target = _event(1, user_id="U-2")
    fe.record_event(target)
    features = fe.compute_group_features(target)
    assert features["device_account_cnt_24h"] == 3.0


def test_device_order_cnt_1h():
    """真实管线顺序：先 record 目标事件 → 再计算（R-FE-02）。"""
    for i in range(4):
        fe.record_event(_event(20, user_id=f"U-{i}"))
    target = _event(1, user_id="U-9")
    fe.record_event(target)
    features = fe.compute_group_features(target)
    assert features["device_order_cnt_1h"] == 5.0        # 4 条历史 + 目标自身


def test_user_event_cnt_1h():
    for i in range(6):
        fe.record_event(_event(10, event_type="login", user_id="U-1"))
    target = _event(1, user_id="U-1")
    fe.record_event(target)
    features = fe.compute_group_features(target)
    assert features["user_event_cnt_1h"] == 7.0


def test_window_excludes_old_events():
    """超出窗口的事件不计入。"""
    fe.record_event(_event(120, user_id="U-1"))          # 2 小时前，超出 1h 窗口
    target = _event(1, user_id="U-1")
    fe.record_event(target)
    features = fe.compute_group_features(target)
    assert features["device_order_cnt_1h"] == 1.0        # 仅目标自身


def test_phone_refund_rate():
    """同手机号 10 单中 8 单退款 → 0.8（SPEC §2.8 #2）。

    构造：9 单下单 + 7 单退款，再 record 目标（第 10 单下单）与第 8 单退款。
    """
    for i in range(9):
        fe.record_event(_event(30, event_type="order_create", user_id=f"U-{i}"))
    for i in range(7):
        fe.record_event(_event(20, event_type="after_sale_apply", user_id=f"U-{i}"))
    fe.record_event(_event(10, event_type="order_create", user_id="U-9"))
    target = _event(1, event_type="after_sale_apply", user_id="U-9")
    fe.record_event(target)
    features = fe.compute_group_features(target)
    assert features["phone_refund_rate_24h"] == 0.8        # 8 / 10


def test_phone_refund_rate_zero_denominator():
    """分母为 0 返回 0.0（不报除零）。"""
    target = _event(1)
    fe.record_event(target)
    features = fe.compute_group_features(target)
    assert features["phone_refund_rate_24h"] == 0.0


def test_phone_refund_rate_capped_at_one():
    """退款率封顶 1.0（避免 >100%）。"""
    fe.record_event(_event(30, event_type="order_create"))
    for _ in range(5):
        fe.record_event(_event(20, event_type="after_sale_apply"))
    target = _event(1, event_type="after_sale_apply")
    fe.record_event(target)
    features = fe.compute_group_features(target)
    assert features["phone_refund_rate_24h"] == 1.0


def test_addr_account_cnt_7d_uses_longer_window():
    """地址用 7 天窗口，2 天前的事件仍计入（与设备 24h 口径不同，R-FE-06）。"""
    fe.record_event(_event(60 * 48, user_id="U-1"))      # 2 天前
    target = _event(1, user_id="U-1")
    fe.record_event(target)
    features = fe.compute_group_features(target)
    assert features["addr_account_cnt_7d"] == 1.0


def test_purge_clears_keys():
    """模板沙箱：purge 清掉旧数据后，窗口只剩本次事件（R-SM-07）。"""
    fe.record_event(_event(10))
    removed = fe.purge({"device": ["DEV-1"]})
    assert removed == 1
    target = _event(1)
    fe.record_event(target)
    features = fe.compute_group_features(target)
    assert features["device_order_cnt_1h"] == 1.0


# ---------------------------------------------------------------- 哨兵值与降级
def test_order_pay_interval_sentinel_without_order(db_session):
    """订单不存在时 order_pay_interval_sec = -1（哨兵值），不能是 0。

    若默认成 0.0 会命中规则 R-014「支付间隔过短」(0 < 10 且 >= 0) 造成误判。
    """
    features = fe.compute_order_features(db_session, "NOT-EXIST-ORDER")
    assert features["order_pay_interval_sec"] == -1.0


def test_compute_all_features_fills_all_keys(db_session):
    """compute_all_features 必须补齐 33 个键（缺失会导致规则静默不命中）。"""
    features = fe.compute_all_features(db_session, _event(1, user_id="U-FRESH"))
    assert set(features.keys()) == set(fe.FEATURE_KEYS)
    assert len(features) == 33


def test_compute_all_features_never_raises_on_missing_data(db_session):
    """查询不到业务数据时降级为 0，不抛异常（R-FE-03）。"""
    features = fe.compute_all_features(
        db_session, _event(1, user_id="U-NOT-EXIST", device_id="DEV-NOT-EXIST"))
    assert features["user_total_orders"] == 0.0
    assert features["user_refund_rate"] == 0.0
