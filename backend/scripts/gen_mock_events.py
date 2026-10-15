"""gen_mock_events.py —— 生成模拟电商风险事件流数据集（SPEC §10.3）。

产出 `data/mock_events.json`，**≥ 1200 条**：

| 类别 | 数量 | 特征 | 预期结果 |
|------|-----:|------|---------|
| 正常用户事件 | 900 | 各特征正常 | Pass / Mark |
| 羊毛党领券+下单 | 120 | 同设备多账号、高频领券 | Mark / Challenge / Reject |
| 恶意退款 | 80 | 高退款率 + 同地址聚集 | Review / Reject |
| 黄牛囤货 | 60 | 高频大额下单 | Reject |
| 物流投诉异常 | 20 | 高频投诉 | Mark |
| 边界样本 | 20 | 分值接近阈值 | 验证阈值 |

每条事件带 `event_time`，按时间序灌入保证特征计算正确。
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


def ts(minutes_ago: float) -> str:
    return to_iso(NOW - timedelta(minutes=minutes_ago))


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


def _base(event_id: str, event_type: str, user_id: str, minute: float, **kw) -> dict:
    event = {"event_id": event_id, "event_type": event_type,
             "user_id": user_id, "event_time": ts(minute)}
    event.update({k: v for k, v in kw.items() if v is not None})
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
            kw = {"device_id": device, "ip": ip,
                  "source_id": bind_complaint(user_id, f"LC-N{400000 + i}"),
                  "event_data": {"complaint": "配送延迟"}}

        events.append(_base(seq, et, user_id, random.uniform(1, 30 * 24 * 60), **kw))
    return events


# ================================================================ 羊毛党 120
def gen_coupon_fraud() -> list[dict]:
    """同设备多账号 + 高频领券：命中 R-005（同设备多账号，极高）与 R-008（短时高频领券）。"""
    events = []
    users = [f"U-{1300 + i}" for i in range(1, 11)]
    devices = [f"DEV-FARM{i}" for i in range(1, 4)]
    ips = [f"172.16.88.{30 + i}" for i in range(3)]

    for i in range(120):
        user_id = users[i % len(users)]
        device = devices[i % len(devices)]
        ip = ips[i % len(ips)]
        minute = random.uniform(1, 55)
        if i % 2 == 0:
            events.append(_base(
                f"E-B{i:04d}", "coupon_receive", user_id, minute,
                device_id=device, ip=ip,
                phone=f"13800009{100 + users.index(user_id)}",
                source_id=f"CPN-B{i:04d}",
                event_data={"coupon_value": random.choice([100, 200, 300, 500])},
            ))
        else:
            oid, amt = bind_order(user_id, f"O-B{i:06d}",
                                  round(random.uniform(300, 1500), 2))
            events.append(_base(
                f"E-B{i:04d}", "order_create", user_id, minute,
                device_id=device, ip=ip, receive_id=f"RCV-BULK-{(i % 5) + 1:02d}",
                phone=f"13800009{100 + users.index(user_id)}",
                source_id=oid, amount=amt,
            ))
    return events


# ================================================================ 恶意退款 80
def gen_refund_fraud() -> list[dict]:
    """地址聚集团伙：9 账号共享收货地址 + 同手机号高退款率。

    → 命中 R-006 地址团伙退款（70，不可挑战）→ 中风险档直接 Review
    → 部分样本叠加 R-004/R-030（极高）触发一票否决 → Reject
    """
    events = []
    users = [f"U-{1400 + i}" for i in range(1, 10)]
    phone = "13900008003"

    for i in range(80):
        user_id = users[i % len(users)]
        minute = random.uniform(5, 23.5 * 60)
        if i % 3 == 0:
            oid, amt = bind_order(user_id, f"O-R{i:06d}",
                                  round(random.uniform(2000, 8000), 2))
            events.append(_base(
                f"E-R{i:04d}", "order_create", user_id, minute,
                device_id=f"DEV-R{users.index(user_id):02d}", ip=f"10.30.11.{20 + (i % 30)}",
                receive_id=REFUND_RING_ADDRESS, phone=phone,
                source_id=oid, amount=amt,
            ))
        else:
            pid, amt = bind_postsale(user_id, f"PS-R{i:05d}",
                                     round(random.uniform(2000, 8000), 2))
            events.append(_base(
                f"E-R{i:04d}", "after_sale_apply", user_id, minute,
                device_id=f"DEV-R{users.index(user_id):02d}", ip=f"10.30.11.{20 + (i % 30)}",
                receive_id=REFUND_RING_ADDRESS, phone=phone,
                source_id=pid, amount=amt,
                event_data={"refund_reason": "not_received"},
            ))
    return events


# ================================================================ 黄牛囤货 60
def gen_scalping() -> list[dict]:
    """同设备高频大额下单 → 命中 R-005(90, 极高) + R-007(75, 不可挑战) → Reject。"""
    events = []
    users = [f"U-{1600 + i}" for i in range(1, 8)]
    device = "DEV-SCALP1"

    for i in range(60):
        user_id = users[i % len(users)]
        oid, amt = bind_order(user_id, f"O-S{i:06d}",
                              round(random.uniform(4000, 9000), 2))
        events.append(_base(
            f"E-S{i:04d}", "order_create", user_id, random.uniform(1, 55),
            device_id=device, ip=f"10.44.20.{11 + (i % 20)}",
            receive_id=f"RCV-HZ-{(i % 4) + 1:02d}",
            phone=f"13700007{100 + users.index(user_id)}",
            source_id=oid, amount=amt,
        ))
    return events


# ================================================================ 物流投诉异常 20
def gen_complaint() -> list[dict]:
    """同用户高频投诉 → 命中 R-022 物流投诉频繁（45，可挑战）→ Mark。"""
    events = []
    for i in range(20):
        events.append(_base(
            f"E-C{i:04d}", "logistics_complaint", "U-1500", random.uniform(30, 2000),
            device_id="DEV-CMP01", ip="10.55.10.10",
            source_id=bind_complaint("U-1500", f"LC-C{i:05d}"),
            event_data={"complaint": random.choice(["配送延迟", "包裹丢失", "虚假签收"])},
        ))
    return events


# ================================================================ 边界样本 20
def gen_boundary() -> tuple[list[dict], list[dict]]:
    """构造分值落在阈值边界（60 分）的样本，用于验证 Challenge / Review 分界。

    **构造约束（必须同时满足，否则会窜到 Reject 档）**：
      · 20 个账号共用一个 IP → `ip_account_cnt_24h = 20 > 8` → R-009（+40）
      · 同 IP 1h 内总下单 ≥ 10 → `ip_order_cnt_1h` 命中 R-010（+60）
      · 每个账号**独占一个设备**且 1h 内下单 ≤ 5 → 不触发 R-007（设备高频下单，75 分不可挑战）
      · 每账号关联账号数 = 1 → 不触发 R-005（同设备多账号，90 分极高，会一票否决）

    结果：`max(60, 40) + 3×(2−1) = 63` → 中风险档；两条规则均「可挑战」 → **Challenge**
    """
    context: list[dict] = []
    targets: list[dict] = []
    shared_ip = "172.16.99.10"
    SEED_ORDERS = 4          # 目标事件是第 5 单，device_order_cnt_1h = 5（不 > 5）

    for i in range(20):
        user_id = f"U-BD{i:02d}"
        context.append(_base(
            f"E-BDL{i:03d}", "login", user_id, random.uniform(41, 50),
            device_id=f"DEV-BD{i:02d}", ip=shared_ip,
            event_data={"login_type": "register", "channel": "h5"},
        ))
        for j in range(SEED_ORDERS):
            oid, amt = bind_order(user_id, f"O-BD{i:02d}{j:02d}",
                                  round(random.uniform(100, 800), 2))
            context.append(_base(
                f"E-BD{i:02d}{j:02d}", "order_create", user_id, random.uniform(2, 39),
                device_id=f"DEV-BD{i:02d}", ip=shared_ip, receive_id=f"RCV-BD{i:02d}",
                phone=f"13500009{100 + i}", source_id=oid, amount=amt,
            ))
        oid, amt = bind_order(user_id, f"O-BD-T{i:02d}",
                              round(random.uniform(100, 800), 2))
        targets.append(_base(
            f"E-BD-T{i:02d}", "order_create", user_id, random.uniform(1, 2),
            device_id=f"DEV-BD{i:02d}", ip=shared_ip, receive_id=f"RCV-BD{i:02d}",
            phone=f"13500009{100 + i}", source_id=oid, amount=amt,
        ))
    return context, targets


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

    main_samples = normal + coupon + refund + scalping + complaint + boundary_targets
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
    print()


if __name__ == "__main__":
    main()
