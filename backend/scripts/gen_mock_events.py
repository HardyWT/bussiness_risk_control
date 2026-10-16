"""gen_mock_events.py —— 生成模拟电商风险事件流数据集（SPEC §10.3）。

产出 `data/mock_events.json`，**≥ 1200 条**：

| 类别 | 数量 | 特征 | 预期结果 |
|------|-----:|------|---------|
| 正常用户事件 | 900 | 各特征正常，**按作息节律**分布（晚 19~23 峰 / 凌晨谷） | Pass / Mark |
| 风险画像样本 | 50 | 10 类画像各 5 人（高频下单 / 高频售后 / 频繁取消 / 高额退款 / 多地址跨省 / 批发扫货 / 大额折扣 / 秒付 / 夜间大额 / 零订单新号） | 覆盖此前 15 条零命中规则 |
| 羊毛党领券+下单 | 186 | 同设备多账号、高频领券（10 次作案窗口） | Mark / Challenge / Reject |
| 恶意退款 | 108 | 高退款率 + 同地址聚集（6 次作案，三种口味） | Review / Reject |
| 黄牛囤货 | 216 | 高频大额下单（18 次作案） | Reject |
| 物流投诉异常 | 40 | **5 个惯投诉账号**轮流出场，主账号每轮密集 9 条 | Mark（R-022 / R-031） |
| 边界样本 | 360 | 120 目标 + 240 上下文，10 批各占一天 | 验证阈值 |

每条事件带 `event_time`，按时间序灌入保证特征计算正确。
每条事件都带 `event_data` **事件快照**（各类型的扩展字段，见 `snapshot_for`）。
固定随机种子，**可复现**。

用法：
    python scripts/gen_mock_events.py
"""

import json
import os
import random
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
# 画像分组 / 投诉集中账号与业务数据生成器共用同一份（单一事实来源）
from scripts.gen_business_data import COMPLAINER_USERS, PERSONA_GROUPS  # noqa: E402
from app.utils import now_dt, to_iso  # noqa: E402

# Windows 控制台默认 GBK，打印 ✓ / ✗ / ⚠ 会抛 UnicodeEncodeError。
# 统一把标准输出强制为 UTF-8（与 run.py / start.py 一致）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


SEED = 20261014
random.seed(SEED)
NOW = now_dt()

OUT_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "mock_events.json")

GOODS = ["数码配件", "无线耳机", "机械键盘", "智能手表", "游戏主机", "显示器", "扫地机器人"]
REFUND_RING_ADDRESS = "RCV-XS-01"


# ---------------------------------------------------------------- 时间轴设定
#
# ⚠ 为什么要有「作案窗口」这个概念（v3.1 重做）：
#
#   风控的团伙特征都是**滑窗统计** —— `device_coupon_cnt_1h` / `device_order_cnt_1h` /
#   `addr_account_cnt_7d` / `phone_refund_rate_24h` 全部依赖「同一设备/地址在一段时间内
#   聚集了多少个账号/多少次事件」。所以作弊行为**必须**在窗口内密集发生，
#   否则那些规则一条都不会命中。
#
#   但 v3.0 的实现走偏了：它把「密集」理解成「全都发生在这一个小时里」——
#   羊毛党 240 条、黄牛 60 条、边界样本 120 条**全部落在最后 1 小时**，
#   于是 10-15 这一天有 371 条评估，而其他天只有 20~40 条：
#     · 大盘趋势图在最后一天陡直拉起（「曲线一路飙升」）
#     · 指标卡的「较昨日」变成荒谬的数字（↑ 1892.3%）
#     · 处理量与案件数也全挤在同一天，看起来像刷出来的
#
#   正确形态是「**多次作案窗口分散在 90 天内**」：
#   每个团伙周期性出手若干次，每次只持续 1~3 小时、在自己的窗口内密集操作。
#   这样既满足滑窗特征的密集性要求，又让时间轴均匀 —— 这才是真实作弊的节奏
#   （团伙会在不同时间点反复动手，而不是集中在某一小时）。
DAYS_SPAN = 90
_MIN = 24 * 60


def _raid_start(span_days: tuple[int, int] = (0, DAYS_SPAN - 3)) -> float:
    """随机一个「作案窗口」的起始时刻（返回距当前的分钟数）。"""
    return random.uniform(span_days[0] * _MIN, span_days[1] * _MIN)


def _in_raid(start_min: float, dur_min: float) -> float:
    """窗口内随机一个时刻（距当前的分钟数）。"""
    return start_min + random.uniform(0, dur_min)


def _raid_starts(n: int, span_days: tuple[int, int] = (0, DAYS_SPAN - 3),
                 dur_min: float = 90) -> list[float]:
    """生成 n 个互不重叠的窗口起点（按天分散）。

    互不重叠很重要：两个窗口挨在一起会把两次作案并成一次，
    `device_account_cnt_24h` 之类的 24h 特征会跨窗口累加，判定就不再是「一次作案一个结果」。
    """
    days = random.sample(range(span_days[0], span_days[1] + 1), n)
    starts = []
    for d in days:
        # 每次作案的时刻也随机（白天/深夜都有）；第 0 天只取「今天已过去的部分」，
        # 否则会造出「未来时间」的事件。
        span = (_MIN - dur_min - 5) if d > 0 else max(NOW.hour * 60 - dur_min - 5, 10)
        starts.append(d * _MIN + random.uniform(0, max(span, 10)))
    return starts


def ts(minutes_ago: float) -> str:
    return to_iso(NOW - timedelta(minutes=minutes_ago))


# ---------------------------------------------------------------- 天内作息节律
#
# ⚠ 为什么要有这张权重表（v3.1 二次加固）：
#   正常流量原来取 `random.uniform(2, 89 天)` —— 在 90 天 × 24 小时上**均匀**。
#   于是任何「按小时」的统计/图表都是一条平线：凌晨 3 点和晚上 8 点的事件量一样多。
#   真实的电商流量不是这样，晚 19~23 点是下单高峰、凌晨 0~5 点是深谷。
#   数据量、天数、类别都够，但**节奏还是假的** —— 与 §8.7.2 修掉的
#   「节奏假」是同一类问题，只是那次修的是「天」，这次修的是「小时」。
#
# ⚠ 只改**天内**的分布，**天的粒度仍然均匀**（≈10/天，SPEC §10.3 的硬要求）。
#   做法：先等概率抽一天，再按权重抽该天的小时 —— 两层独立，日总量不受影响。
#
# ⚠ 团伙的「作案窗口」（`_raid_starts`）**不动**：它们的窗口内密集是滑窗特征的
#   硬要求（1h/24h/7d 统计），按作息去摊开会让 R-005/R-008 之类的规则整批失效。
_HOUR_WEIGHTS = [
    0.18, 0.10, 0.07, 0.06, 0.07, 0.12,   # 00-05 凌晨：深谷
    0.45, 0.75, 0.95, 1.00, 1.05, 1.10,   # 06-11 上午：爬坡到平稳
    1.15, 0.95, 0.90, 0.95, 1.05, 1.20,   # 12-17 午后：午休小回落
    1.45, 1.60, 1.70, 1.55, 1.30, 0.75,   # 18-23 晚间：20-22 为全天峰
]
_WEEKEND_FACTOR = 1.15                    # 周末整体略高于工作日


