"""gen_business_data.py —— 生成 17 张业务表的模拟数据（SPEC §10.2）。

数据规模（PRD Q11 已确认）：
| 表 | 目标行数 |
| user_info | 200 | region | 340 | receive_info | 350 |
| product_category | 12 | sku_info | 120 | order_status | 7 |
| order_info | 600 | order_detail | 1200 | logistics | 500 |
| order_logistics | 600 | logistics_company | 8 | logistics_complaint | 10 |
| logistics_complaints_record | 80 | postsale_status | 6 | postsale_reason | 12 |
| postsale | 150 | postsale_logistics | 120 |

**关键要求**：
- 数据**自洽**：order_detail.order_id 必须存在于 order_info；order_info.receive_id 必须属于该 user_id
- 时间跨度 90 天，保证 user_orders_30d / user_orders_7d 有区分度
- **固定随机种子**保证可复现
- 内置作弊团伙（地址聚集 / 高退款率）让风控特征有信号

用法：
    python scripts/gen_business_data.py
    python scripts/gen_business_data.py --clean   # 先清空再生成
"""

import argparse
import os
import random
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete  # noqa: E402

from app.database import create_all, session_scope  # noqa: E402
from app.models import (Logistics, LogisticsCompany, LogisticsComplaint,  # noqa: E402
                        LogisticsComplaintsRecord, OrderDetail, OrderInfo,
                        OrderLogistics, OrderStatus, Postsale, PostsaleLogistics,
                        PostsaleReason, PostsaleStatus, ProductCategory, ReceiveInfo,
                        Region, SkuInfo, UserInfo)
from app.utils import now_dt  # noqa: E402

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

# ---------------------------------------------------------------- 参考数据
CATEGORIES = ["数码配件", "手机通讯", "电脑办公", "家用电器", "服饰鞋包",
              "美妆个护", "食品生鲜", "母婴玩具", "运动户外", "图书文娱",
              "家居建材", "汽车用品"]

COMPANIES = ["顺丰速运", "中通快递", "圆通速递", "韵达快递",
             "京东物流", "邮政EMS", "申通快递", "极兔速递"]

ORDER_STATUSES = [("待付款", 10), ("已支付", 20), ("已发货", 30), ("已签收", 40),
                  ("已完成", 50), ("已取消", 60), ("退款中", 70)]

POSTSALE_STATUSES = [("待处理", 0, 0, 0), ("退款中", 1, 0, 0), ("已退款", 1, 0, 0),
                     ("退货中", 0, 1, 0), ("已换货", 0, 0, 1), ("已拒绝", 0, 0, 0)]

POSTSALE_REASONS = ["不想要了", "尺寸不符", "质量问题", "与描述不符", "未收到货",
                    "发错货", "少件漏发", "包装破损", "价格保护", "七天无理由",
                    "商品损坏", "其他"]

LOGISTICS_STATUSES = ["待揽收", "运输中", "派送中", "已签收", "异常"]
LOGISTICS_COMPLAINTS = ["配送延迟", "货物破损", "包裹丢失", "服务态度差",
                        "未经允许代签", "虚假签收", "运费争议", "配送错误",
                        "多次投递失败", "其他投诉"]

PROVINCES = [("浙江省", ["杭州市", "宁波市", "温州市"], ["西湖区", "余杭区", "鄞州区"]),
             ("江苏省", ["南京市", "苏州市"], ["鼓楼区", "姑苏区"]),
             ("上海市", ["上海市"], ["浦东新区", "徐汇区"]),
             ("广东省", ["广州市", "深圳市"], ["天河区", "南山区"]),
             ("北京市", ["北京市"], ["朝阳区", "海淀区"]),
             ("四川省", ["成都市"], ["武侯区", "锦江区"])]

SKU_NAMES = ["无线耳机", "机械键盘", "智能手表", "蓝牙音箱", "移动电源",
             "显示器", "扫地机器人", "游戏主机", "固态硬盘", "路由器",
             "摄像头", "鼠标", "数据线", "硬盘盒", "屏幕",
             "显卡", "CPU 散热器", "机箱", "电源", "内存条"]

# ---------------------------------------------------------------- 作弊团伙设定
REFUND_RING_SIZE = 9                    # 地址聚集团伙账号数
REFUND_RING_ADDRESS = "RCV-XS-01"       # 团伙共享地址（与仿真模板 TPL-02 呼应）

# ---------------------------------------------------------------- 事件数据集用到的账号
#
# ⚠ 为什么这些账号必须建在业务库里（v3.1 新增）：
#   `gen_mock_events.py` 里的事件带 `source_id`（订单号 / 售后单号 / 投诉记录号），
#   风控在 `enrich_request()` 里靠它反查 `order_info` → `order_detail`，
#   8 个**订单维度特征**与事件金额全部来自这次反查。
#   若事件指向的订单号在业务库里不存在，那 8 个特征恒为 0、金额恒为 0 ——
#   「挽回资损」（FR-46）永远是 ¥0，案件详情的「当前订单」永远是空，
#   「拦截订单」处置永远返回「订单不存在」。
#   所以事件里出现的每一类账号，都要在业务库里有真实的用户、地址与订单。
FARM_USERS = [f"U-{1300 + i}" for i in range(1, 11)]        # 羊毛党（10 个）
SCALPER_USERS = [f"U-{1600 + i}" for i in range(1, 8)]      # 黄牛囤货（7 个）
COMPLAINER_USERS = ["U-1500", "U-1501", "U-1502", "U-1503", "U-1504"]  # 投诉集中账号
HIGH_REFUND_USERS = 12                  # 高退款率用户数


def ts(days_ago: float) -> object:
    return NOW - timedelta(days=days_ago)


# ================================================================ 各表生成
def gen_reference(db) -> dict:
    """参考数据表（region / product_category / order_status / logistics_* 等）。"""
    for province, cities, districts in PROVINCES:
        for city in cities:
            for district in districts:
                if db.get(Region, (province, city, district)) is None:
                    db.add(Region(province=province, city=city, district=district))

    for c in CATEGORIES:
        if db.get(ProductCategory, c) is None:
            db.add(ProductCategory(product_category=c))

    for status, code in ORDER_STATUSES:
        if db.get(OrderStatus, status) is None:
            db.add(OrderStatus(order_status=status, status_code=code))

    for status, is_refund, is_return, is_exchange in POSTSALE_STATUSES:
        if db.get(PostsaleStatus, status) is None:
            db.add(PostsaleStatus(postsale_status=status, is_refund=is_refund,
                                  is_return=is_return, is_exchange=is_exchange))

    for reason in POSTSALE_REASONS:
        if db.get(PostsaleReason, reason) is None:
            db.add(PostsaleReason(postsale_reason=reason,
                                  product_category=random.choice(CATEGORIES)))

    for company in COMPANIES:
        if db.get(LogisticsCompany, company) is None:
            db.add(LogisticsCompany(company_name=company))

    for status in LOGISTICS_STATUSES:
        for complaint in LOGISTICS_COMPLAINTS:
            if db.get(LogisticsComplaint, (status, complaint)) is None:
                db.add(LogisticsComplaint(logistics_status=status,
                                          logistics_complaint=complaint))
    db.flush()
    return {"region": len(PROVINCES) * 8, "product_category": len(CATEGORIES),
            "order_status": len(ORDER_STATUSES), "postsale_status": len(POSTSALE_STATUSES),
            "postsale_reason": len(POSTSALE_REASONS), "logistics_company": len(COMPANIES),
            "logistics_complaint": len(LOGISTICS_STATUSES) * len(LOGISTICS_COMPLAINTS)}