def _daily_minute_ago() -> float:
    """在「刚刚 ~ 89 天前」区间内按**作息节律**取一个时刻（覆盖 90 个自然日）。

    返回「距现在的分钟数」（与 `ts()` 的入参同一口径）。
    """
    # ① 先等概率抽「多少天前」——保证天的粒度均匀（≈10/天）。
    #   ⚠ 必须取到 `DAYS_SPAN-1`：写成 `DAYS_SPAN-2` 会让时间轴只覆盖 89 天，
    #     与 SPEC §10.3「跨度 90 天」不符（实测被 data_health 的「覆盖天数」抓出来）。
    day_ago = random.randint(0, DAYS_SPAN - 1)
    base = NOW - timedelta(days=day_ago)

    # ② 再按作息权重抽该天的小时。
    #   今天（day_ago==0）**只能取已经过去的小时** —— 抽到「今天稍晚的小时」会造出
    #   未来时刻的事件：大盘「今日评估量」会比实际发生的还多，且与当前时间自相矛盾。
    hours, weights = list(range(24)), list(_HOUR_WEIGHTS)
    if day_ago == 0:
        cutoff = max(NOW.hour, 1)
        hours, weights = hours[:cutoff], weights[:cutoff]
    hour = random.choices(hours, weights=weights, k=1)[0]

    # 周末系数：落在周末的日子整体上浮，做法是把抽到的小时向晚间（峰值区）偏移
    if base.weekday() >= 5 and random.random() < (_WEEKEND_FACTOR - 1.0) * 2:
        hour = max(hour, random.randint(18, 22))

    moment = base.replace(hour=min(hour, 23), minute=random.randint(0, 59),
                          second=random.randint(0, 59), microsecond=0)
    # ③ 兜底：周末上浮仍可能把「今天」的时刻顶到未来，退回今天已过去的某一刻。
    #   （不夹到同一个固定值 —— 那会在时间轴上留下一个几点都重合的小尖峰）
    if moment >= NOW:
        moment = NOW - timedelta(minutes=random.uniform(1, 45))

    minutes = (NOW - moment).total_seconds() / 60
    return min(max(minutes, 0.5), DAYS_SPAN * _MIN)


# ================================================================ 业务单据绑定
#
# ⚠ **为什么事件必须绑定真实单据**（v3.1 新增）：
#   事件的 `source_id` 是订单号 / 售后单号 / 投诉记录号。风控在
#   `enrich_request()` 里靠它反查 `order_info` → `order_detail`，而
#   **8 个订单维度特征**（金额、明细行数、件数、折扣率、支付间隔、夜间、类别数）
#   与事件金额**全部来自这次反查**。
#
#   若 `source_id` 是编造的（如 `O-N100424`），反查必然落空：
#     · 8 个订单维度特征恒为 0；`order_pay_interval_sec` 恒为 -1
#     · 依赖它们的规则（R-002/R-003/R-011/R-015~R-018/R-025/R-028）**永不命中**
#     · `risk_event.amount` 恒为 0 → 「挽回资损」（PRD FR-46）永远显示 ¥0
#     · 案件详情「当前订单」为空、「拦截订单」处置返回「订单不存在」
#
#   数据看起来完整、页面也不报错，但整块功能是死的 —— 属最难发现的一类问题。
#   所以这里先把业务库的真实单据读进内存，生成事件时**轮询绑定**到该用户名下的
#   真实记录；确实没有业务记录的账号，保留合成 ID 并计数上报（见 main 的告警）。
class _Binder:
    """把事件绑定到真实业务单据；同一用户的多条事件按顺序轮询其名下记录。"""

    def __init__(self, index: dict):
        self.index = index
        self.cursor: dict[tuple[str, str], int] = {}
        self.unbound = {"order": 0, "postsale": 0, "complaint": 0}
        # 售后事件退化为订单号的数量（source_id 的类型与事件类型不符，
        # 下游靠 enrich_request 的容错分支处理，详见 postsale() 的说明）
        self.via_order = {"postsale": 0}

    def _next(self, kind: str, user_id: str):
        rows = self.index.get(kind, {}).get(user_id) or []
        if not rows:
            return None
        key = (kind, user_id)
        i = self.cursor.get(key, 0)
        self.cursor[key] = i + 1
        return rows[i % len(rows)]

    def order(self, user_id: str, fallback_id: str, fallback_amount: float):
        """返回 (order_id, amount)。"""
        hit = self._next("orders", user_id)
        if hit is None:
            self.unbound["order"] += 1
            return fallback_id, fallback_amount
        order_id, amount = hit
        return order_id, (amount if amount > 0 else fallback_amount)

    def postsale(self, user_id: str, fallback_id: str, fallback_amount: float):
        """售后事件绑定为 (source_id, amount)。

        ⚠ **两种语义，必须让下游知道**（v3.1 修正）：
          · 该用户有售后单 → 返回**售后单号**（`PS-xxxxx`），符合 `source_id` 的约定；
          · 该用户没有售后单 → 返回**订单号**（`O-xxxxxx`）。
            这时 `source_id` 装的是订单号，不再是售后单号 ——
            下游若死板地按 `postsale.postsale_id = source_id` 查必然落空。
            `enrich_request()` 已经能识别这种输入（按订单号补全），但这是个
            **类型不一致**的输入，所以单独计数上报，不混在「未绑定」里。
        """
        hit = self._next("postsales", user_id)
        if hit is None:
            self.via_order["postsale"] += 1
            # 该用户没有售后单 → 退化为「绑定到其订单」（售后事件的金额取自订单）
            return self.order(user_id, fallback_id, fallback_amount)
        postsale_id, amount = hit
        return postsale_id, (amount if amount > 0 else fallback_amount)

    def complaint(self, user_id: str, fallback_id: str) -> str:
        hit = self._next("complaints", user_id)
        if hit is None:
            self.unbound["complaint"] += 1
            return fallback_id
        return str(hit)