def gen_users_and_addresses(db) -> tuple[list[str], dict[str, list[str]]]:
    """user_info + receive_info：200 用户，每号 1~3 个收货地址。"""
    user_ids = [f"U-{1000 + i}" for i in range(1, 201)]
    for idx, uid in enumerate(user_ids):
        if db.get(UserInfo, uid) is None:
            db.add(UserInfo(user_id=uid, register_time=ts(random.uniform(30, 800)),
                            user_level=random.choice(["普通", "银牌", "金牌"])))

    addr_of: dict[str, list[str]] = {}
    addr_seq = 0
    for idx, uid in enumerate(user_ids):
        count = 1 if idx < 40 else random.randint(1, 3)
        addrs: list[str] = []
        for _ in range(count):
            addr_seq += 1
            rid = f"RCV-{addr_seq:04d}"
            province, city, district = _random_region()
            addrs.append(rid)
            if db.get(ReceiveInfo, rid) is None:
                db.add(ReceiveInfo(
                    receive_id=rid, user_id=uid,
                    receiver_name=f"收货人{idx:03d}",
                    receiver_phone=f"13{random.randint(100000000, 999999999)}",
                    receive_province=province, receive_city=city,
                    receive_district=district,
                    receive_street_address=f"{city}{district}{random.randint(1, 200)}号",
                ))
        addr_of[uid] = addrs

    # 地址聚集团伙：9 个账号共享同一收货地址（制造 addr_account_cnt_7d 信号）
    ring_users = [f"U-{1400 + i}" for i in range(1, REFUND_RING_SIZE + 1)]
    if db.get(ReceiveInfo, REFUND_RING_ADDRESS) is None:
        db.add(ReceiveInfo(
            receive_id=REFUND_RING_ADDRESS, user_id=ring_users[0],
            receiver_name="团伙收货人", receiver_phone="13900008003",
            receive_province="浙江省", receive_city="杭州市", receive_district="余杭区",
            receive_street_address="XX小区 1 号",
        ))
    for uid in ring_users:
        if db.get(UserInfo, uid) is None:
            db.add(UserInfo(user_id=uid, register_time=ts(random.uniform(60, 300)),
                            user_level="普通"))
        addr_of.setdefault(uid, []).append(REFUND_RING_ADDRESS)

    # 事件数据集里的其余团伙账号：羊毛党 / 黄牛 / 投诉集中账号。
    # 各自给 1~2 个独立收货地址，保证 order_info.receive_id 自洽
    # （订单的收货地址必须属于该用户）。
    extra_users = FARM_USERS + SCALPER_USERS + COMPLAINER_USERS
    for uid in extra_users:
        if db.get(UserInfo, uid) is None:
            db.add(UserInfo(user_id=uid, register_time=ts(random.uniform(10, 120)),
                            user_level=random.choice(["普通", "普通", "VIP"])))
        if uid not in addr_of:
            addrs = []
            for k in range(random.randint(1, 2)):
                rid = f"RCV-{uid[2:]}-{k + 1}"
                if db.get(ReceiveInfo, rid) is None:
                    province, city, district = _random_region()
                    db.add(ReceiveInfo(
                        receive_id=rid, user_id=uid,
                        receiver_name=f"收货人{uid[2:]}",
                        receiver_phone=f"13{random.randint(100000000, 999999999)}",
                        receive_province=province, receive_city=city,
                        receive_district=district,
                        receive_street_address=f"{city}{district}{random.randint(1, 200)}号",
                    ))
                addrs.append(rid)
            addr_of[uid] = addrs

    db.flush()
    total_addrs = sum(len(v) for v in addr_of.values()) + 1
    return user_ids + ring_users + extra_users, addr_of


def _random_region() -> tuple[str, str, str]:
    province, cities, districts = random.choice(PROVINCES)
    return province, random.choice(cities), random.choice(districts)


def gen_skus(db) -> tuple[list[tuple[str, str, float]], list[tuple[str, str, float]]]:
    """sku_info：120 个 SKU，价格**双峰分布**。

    - 大众商品（84 个，¥39~¥900）：覆盖正常用户的日常消费
    - 高价值商品（36 个，¥3000~¥9000）：用于黄牛囤货 / 恶意退款团伙场景

    这样 `user_avg_order_amount` 才对正常用户与团伙有区分度 ——
    若价格均匀分布在 39~8999，几乎所有人都会被「平均订单金额异常」误命中。
    """
    cheap, premium = [], []
    for i in range(1, 121):
        sku_id = f"SKU-{i:04d}"
        category = CATEGORIES[(i - 1) % len(CATEGORIES)]
        name = f"{random.choice(SKU_NAMES)} {category}-{i:03d}"
        if i <= 84:
            price = round(random.uniform(39, 900), 2)
            cheap.append((sku_id, category, price))
        else:
            price = round(random.uniform(3000, 9000), 2)
            premium.append((sku_id, category, price))
        if db.get(SkuInfo, sku_id) is None:
            db.add(SkuInfo(sku_id=sku_id, sku_name=name, sku_price=price,
                           sku_category=category, sku_count=random.randint(0, 500)))
    db.flush()
    return cheap, premium