_BIND: "_Binder | None" = None


def bind_order(user_id: str, fallback_id: str, fallback_amount: float):
    """订单类事件：绑定真实订单号与真实金额。"""
    if _BIND is None:
        return fallback_id, fallback_amount
    return _BIND.order(user_id, fallback_id, fallback_amount)


def bind_postsale(user_id: str, fallback_id: str, fallback_amount: float):
    """售后类事件：绑定真实售后单号与真实金额。"""
    if _BIND is None:
        return fallback_id, fallback_amount
    return _BIND.postsale(user_id, fallback_id, fallback_amount)


def bind_complaint(user_id: str, fallback_id: str) -> str:
    """投诉类事件：绑定真实投诉记录号（`record_id` 是自增整数）。"""
    if _BIND is None:
        return fallback_id
    return _BIND.complaint(user_id, fallback_id)


def load_business_index() -> dict:
    """把业务库的真实单据读进内存（订单 / 售后 / 投诉，按用户分组）。"""
    from sqlalchemy import text

    from app.database import session_scope

    index: dict[str, dict[str, list]] = {"orders": {}, "postsales": {}, "complaints": {}}
    with session_scope() as db:
        for uid, oid, amount in db.execute(text("""
            SELECT oi.user_id, oi.order_id, COALESCE(SUM(od.final_amount), 0) AS amount
            FROM order_info oi
            LEFT JOIN order_detail od ON od.order_id = oi.order_id
            GROUP BY oi.user_id, oi.order_id
            ORDER BY oi.user_id, oi.create_time DESC
        """)).all():
            index["orders"].setdefault(uid, []).append((oid, float(amount or 0)))

        for uid, pid, amount in db.execute(text("""
            SELECT oi.user_id, p.postsale_id, COALESCE(p.refund_amount, 0) AS amount
            FROM postsale p
            JOIN order_detail od ON p.order_detail_id = od.order_detail_id
            JOIN order_info   oi ON od.order_id = oi.order_id
            ORDER BY oi.user_id
        """)).all():
            index["postsales"].setdefault(uid, []).append((pid, float(amount or 0)))

        for uid, rid in db.execute(text(
                "SELECT user_id, record_id FROM logistics_complaints_record")).all():
            index["complaints"].setdefault(uid, []).append(rid)
    return index


# ================================================================ 事件快照（event_data）
#
# ⚠ **快照里只放「业务系统随事件报文带上、但业务表里查不到的」上下文**：
#   渠道 / 支付方式 / 活动号 / 登录场景 之类。
#
#   金额、明细行数、件数、品类数、折扣率、支付间隔**一律不放进快照** —— 那些由
#   `enrich_request()` 从 `order_info` / `order_detail` 反查算出（SPEC §10.3.1），
#   在快照里再抄一份，迟早会出现「快照写着 5 行、特征算出 3 行」的自相矛盾数据，
#   而且这种矛盾不会报错，只会在演示时被人一眼看穿。
#
# ⚠ 以前 `order_create` / `order_pay` 的快照是**全 null**（796 + 204 = 1000 条，
#   占数据集 54%）：早期只给 login / 领券 / 售后 / 投诉写了快照，把金额与订单维度
#   特征全靠的这两类漏掉了。而 `event_data` 只是审计与展示字段，不参与任何特征
#   计算与规则判定（链路上仅透传存储），所以补齐它**不会改变任何判定结果**。
CHANNELS = ("app", "h5", "mini", "pc")
ORDER_CHANNELS = ("app", "h5", "mini", "pc", "coupon_page")
PAY_METHODS = ("alipay", "wechat", "unionpay", "balance")
PAY_CHANNELS = ("cashier", "quick_pay", "withhold")
PAY_SCENES = ("order_center", "order_detail", "push")
COUPON_TYPES = ("platform", "shop", "new_user", "activity")
REFUND_TYPES = ("refund_only", "return_refund", "exchange")
# 投诉文案与投诉类型必须**成对**取，不能各自随机 —— 否则会出现
# 「complaint=包裹丢失 但 complaint_type=delivery_delay」这种对不上的快照
COMPLAINT_PAIRS = (
    ("配送延迟", "delivery_delay"),
    ("包裹丢失", "package_lost"),
    ("虚假签收", "fake_sign"),
    ("商品破损", "damaged"),
)


def _activity_id() -> str:
    return f"ACT-2026-{random.randint(1, 40):03d}"


def snapshot_for(event_type: str, provided: dict | None = None) -> dict:
    """某类事件的默认快照字段。

    `provided` 是调用方已经显式给出的键（如 `refund_reason`）——
    默认值要**参考**它，否则会造出互相矛盾的快照
    （典型：注册事件配上 `result=fail`）。
    """
    provided = provided or {}

    if event_type == "order_create":
        return {"channel": random.choice(ORDER_CHANNELS),
                "pay_method": random.choice(PAY_METHODS),
                "activity_id": _activity_id()}

    if event_type == "order_pay":
        return {"pay_method": random.choice(PAY_METHODS),
                "pay_channel": random.choice(PAY_CHANNELS),
                "pay_scene": random.choice(PAY_SCENES)}

    if event_type == "login":
        login_type = provided.get("login_type") or "login"
        return {
            "login_type": login_type,
            "channel": provided.get("channel") or random.choice(CHANNELS),
            # 只有「登录」才可能失败（撞库）；「注册」不存在失败这一说
            "result": "success" if login_type == "register" else
                      random.choice(("success",) * 19 + ("fail",)),
            "login_scene": "first_login" if login_type == "register" else
                           random.choice(("routine", "account_switch", "device_change")),
        }

    if event_type == "coupon_receive":
        return {"coupon_type": random.choice(COUPON_TYPES),
                "activity_id": _activity_id(),
                "channel": random.choice(CHANNELS)}

    if event_type == "after_sale_apply":
        return {"refund_type": random.choice(REFUND_TYPES),
                "evidence_provided": random.random() < 0.45,
                "apply_channel": random.choice(CHANNELS)}

    if event_type == "logistics_complaint":
        text, code = random.choice(COMPLAINT_PAIRS)
        return {"complaint": text, "complaint_type": code,
                "claim_compensation": random.random() < 0.5,
                "channel": random.choice(CHANNELS)}

    return {}