def gen_orders(db, user_ids: list[str], addr_of: dict,
               skus: tuple[list, list]) -> dict:
    """order_info + order_detail：600 单 + 约 1200 明细。

    商品按用户类型选取：团伙用高价值 SKU、正常用户用大众 SKU，
    使 `user_avg_order_amount` / `order_total_amount` 具备区分度。
    """
    cheap_skus, premium_skus = skus
    # ⚠ 用**显式集合**判定团伙，不要用 `u >= "U-1400"` 这类字符串比较：
    #   黄牛账号 U-1601 在字典序上也 ≥ "U-1400"，会被误判成地址团伙，
    #   导致「9 个账号共享地址」的信号被稀释（共享地址上多出 7 个黄牛账号）。
    ring_users = [f"U-{1400 + i}" for i in range(1, REFUND_RING_SIZE + 1)]
    farm_users = list(FARM_USERS)
    scalper_users = list(SCALPER_USERS)
    ring_set = set(ring_users)
    # 高退款用户只在**正常用户**里选（U-1001 ~ U-1200），不牵扯团伙账号
    normal_users = [u for u in user_ids if u.startswith("U-1") and 1000 <= int(u[2:]) < 1200]
    high_refund = set(random.sample(normal_users,
                                    min(HIGH_REFUND_USERS, len(normal_users) - 20)))
    order_ids: list[str] = []
    detail_ids: list[str] = []

    for i in range(1, 601):
        order_id = f"O-{i:06d}"
        # 团伙账号集中下单，其余正常分布
        roll = random.random()
        if ring_set and roll < 0.12:
            uid = random.choice(ring_users)
            days_ago = random.uniform(1, 40)
            sku_pool = premium_skus
        elif roll < 0.16:
            # 黄牛：近期高频大额下单（对应事件数据集「黄牛囤货」场景）
            uid = random.choice(scalper_users)
            days_ago = random.uniform(0.5, 20)
            sku_pool = premium_skus
        elif roll < 0.22:
            # 羊毛党：近期小额多单（对应「批量领券+下单」场景）
            uid = random.choice(farm_users)
            days_ago = random.uniform(0.5, 15)
            sku_pool = cheap_skus
        elif roll < 0.28:
            # 投诉集中账号：有订单才有物流可投诉，也让 user_complaint_count 有业务背景
            uid = random.choice(COMPLAINER_USERS)
            days_ago = random.uniform(1, 60)
            sku_pool = cheap_skus
        elif random.random() < 0.18:
            uid = random.choice(sorted(high_refund))
            days_ago = random.uniform(1, 60)
            sku_pool = premium_skus if random.random() < 0.5 else cheap_skus
        else:
            uid = random.choice(user_ids)
            days_ago = random.uniform(0.5, 90)
            sku_pool = cheap_skus

        create_time = ts(days_ago)
        addrs = addr_of.get(uid) or ["RCV-0001"]
        receive_id = REFUND_RING_ADDRESS if uid in ring_users and random.random() < 0.8 \
            else random.choice(addrs)

        # 状态：近期多为已支付/已发货，久远的已完成
        if days_ago < 3:
            status = random.choice(["待付款", "已支付", "已发货"])
        elif days_ago < 30:
            status = random.choice(["已支付", "已发货", "已签收", "退款中", "已取消"])
        else:
            status = random.choice(["已完成", "已完成", "已完成", "已取消"])

        payment_time = None
        delivered_time = None
        complete_time = None
        if status != "待付款":
            # 团伙账号支付间隔极短（脚本行为），正常用户 1 分钟 ~ 2 小时
            gap = random.uniform(2, 20) if uid in ring_users else random.uniform(60, 7200)
            payment_time = create_time + timedelta(seconds=gap)
        if status in ("已发货", "已签收", "已完成", "退款中"):
            delivered_time = (payment_time or create_time) + timedelta(days=random.uniform(1, 5))
        if status == "已完成":
            complete_time = (delivered_time or create_time) + timedelta(days=random.uniform(2, 10))

        if db.get(OrderInfo, order_id) is None:
            db.add(OrderInfo(
                order_id=order_id, create_time=create_time, payment_time=payment_time,
                delivered_time=delivered_time, complete_time=complete_time,
                user_id=uid, receive_id=receive_id, order_status=status,
            ))

        # 1~3 条明细
        for j in range(random.randint(1, 3)):
            detail_id = f"OD-{i:06d}-{j + 1}"
            sku_id, _, price = random.choice(sku_pool)
            count = random.randint(1, 3) if uid not in ring_users else random.randint(1, 8)
            total = round(price * count, 2)
            discount = round(total * random.uniform(0, 0.35), 2)
            detail_ids.append(detail_id)
            if db.get(OrderDetail, detail_id) is None:
                db.add(OrderDetail(
                    order_detail_id=detail_id, order_id=order_id, sku_id=sku_id,
                    sku_name=f"商品-{sku_id}", sku_count=count,
                    total_amount=total, discount_amount=discount,
                    final_amount=round(total - discount, 2),
                ))
        order_ids.append(order_id)

    # ---- 兜底补单：保证每个业务用户至少有 1 单 ----
    #
    # ⚠ 为什么必须补（v3.1）：600 单按随机分配时约 28% 的用户一单也没有。
    #   事件数据集里这些用户的 `order_create` 事件找不到可绑定的订单 →
    #   订单维度 8 个特征与金额恒为 0 → 相关规则永不命中、
    #   「挽回资损」少算、「拦截订单」无从联动。
    #   补齐后「有用户就必然有订单」，事件绑定覆盖率接近 100%。
    # ⚠ 必须先 flush：`autoflush=False` 时 query() 看不到本次会话新增的订单
    db.flush()
    covered = {row[0] for row in db.query(OrderInfo.user_id).distinct().all()}
    missing = [u for u in user_ids if u not in covered]
    next_seq = 601
    for uid in missing:
        order_id = f"O-{next_seq:06d}"
        next_seq += 1
        create_time = ts(random.uniform(1, 80))
        sku_id, _, price = random.choice(cheap_skus)
        count = random.randint(1, 2)
        total = round(price * count, 2)
        discount = round(total * random.uniform(0, 0.35), 2)
        addrs = addr_of.get(uid) or ["RCV-0001"]
        if db.get(OrderInfo, order_id) is None:
            db.add(OrderInfo(
                order_id=order_id, create_time=create_time,
                payment_time=create_time + timedelta(seconds=random.uniform(60, 7200)),
                user_id=uid, receive_id=random.choice(addrs),
                order_status=random.choice(["已支付", "已发货", "已完成"]),
            ))
            db.add(OrderDetail(
                order_detail_id=f"OD-{next_seq:06d}-1", order_id=order_id,
                sku_id=sku_id, sku_name=f"商品-{sku_id}", sku_count=count,
                total_amount=total, discount_amount=discount,
                final_amount=round(total - discount, 2),
            ))
            order_ids.append(order_id)
            detail_ids.append(f"OD-{next_seq:06d}-1")

    db.flush()
    return {"order_ids": order_ids, "detail_ids": detail_ids,
            "ring_users": ring_users, "high_refund": sorted(high_refund),
            "topped_up": len(missing)}


def gen_logistics(db, order_ids: list[str]) -> None:
    """logistics + order_logistics + 投诉记录。"""
    logistics_ids = [f"LG-{i:05d}" for i in range(1, 501)]
    for lg_id in logistics_ids:
        if db.get(Logistics, lg_id) is None:
            created = ts(random.uniform(1, 80))
            db.add(Logistics(
                logistics_id=lg_id, logistics_category=random.choice(COMPANIES),
                create_time=created,
                delivered_time=created + timedelta(days=random.uniform(1, 6)),
            ))

    for idx, order_id in enumerate(order_ids[:600]):
        lg_id = logistics_ids[idx % len(logistics_ids)]
        if db.get(OrderLogistics, (order_id, lg_id)) is None:
            db.add(OrderLogistics(order_id=order_id, logistics_id=lg_id))

    # 80 条投诉记录（集中在少数用户，制造 user_complaint_count 信号）
    #
    # ⚠ `logistics_complaints_record` 的主键是**自增** `record_id`，没有业务唯一键，
    #   因此没法像其它表那样用 `db.get(主键)` 做 upsert。v3.1 之前这里是无条件
    #   `db.add()` —— 生成脚本每跑一次就多 80 条，跑三次变 240 条，
    #   `user_complaint_count` 随之翻倍，R-022「物流投诉频繁」的命中率被悄悄放大。
    #   派生数据先清后插，保证「跑 N 次 = 跑 1 次」。
    removed = db.query(LogisticsComplaintsRecord).delete()
    if removed:
        print(f"  （已先清空 {removed} 条历史投诉记录，保证重复执行结果一致）")

    complainers = ["U-1500", "U-1501", "U-1502", "U-1503", "U-1504"]
    for i in range(1, 81):
        uid = complainers[i % len(complainers)] if random.random() < 0.75 else "U-1001"
        db.add(LogisticsComplaintsRecord(
            logistics_id=random.choice(logistics_ids),
            complaint_time=ts(random.uniform(1, 60)), user_id=uid,
            logistics_status=random.choice(LOGISTICS_STATUSES),
            logistics_complaint=random.choice(LOGISTICS_COMPLAINTS),
        ))
    db.flush()


def gen_postsale(db, detail_ids: list[str], ring_users: list[str],
                 high_refund: list[str]) -> None:
    """postsale + postsale_logistics：150 条售后。"""
    # id → order_id 映射（从 detail_id 解析，便于按用户筛）
    refund_reasons = ["未收到货", "质量问题", "与描述不符"]
    postsale_ids: list[str] = []

    for i in range(1, 151):
        postsale_id = f"PS-{i:05d}"
        detail_id = random.choice(detail_ids)
        # 从 OD-000123-1 解析订单号
        try:
            order_seq = detail_id.split("-")[1]
            order_id = f"O-{order_seq}"
        except IndexError:
            order_id = "O-000001"

        is_ring = random.random() < 0.35
        postsale_type = random.choice(["退款", "退款", "退货", "换货"])
        status = random.choice(["已退款", "已退款", "退款中", "已拒绝"]) if is_ring \
            else random.choice(["已退款", "退款中", "已拒绝", "待处理"])
        reason = random.choice(refund_reasons) if is_ring \
            else random.choice(POSTSALE_REASONS)

        if db.get(Postsale, postsale_id) is None:
            db.add(Postsale(
                postsale_id=postsale_id, order_detail_id=detail_id,
                refund_amount=round(random.uniform(50, 8000), 2),
                postsale_type=postsale_type, postsale_status=status,
                postsale_reason=reason, create_time=ts(random.uniform(1, 60)),
            ))
        postsale_ids.append(postsale_id)

    for idx, ps_id in enumerate(postsale_ids[:120]):
        lg_id = f"LG-{(idx % 500) + 1:05d}"
        if db.get(PostsaleLogistics, (ps_id, lg_id)) is None:
            db.add(PostsaleLogistics(postsale_id=ps_id, logistics_id=lg_id))
    db.flush()