def _base(event_id: str, event_type: str, user_id: str, minute: float, **kw) -> dict:
    event = {"event_id": event_id, "event_type": event_type,
             "user_id": user_id, "event_time": ts(minute)}
    event.update({k: v for k, v in kw.items() if v is not None})
    # 快照 = 该类型的默认字段，再盖上调用方显式给的值。
    # 放在 `_base` 里而不是各调用点，是为了让**新增场景自动获得快照** ——
    # 否则又会出现「某个新场景忘了写 event_data，那一批全是 null」。
    snap = snapshot_for(event_type, event.get("event_data"))
    snap.update(event.get("event_data") or {})
    if snap:
        event["event_data"] = snap
    return event


# ================================================================ 正常流量 900
def gen_normal() -> list[dict]:
    events = []
    for i in range(900):
        seq = f"E-N{i:04d}"
        user_id = f"U-{1000 + (i % 170) + 1}"
        device = f"DEV-N{(i % 170):04d}"
        ip = f"192.168.{random.randint(0, 8)}.{random.randint(2, 250)}"
        addr = f"RCV-{(i % 300) + 1:04d}"
        phone = f"13{600000000 + (i % 170)}"[:11]

        roll = random.random()
        if roll < 0.20:
            et = "login"
            kw = {"device_id": device, "ip": ip,
                  "event_data": {"login_type": "login", "channel": "app"}}
        elif roll < 0.45:
            et = "order_create"
            oid, amt = bind_order(user_id, f"O-N{100000 + i}",
                                  round(random.uniform(50, 1500), 2))
            kw = {"device_id": device, "ip": ip, "receive_id": addr, "phone": phone,
                  "source_id": oid, "amount": amt}
        elif roll < 0.65:
            et = "order_pay"
            oid, amt = bind_order(user_id, f"O-N{100000 + i}",
                                  round(random.uniform(50, 1500), 2))
            kw = {"device_id": device, "ip": ip, "source_id": oid, "amount": amt}
        elif roll < 0.82:
            et = "coupon_receive"
            kw = {"device_id": device, "ip": ip, "phone": phone,
                  "source_id": f"CPN-N{200000 + i}",
                  "event_data": {"coupon_value": random.choice([10, 20, 50, 100])}}
        elif roll < 0.95:
            et = "after_sale_apply"
            pid, amt = bind_postsale(user_id, f"PS-N{300000 + i}",
                                     round(random.uniform(50, 1500), 2))
            kw = {"device_id": device, "ip": ip, "receive_id": addr, "phone": phone,
                  "source_id": pid, "amount": amt,
                  "event_data": {"refund_reason": random.choice(["size", "quality", "other"])}}
        else:
            et = "logistics_complaint"
            # 不再手写 `complaint` 文案：快照默认值会给出**成对**的
            # complaint + complaint_type（手写文案会让两者对不上，见 COMPLAINT_PAIRS）
            kw = {"device_id": device, "ip": ip,
                  "source_id": bind_complaint(user_id, f"LC-N{400000 + i}")}

        # 随机时刻：2 分钟前 ~ 89 天前，且**按真实作息加权**（见 `_daily_minute_ago`）。
        # ⚠ 返回的是「分钟数」不是「天数」—— 写 `2 * _MIN` 会变成「2 天前」，
        #   结果整个数据集的最新事件停在两天前，大盘「今日评估量」永远是 0。
        events.append(_base(seq, et, user_id, _daily_minute_ago(), **kw))
    return events


# ================================================================ 羊毛党 188（10 次作案）
def gen_coupon_fraud() -> list[dict]:
    """同设备多账号 + 高频领券：命中 R-008（短时高频领券，中/45）→ Mark 档。

    ⚠ **以「一次作案」为循环单位**，不是「以事件为循环单位」（v3.1 修正）。
      v3.0 的写法是 `for i in range(120)`、设备用 `devices[i % 3]` 轮转 ——
      在「全部塞在 1 小时里」时三个设备的领券量会累加到同一个 1h 窗口，所以能命中；
      一旦把时间分散成多次作案，每个窗口内只剩 5 条 → 规则失效。
      正确形态：**一次作案 = 一个设备 + 一小组账号 + 短时间内密集领券**。

    ⚠ **作案次数与单次规模**（v3.1 二次调参）：
      一开始是「8 次 × 33 条」，结果作案日太稀疏 ——
      90 天里只有 8 天有羊毛党活动，近 7 天窗口经常一次都没有或有两次，
      折线看起来就是「平的 → 突然翘起来」。
      改成「10 次 × 15 条，其中 2 次是 34 条」，作案日翻倍、规模减半，
      7 天窗口内稳定出现 2~3 次作案，曲线变成自然的波动。

      那 2 次大作案是**为 R-027「事件频次异常」保留的**：
      该规则要求 `user_event_cnt_1h >= 30`（单账号 1 小时内 30 个事件），
      这是规则条件本身决定的，没法靠分散规避 —— 真实羊毛党也确实是脚本刷，
      单账号一小时内领 30 次券完全符合画像。

    构造（小作案 15 条 / 大作案 34 条）：
      · 目标账号领券 12 或 30 次，另 3 个账号各 1 次
        → `device_coupon_cnt_1h` = 15 或 34，都 > 10 → R-008 命中
      · 同设备共 4 个账号 → `device_account_cnt_24h = 4 ≤ 5`，**不**触发 R-005
        （R-005 是极高 + 一票否决，会把结果冲到 Reject，破坏「Mark 档」的设计意图）
    """
    events: list[dict] = []
    users = [f"U-{1300 + i}" for i in range(1, 11)]
    devices = [f"DEV-FARM{i}" for i in range(1, 4)]
    ips = [f"172.16.88.{30 + i}" for i in range(3)]

    raids = _raid_starts(10, dur_min=40)
    # 第 2、7 次作案采用「脚本刷券」形态（覆盖 R-027）
    script_raids = {2, 7}
    base = 0
    for r, raid in enumerate(raids):
        device, ip = devices[r % len(devices)], ips[r % len(ips)]
        group = [users[(r * 3 + k) % len(users)] for k in range(4)]
        main, others = group[0], group[1:]
        n_main = 30 if r in script_raids else 12

        for j in range(n_main):
            events.append(_base(
                f"E-B{base + j:04d}", "coupon_receive", main, _in_raid(raid, 40),
                device_id=device, ip=ip, phone=f"13800009{100 + users.index(main)}",
                source_id=f"CPN-B{base + j:04d}",
                event_data={"coupon_value": random.choice([100, 200, 300, 500])},
            ))
        for j, uid in enumerate(others):
            events.append(_base(
                f"E-B{base + n_main + j:04d}", "coupon_receive", uid, _in_raid(raid, 40),
                device_id=device, ip=ip, phone=f"13800009{100 + users.index(uid)}",
                source_id=f"CPN-B{base + n_main + j:04d}",
                event_data={"coupon_value": random.choice([100, 200, 300, 500])},
            ))
        base += n_main + len(others)
    return events