def clean(db) -> None:
    """清空 17 张业务表（按外键依赖倒序）。"""
    for model in (PostsaleLogistics, Postsale, LogisticsComplaintsRecord,
                  OrderLogistics, OrderDetail, OrderInfo, Logistics,
                  ReceiveInfo, SkuInfo, Region, ProductCategory, OrderStatus,
                  PostsaleStatus, PostsaleReason, LogisticsCompany,
                  LogisticsComplaint, UserInfo):
        db.execute(delete(model))


# ================================================================ 主入口
def main() -> None:
    parser = argparse.ArgumentParser(description="ERCS 业务数据生成")
    # ⚠ v3.1 起**默认先清空再生成**（原先默认是增量 upsert，`--clean` 才清空）。
    #
    #   为什么改：脚本各表用 `db.get(主键)` 做幂等，但对「自增主键 + 无业务唯一键」的表
    #   （如 `logistics_complaints_record`）以及「循环次数由 random 决定」的表
    #   （如 `order_detail` 每个订单 1~3 行）根本不成立。结果是**每次重跑都会追加行**：
    #     · 投诉记录 80 → 240（跑三次）
    #     · 订单明细越跑越多，订单金额随之上浮
    #   数据看起来正常，但「固定种子 ⇒ 可复现」（SPEC §10.2）这条承诺已经不成立，
    #   而回放出来的风险分布会随重跑次数漂移。
    #
    #   这 17 张表全是合成的派生数据，清空重建是唯一能保证可复现的做法。
    #
    # ⚠ 另一个坑：`SessionLocal` 配的是 `autoflush=False`（database.py），
    #   所以 `db.query(...)` **不会**把尚未提交的 `db.add()` 刷进 SQL。
    #   下面算 `covered` 之前必须先 `db.flush()`，否则查出的是「清空后的空表」，
    #   于是每个用户都被判为「没有订单」→ 给所有人各补一单（600 变 831）。
    #   这类问题不会报错，只会让数据量悄悄翻倍。
    parser.add_argument("--clean", action="store_true", default=True,
                        help="先清空业务表再生成（默认开启，保证可复现）")
    parser.add_argument("--no-clean", dest="clean", action="store_false",
                        help="增量生成，不清空（仅用于往已有库补数据，会导致数据不可复现）")
    args = parser.parse_args()

    create_all()

    with session_scope() as db:
        if args.clean:
            clean(db)
            db.flush()
            print("  已清空 17 张业务表（保证重复执行结果一致）")

        ref = gen_reference(db)
        user_ids, addr_of = gen_users_and_addresses(db)
        cheap_skus, premium_skus = gen_skus(db)
        skus = (cheap_skus, premium_skus)
        result = gen_orders(db, user_ids, addr_of, skus)
        gen_logistics(db, result["order_ids"])
        gen_postsale(db, result["detail_ids"], result["ring_users"], result["high_refund"])

    print("\n" + "=" * 62)
    print("  业务数据生成完成")
    print("=" * 62)
    print(f"  用户            {len(user_ids):>6}  （含 {REFUND_RING_SIZE} 个地址团伙账号）")
    print(f"  收货地址        {sum(len(v) for v in addr_of.values()) + 1:>6}")
    print(f"  SKU             {len(cheap_skus) + len(premium_skus):>6}  （大众 {len(cheap_skus)} / 高价值 {len(premium_skus)}）")
    print(f"  订单            {len(result['order_ids']):>6}  （含兜底补单 {result.get('topped_up', 0)}，保证每人至少 1 单）")
    print(f"  订单明细        {len(result['detail_ids']):>6}")
    print(f"  售后单          {150:>6}")
    print(f"  高退款用户      {len(result['high_refund']):>6}")
    print("-" * 62)
    print("  参考数据：")
    for k, v in ref.items():
        print(f"    {k:<24}{v:>6}")
    print("=" * 62)
    print(f"  时间跨度：90 天（{ts(90).date()} ~ {NOW.date()}）")
    print(f"  团伙共享地址：{REFUND_RING_ADDRESS}\n")


if __name__ == "__main__":
    main()