# ================================================================ 恶意退款 80（8 次作案）
def gen_refund_fraud() -> list[dict]:
    """地址聚集团伙：9 账号共享收货地址 + 同手机号高退款率。

    → 命中 R-006 地址团伙退款（70，不可挑战）→ 中风险档直接 Review
    → 部分样本叠加 R-030（极高）触发一票否决 → Reject

    同 `gen_coupon_fraud`：**一次作案 = 9 个账号在同一地址/手机号上集中下单与索赔**，
    持续 12 小时（R-006 依赖 `addr_account_cnt_7d` 与 `phone_refund_rate_24h`，
    两者都在 24h/7d 窗口内，所以作案窗口取 12 小时即可）。
    """
    events: list[dict] = []
    users = [f"U-{1400 + i}" for i in range(1, 10)]
    phone = "13900008003"

    # 6 次作案 × 9 用户 × 2 事件 = 108 条。
    # 每次作案：先 8 单下单打底（`phone_refund_rate_24h` 的分母），再 10 单售后（分子）。
    # 9 个账号全部出现在同一地址 → `addr_account_cnt_7d = 9 > 3` → R-006（70，不可挑战）→ Review；
    # 手机号退款率被顶到 1.0 → R-030（90，极高）→ 一票否决 → 部分落到 Reject。
    raids = _raid_starts(6, dur_min=12 * 60)
    for r, raid in enumerate(raids):
        base = r * 18
        # ⚠ 下单/售后的**比例要在批次间变化**，否则 R-030 会把 R-006 盖住（v3.1）：
        #   `case_category` 取**最高分规则**的分类。若每次作案的
        #   `phone_refund_rate_24h` 都被顶到 ≥0.9，R-030（售后滥用/90）就总是压过
        #   R-006（地址风险/70），于是「地址风险」这个分类在案件里彻底消失 ——
        #   实测确实如此（地址风险案件 40 → 0），而它本来是这个团伙的主罪名。
        #   让偶数批次退款率低一些（12 单下单 / 6 单售后 = 0.5 < 0.9），
        #   R-030 不命中、R-006 成为最高分 → 案件归类到「地址风险」。
        # 三种「作案口味」，让不同的规则各自有机会成为**最高分**规则：
        #   r%3==0 → 10 单下单 / 8 单售后 = 退款率 0.8
        #            · > 0.7 → R-006 命中（地址风险/70）
        #            · < 0.9 → R-030 **不**命中（售后滥用/90）
        #            → 案件归类到「地址风险」✓
        #   r%3==1 → 8 / 10 = 1.0（封顶）→ R-006 与 R-030 同时命中，R-030 最高分
        #            → 归到「售后滥用」，且一票否决 → Reject
        #   r%3==2 → 12 / 6 = 0.5 → 两条**都**不命中，只留中低分规则
        #            → 落在 Mark 档，不进案件（模拟「看着可疑但证据不足」）
        _FLAVOUR_ORDERS = {0: 10, 1: 8, 2: 12}
        n_orders = _FLAVOUR_ORDERS[r % 3]
        for j in range(18):
            uid = users[(j // 2 + r) % len(users)]
            dev = f"DEV-R{users.index(uid):02d}"
            ip = f"10.30.11.{20 + (r * 3 + j) % 30}"
            if j < n_orders:
                oid, amt = bind_order(uid, f"O-R{base + j:06d}",
                                      round(random.uniform(2000, 8000), 2))
                events.append(_base(
                    f"E-R{base + j:04d}", "order_create", uid, _in_raid(raid, 12 * 60),
                    device_id=dev, ip=ip, receive_id=REFUND_RING_ADDRESS, phone=phone,
                    source_id=oid, amount=amt))
            else:
                pid, amt = bind_postsale(uid, f"PS-R{base + j:05d}",
                                         round(random.uniform(2000, 8000), 2))
                events.append(_base(
                    f"E-R{base + j:04d}", "after_sale_apply", uid, _in_raid(raid, 12 * 60),
                    device_id=dev, ip=ip, receive_id=REFUND_RING_ADDRESS, phone=phone,
                    source_id=pid, amount=amt,
                    event_data={"refund_reason": "not_received"}))
    return events


# ================================================================ 黄牛囤货 60（10 次作案）
def gen_scalping() -> list[dict]:
    """同设备高频大额下单 → 命中 R-005(90, 极高) + R-007(75, 不可挑战) → Reject。

    一次作案 = 同一设备上 6 个账号在 50 分钟内各抢 1 单（目标单使总数到 7）：
      · `device_account_cnt_24h = 7 > 5` → R-005（极高 → 一票否决）
      · `device_order_cnt_1h = 7 > 5`   → R-007（不可挑战）
      · 两条都是不可挑战 → 中风险档直接 Review；极高规则一票否决 → 最终 Reject
    """
    events: list[dict] = []
    users = [f"U-{1600 + i}" for i in range(1, 8)]
    device = "DEV-SCALP1"

    # ⚠ 规模为什么是「12 次作案 × 7 用户各下 2 单」（v3.1 调参）：
    #   流式风控里，一次作案的**前几条**事件是「看不见集群」的 ——
    #   第 1 条下单时 `device_account_cnt_24h` 还只是 1，只有等 6 个账号都出过手，
    #   后续事件才会看到 > 5。所以一次作案 6 条只能抓到末尾 1~2 条，
    #   10 次作案一共才 20 条 Reject，整体拦截率掉到 2.5%（低于 PRD 的 8%~15%）。
    #   让每个账号在窗口内下 2 单，把「看得见集群」的事件段拉长到 8/14，
    #   拦截量才回到合理区间。
    # 18 次作案 × 10 条（每次 5 个账号各下 2 单）：
    #   总规模与「13 次 × 14 条」接近（拦截量不变），但**作案日更多、单次更小**，
    #   时间轴因此更均匀 —— 与羊毛党同样的调参思路，见 gen_coupon_fraud 的说明。
    #   ⚠ 每轮必须有 **≥6 个不同账号**（v3.1 踩过一次）：
    #     R-005「同设备多账号」的门槛是 `device_account_cnt_24h > 5`。
    #     我先按「每轮 5 个账号各下 2 单」调参，结果 `device_account_cnt_24h = 5`，
    #     差一点点没越过门槛 → R-005（极高）不命中 → 一票否决不触发 →
    #     整体拦截率从 10% 掉到 7.4%。**门槛是严格大于，不是大于等于。**
    #   每轮 6 个账号各下 2 单 = 12 条：
    #     · `device_account_cnt_24h = 6 > 5` → R-005（极高，一票否决）✓
    #     · `device_order_cnt_1h` = 12 > 5 → R-007（高/75，不可挑战）✓
    #     → 高分档 + 一票否决 → Reject
    raids = _raid_starts(18, dur_min=50)
    for r, raid in enumerate(raids):
        base = r * 12
        for j in range(12):
            uid = users[(j // 2 + r) % len(users)]
            oid, amt = bind_order(uid, f"O-S{base + j:06d}",
                                  round(random.uniform(4000, 9000), 2))
            events.append(_base(
                f"E-S{base + j:04d}", "order_create", uid, _in_raid(raid, 50),
                device_id=device, ip=f"10.44.20.{11 + (r * 2 + j) % 20}",
                receive_id=f"RCV-HZ-{(j % 4) + 1:02d}",
                phone=f"13700007{100 + users.index(uid)}",
                source_id=oid, amount=amt))
    return events


# ================================================================ 物流投诉异常 40
def gen_complaint() -> list[dict]:
    """同用户高频投诉 → 命中 R-022 物流投诉频繁（45，可挑战）→ Mark。

    ⚠ **为什么 5 个账号都出镜**（v3.1 二次加固）：
      业务库里投诉本来就集中在 5 个账号（`COMPLAINER_USERS`，每个约 16 条投诉记录），
      但事件数据集只用 `U-1500` 一个 —— 于是「物流风险」这一类风险画像
      在事件侧只有一个样本，且其余 4 个账号的投诉记录在事件侧毫无体现。
      改为两个账号组轮流出场，样本更厚，也更接近「少数几个惯投诉用户」的真实分布。

    ⚠ **必须保住的阈值**（否则 R-031 会重新变成零命中，就是 §8.7.1 那类
      「数据覆盖」陷阱 —— 规则没错，是样本没了）：
        · R-031 要求 `user_event_cnt_1h >= 8`（1h 内该用户的**全部**事件数）。
          所以每轮必须有一个**主账号**在 30 分钟窗口内密集投诉 ≥8 次。
          当初一旦把 20 条摊平成「5 个账号各 4 条」，主账号就只有 4 条 → 规则失效。
          这里保持主账号每轮 9 条（留 1 条余量，门槛是 `>= 8`）。
        · R-022 走 `logistics_complaints_record` 的**业务计数**（每账号约 16 条），
          与事件条数无关，5 个账号本来就都命中。
    """
    events: list[dict] = []
    # 2 次投诉爆发，每次 30 分钟内主账号 9 条 + 另 4 个账号各 3 条 = 21 条
    raids = _raid_starts(2, dur_min=30)
    for r, raid in enumerate(raids):
        main = COMPLAINER_USERS[r % len(COMPLAINER_USERS)]
        others = [u for u in COMPLAINER_USERS if u != main]
        seq = 0
        for _ in range(9):
            events.append(_base(
                f"E-C{r}{seq:03d}", "logistics_complaint", main, _in_raid(raid, 30),
                device_id=f"DEV-CMP{COMPLAINER_USERS.index(main):02d}",
                ip=f"10.55.10.{10 + COMPLAINER_USERS.index(main)}",
                source_id=bind_complaint(main, f"LC-C{r}{seq:03d}"),
            ))
            seq += 1
        for uid in others:
            for _ in range(3):
                events.append(_base(
                    f"E-C{r}{seq:03d}", "logistics_complaint", uid, _in_raid(raid, 30),
                    device_id=f"DEV-CMP{COMPLAINER_USERS.index(uid):02d}",
                    ip=f"10.55.10.{10 + COMPLAINER_USERS.index(uid)}",
                    source_id=bind_complaint(uid, f"LC-C{r}{seq:03d}"),
                ))
                seq += 1
    return events


# ================================================================ 边界样本 120（6 批 × 20 样本）
def gen_boundary() -> tuple[list[dict], list[dict]]:
    """构造分值落在阈值边界（60~79）的样本，用于验证 Challenge / Review 分界。

    **构造约束（必须同时满足，否则会窜到 Reject 档）**：
      · 同批 20 个账号共用一个 IP → `ip_account_cnt_24h = 20 > 8` → R-009（+40）
      · 同 IP 1h 内总下单 ≥ 10 → `ip_order_cnt_1h` 命中 R-010（+60）
      · 每个账号**独占一个设备**且 1h 内下单 ≤ 5 → 不触发 R-007（设备高频下单，75 分不可挑战）
      · 每账号关联账号数 = 1 → 不触发 R-005（同设备多账号，90 分极高，会一票否决）

    结果：`max(60, 40) + 3×(2−1) = 63` → 中风险档；两条规则均「可挑战」 → **Challenge**

    ⚠ 时间安排（v3.1）：约束里的「共用 IP」「24h 内 20 个账号」是**同日聚集**的语义，
      不能像别的场景那样简单地把每个账号摊到不同天 —— 那样 `ip_account_cnt_24h` 会掉到 1，
      R-009 不命中，分数就不是 63 而是废的。
      所以改成 **6 批**，每批 20 个样本、在**同一天内**完成（各批日期互不相同）：
      既保住构造语义，又让时间轴上均匀分布（原来的做法是 120 条全挤在最后 1 小时）。
    """
    context: list[dict] = []
    targets: list[dict] = []
    shared_ip = "172.16.99.10"
    # 铺垫单数：**1 单就够**。
    #   ⚠ v3.0 用 4 单，注释写「让 device_order_cnt_1h = 5，不 > 5（避开 R-007）」——
    #     但「不 > 5」的条件在 1 单时同样成立（1<5），4 单纯属多余，
    #     却让边界样本的**上下文事件**数量翻了两番：这些事件本身也会被评估，
    #     而它们大多落在 Challenge 档，结果 6 批下来 Challenge 占了全部决策的 30%，
    #     把五档分布彻底带偏。铺垫只承担「把 1h/24h 窗口填满」这一个职责。
    SEED_ORDERS = 1
    # 批次与每批人数（v3.1 调参）：
    #   `ip_account_cnt_24h > 8` 只需同一批 >8 个账号，12 个足够；
    #   `ip_order_cnt_1h >= 10` 只需 1h 内 ≥10 单，12 人下单 24 单也够。
    #   把「6 批 × 20 人」改成「10 批 × 12 人」：总量相近，
    #   但每批只给单日增加 36 条事件（原来 60 条），单日峰值从 ~100 降到 ~70，
    #   同时把边界样本摊到 10 个不同日期，时间轴更均匀。
    BATCHES = 10
    BATCH_USERS = 12

    # 各批次占一天，日期互不相邻（相隔 8 天，保证 24h 窗口不重叠）
    batch_days = [6 + i * 8 for i in range(BATCHES)]

    for b, day in enumerate(batch_days):
        raid = day * _MIN + random.uniform(0, _MIN - 60)
        for i in range(BATCH_USERS):
            user_id = f"U-BD{i:02d}"
            seq = f"{b}{i:02d}"
            context.append(_base(
                f"E-BDL{b}{i:03d}", "login", user_id, raid + random.uniform(23, 50),
                device_id=f"DEV-BD{i:02d}", ip=shared_ip,
                event_data={"login_type": "register", "channel": "h5"},
            ))
            for j in range(SEED_ORDERS):
                oid, amt = bind_order(user_id, f"O-BD{b}{i:02d}{j:02d}",
                                      round(random.uniform(100, 800), 2))
                context.append(_base(
                    f"E-BD{b}{i:02d}{j:02d}", "order_create", user_id,
                    raid + random.uniform(7, 45),
                    device_id=f"DEV-BD{i:02d}", ip=shared_ip,
                    receive_id=f"RCV-BD{i:02d}",
                    phone=f"13500009{100 + i}", source_id=oid, amount=amt,
                ))
            oid, amt = bind_order(user_id, f"O-BD-T{b}{i:02d}",
                                  round(random.uniform(100, 800), 2))
            # 目标事件是窗口内**最近**的一条（分钟数最小）
            targets.append(_base(
                f"E-BD-T{b}{i:02d}", "order_create", user_id, raid + random.uniform(0, 4),
                device_id=f"DEV-BD{i:02d}", ip=shared_ip,
                receive_id=f"RCV-BD{i:02d}",
                phone=f"13500009{100 + i}", source_id=oid, amount=amt,
            ))
    return context, targets


# ================================================================ 风险画像用户事件
def gen_persona_events() -> list[dict]:
    """为 `gen_business_data.py` 造出的 **10 类风险画像用户**生成对应事件。

    ⚠ 为什么必须补这一块：
      规则集里的 30 条规则各自刻画一种风险画像，但 v3.0 的事件数据集只覆盖
      「正常流量 + 羊毛党/恶意退款/黄牛」四类，于是另外 15 条规则的条件
      在数据上永远不成立，规则效果表上一半显示「零命中」。
      业务库补了画像用户之后（见 gen_business_data.py 的 PERSONA_GROUPS），
      还需要**事件**去触发它们 —— 业务数据只决定特征取值，事件才驱动评估。

    画像 → 事件：
      高频下单 P-HF → order_create（近 7 天已有 22 单 → R-019）
      高频售后 P-AS → after_sale_apply（9 单售后 → R-020 / R-024）
      频繁取消 P-CX → order_create（10 单取消 → R-021）
      高额退款 P-RF → after_sale_apply（退款额 ~2.6 万 → R-023）
      多地址跨省 P-AD → order_create（12 地址 / 7 省 → R-012 / R-013）
      批发扫货 P-WS → order_create（24 行 / 9 类 / 60 件 → R-016/17/18）
      大额折扣 P-DC → order_create（折扣率 >0.85 → R-015）
      秒付订单 P-FP → order_pay（支付间隔 3~8 秒 → R-014）
      夜间大额 P-NG → order_pay（凌晨 + 金额 ≥3000 → R-003 / R-028）
      零订单新号 P-NU → order_create（引用**他人**大额订单 → R-025）
    """
    events: list[dict] = []
    # 画像事件分散在 90 天里，避免又堆到同一天
    starts = _raid_starts(10, span_days=(5, DAYS_SPAN - 5), dur_min=60)

    def _persona(gi: int, k: int) -> str:
        return f"U-{2000 + gi * 10 + k}"

    def _persona_phone(gi: int, k: int) -> str:
        """画像用户手机号：**必须恰好 11 位数字**。

        ⚠ 踩过的坑：一开始写成 `f"133000{gi:02d}{k:02d}"` —— 拼出来只有 10 位，
          被 `RiskCheckRequest._check_phone`（要求恰好 11 位）判为非法，
          于是 10 类画像里有 9 类的**全部 45 条事件被静默拒收**，
          画像规则一条都没命中。用 8 位定长数字补足到 11 位，杜绝这类拼接错误。
        """
        return f"133{2000 + gi * 10 + k:08d}"

    # 每类画像给 5 个账号各一条事件；时间分散到不同天
    for gi in range(len(PERSONA_GROUPS)):
        group = PERSONA_GROUPS[gi]
        for k in range(1, 6):
            uid = _persona(gi, k)
            # 每个账号挑一个自己的日期（用 gi/k 派生，保证分散且可复现）
            day = 6 + ((gi * 5 + k) * 8) % (DAYS_SPAN - 12)
            minute = day * _MIN + random.uniform(0, _MIN - 5)
            code = group["code"]

            if code == "NU":
                # 零订单新号：引用一个**不属于自己**的**大额**订单（盗号 / 撞库下单）。
                #   ⚠ `other` 必须挑「订单金额够大」的账号（v3.1 踩过）：
                #     R-025 = `user_total_orders == 0 AND order_total_amount >= 1000`，
                #     第二项取的是**被引用订单**的金额。原先随手从普通用户里挑，
                #     而普通用户的订单平均只有几百元（大众 SKU ¥39~900），
                #     金额凑不到 1000 → 这条规则命中数恒为 0。
                #     黄牛账号（U-16xx）的订单是高价值 SKU（¥3000~9000），必过门槛。
                other = f"U-{1601 + (gi + k) % 7}"
                oid, amt = bind_order(other, f"O-PNU{k:05d}",
                                      round(random.uniform(3200, 8600), 2))
                events.append(_base(
                    f"E-P{gi:02d}{k:02d}", "order_create", uid, minute,
                    device_id=f"DEV-PNU{k}", ip=f"10.77.9.{k}",
                    receive_id=f"RCV-P-{uid[2:]}-1", phone=_persona_phone(gi, k),
                    source_id=oid, amount=amt))
                continue

            if code in ("AS", "RF"):
                pid, amt = bind_postsale(uid, f"PS-P{gi:02d}{k:02d}",
                                         round(random.uniform(2000, 6000), 2))
                events.append(_base(
                    f"E-P{gi:02d}{k:02d}", "after_sale_apply", uid, minute,
                    device_id=f"DEV-P{group['code']}{k}", ip=f"10.77.1{gi}.{k}",
                    receive_id=f"RCV-P-{uid[2:]}-1", phone=_persona_phone(gi, k),
                    source_id=pid, amount=amt,
                    event_data={"refund_reason": "not_received"}))
                continue

            if code == "FP":
                oid, amt = bind_order(uid, f"O-P{gi:02d}{k:02d}",
                                      round(random.uniform(80, 900), 2))
                events.append(_base(
                    f"E-P{gi:02d}{k:02d}", "order_pay", uid, minute,
                    device_id=f"DEV-PFP{k}", ip=f"10.77.2.{k}",
                    receive_id=f"RCV-P-{uid[2:]}-1", phone=_persona_phone(gi, k),
                    source_id=oid, amount=amt))
                continue

            # 其余画像统一走 order_create / order_pay
            oid, amt = bind_order(uid, f"O-P{gi:02d}{k:02d}",
                                  round(random.uniform(300, 8600), 2))
            et = "order_pay" if code == "NG" else "order_create"
            events.append(_base(
                f"E-P{gi:02d}{k:02d}", et, uid, minute,
                device_id=f"DEV-P{code}{k}", ip=f"10.77.{30 + gi}.{k}",
                receive_id=f"RCV-P-{uid[2:]}-1",
                phone=_persona_phone(gi, k),
                source_id=oid, amount=amt))

    return events


# ================================================================ 主入口
def main() -> None:
    # ⚠ 绑定业务库真实单据：没有这一步，订单维度特征与事件金额全部为 0
    #   （见本模块 `_Binder` 的说明）。绑定必须发生在生成**之前**。
    global _BIND
    try:
        _BIND = _Binder(load_business_index())
    except Exception as exc:  # noqa: BLE001  业务库未就绪时降级为合成单据
        print(f"  ⚠ 业务库不可用（{exc}），事件将使用合成单据 ——")
        print("     订单维度特征与金额会恒为 0，请先执行 gen_business_data.py")
        _BIND = None

    normal = gen_normal()
    coupon = gen_coupon_fraud()
    refund = gen_refund_fraud()
    scalping = gen_scalping()
    complaint = gen_complaint()
    boundary_ctx, boundary_targets = gen_boundary()
    persona = gen_persona_events()

    main_samples = (normal + coupon + refund + scalping + complaint
                   + persona + boundary_targets)
    events = main_samples + boundary_ctx
    events.sort(key=lambda e: e["event_time"])

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(events, fh, ensure_ascii=False, indent=1)

    print("\n" + "=" * 62)
    print("  模拟事件数据集生成完成")
    print("=" * 62)
    print(f"  {'类别':<22}{'条数':>8}")
    print("-" * 62)
    print(f"  {'正常用户事件':<22}{len(normal):>8}")
    print(f"  {'羊毛党（领券+下单）':<20}{len(coupon):>8}")
    print(f"  {'恶意退款（地址团伙）':<20}{len(refund):>8}")
    print(f"  {'黄牛囤货':<22}{len(scalping):>8}")
    print(f"  {'物流投诉异常':<22}{len(complaint):>8}")
    print(f"  {'边界样本':<22}{len(boundary_targets):>8}")
    print(f"  {'风险画像样本（10 类）':<19}{len(persona):>8}")
    print("-" * 62)
    print(f"  {'主样本小计（SPEC §10.3）':<19}{len(main_samples):>8}")
    print(f"  {'边界上下文事件':<21}{len(boundary_ctx):>8}")
    print(f"  {'文件总条数':<22}{len(events):>8}")
    print("=" * 62)
    print(f"  输出文件：{OUT_FILE}")
    print(f"  时间范围：{events[0]['event_time']}  →  {events[-1]['event_time']}")

    # 单据绑定情况：未绑定的比例直接决定「订单维度是否真的在工作」，
    # 必须显式上报 —— 否则订单特征恒为 0 这件事在数据文件里完全看不出来。
    if _BIND is not None:
        unbound = _BIND.unbound
        total = sum(unbound.values())
        print(f"  单据绑定：订单未绑定 {unbound['order']} 条 · "
              f"投诉未绑定 {unbound['complaint']} 条")
        if total:
            print("    ⚠ 上述事件指向合成单据，其订单维度特征与金额将为 0 ——")
            print("      通常是这些账号在业务库里没有订单 / 投诉记录（参见 gen_business_data.py）")
        via = _BIND.via_order["postsale"]
        if via:
            print(f"  售后事件中，有 {via} 条因该用户没有售后单而绑定到**订单号** ——")
            print("      source_id 装的是订单号而非售后单号，下游由 enrich_request()")
            print("      按订单号容错补全（不再报「售后单未关联到订单」）。")

    # 事件快照覆盖率 —— 与上面的单据绑定同理，属「看起来有、其实是空」的高危项。
    # 历史上 order_create / order_pay 的快照**整类为 null**（占数据集 54%），
    # 而这一点在 json 文件里、在任何页面和接口上都看不出来，只能靠显式上报兜住。
    cover: dict[str, list[int]] = {}
    for e in events:
        slot = cover.setdefault(e["event_type"], [0, 0])
        slot[0] += 1
        if e.get("event_data"):
            slot[1] += 1
    print("  事件快照（event_data）覆盖：")
    for et in sorted(cover):
        total_n, with_snap = cover[et]
        flag = "✓" if with_snap == total_n else "✗"
        print(f"    {flag} {et:<22}{with_snap:>5}/{total_n:<5}")
    naked = [e["event_id"] for e in events if not e.get("event_data")]
    if naked:
        print(f"    ⚠ {len(naked)} 条事件缺快照（例：{naked[:3]}）——"
              " 下游页面与接口都看不出这件事，请检查 snapshot_for()")
    print()


if __name__ == "__main__":
    main()
