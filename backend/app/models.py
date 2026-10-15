"""ORM 模型 —— 17 张业务表（只读）+ 16 张风控表（读写）。

对应文档：
- SPEC §1.3 / §2.3 / §3.3 / §4.3 / §5.3 / §6.3 / §9.3 / §11.2 的 DDL
- Demo Doc-02 §3.2 业务表设计（17 张）/ §3.3 风控表设计（7 张）

设计原则（Demo Doc-02 §1.2）：
- **业务表只读映射**：电商平台已有数据，风控系统仅查询，不写入
- **风控表读写**：风控系统新增，完全管理生命周期；事件与特征快照**仅追加**

外键策略（PRD §8.2 / SPEC §5.6 R-PG-09）：风控表之间保留 **4 条真实外键**
（feature→event、challenge→event、disposal→case、agent_message→session），
由数据库保证引用完整性；其余关联为**逻辑外键**，由应用层保证 ——
它们可能跨事务或跨批次写入（如 `risk_entity_relation` 增量更新允许失败），
加物理约束会把「图谱更新失败」升级成「整笔决策链失败」。

⚠ 加外键后 `POST /admin/reset` 的删表顺序必须**先子后父**，见 `routers/admin.py`。
"""

from datetime import datetime

from sqlalchemy import (BigInteger, DateTime, ForeignKeyConstraint,
                        Index, Integer, Numeric, String, Text, UniqueConstraint)
from sqlalchemy.dialects.mysql import JSON, TINYINT
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base

# ============================================================================
# 第一部分：业务表（17 张，只读）
# ============================================================================


class UserInfo(Base):
    """用户信息表（业务）。"""
    __tablename__ = "user_info"

    user_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    register_time: Mapped[datetime | None] = mapped_column(DateTime)
    user_level: Mapped[str | None] = mapped_column(String(20))


class Region(Base):
    """地区表（业务参考数据）。"""
    __tablename__ = "region"

    province: Mapped[str] = mapped_column(String(20), primary_key=True)
    city: Mapped[str] = mapped_column(String(20), primary_key=True)
    district: Mapped[str] = mapped_column(String(20), primary_key=True)


class ReceiveInfo(Base):
    """收货信息表（业务）。"""
    __tablename__ = "receive_info"

    receive_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(50), nullable=False)
    receiver_name: Mapped[str] = mapped_column(String(50), nullable=False)
    receiver_phone: Mapped[str] = mapped_column(String(50), nullable=False)
    receive_province: Mapped[str] = mapped_column(String(50), nullable=False)
    receive_city: Mapped[str] = mapped_column(String(50), nullable=False)
    receive_district: Mapped[str] = mapped_column(String(50), nullable=False)
    receive_street_address: Mapped[str] = mapped_column(String(120), nullable=False)

    __table_args__ = (
        Index("idx_receive_user", "user_id"),
        Index("idx_receive_phone", "receiver_phone"),
    )


class ProductCategory(Base):
    """商品分类表（业务参考数据）。"""
    __tablename__ = "product_category"

    product_category: Mapped[str] = mapped_column(String(20), primary_key=True)


class SkuInfo(Base):
    """商品信息表（业务）。"""
    __tablename__ = "sku_info"

    sku_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    sku_name: Mapped[str] = mapped_column(String(100), nullable=False)
    sku_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    sku_category: Mapped[str] = mapped_column(String(20), nullable=False)
    sku_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class OrderStatus(Base):
    """订单状态表（业务参考数据）。"""
    __tablename__ = "order_status"

    order_status: Mapped[str] = mapped_column(String(20), primary_key=True)
    status_code: Mapped[int | None] = mapped_column(Integer)


class OrderInfo(Base):
    """订单表（业务，特征计算的核心数据源）。"""
    __tablename__ = "order_info"

    order_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    payment_time: Mapped[datetime | None] = mapped_column(DateTime)
    delivered_time: Mapped[datetime | None] = mapped_column(DateTime)
    complete_time: Mapped[datetime | None] = mapped_column(DateTime)
    user_id: Mapped[str] = mapped_column(String(50), nullable=False)
    receive_id: Mapped[str | None] = mapped_column(String(50))
    order_status: Mapped[str] = mapped_column(String(20), nullable=False)

    __table_args__ = (
        Index("idx_order_user", "user_id", "create_time"),
        Index("idx_order_receive", "receive_id"),
        Index("idx_order_status", "order_status"),
    )


class OrderDetail(Base):
    """订单详细表（业务）。"""
    __tablename__ = "order_detail"

    order_detail_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(50), nullable=False)
    sku_id: Mapped[str] = mapped_column(String(50), nullable=False)
    sku_name: Mapped[str] = mapped_column(String(100), nullable=False)
    sku_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    total_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    discount_amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    final_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)

    __table_args__ = (Index("idx_detail_order", "order_id"),)


class Logistics(Base):
    """物流表（业务）。"""
    __tablename__ = "logistics"

    logistics_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    logistics_category: Mapped[str | None] = mapped_column(String(50))
    create_time: Mapped[datetime | None] = mapped_column(DateTime)
    delivered_time: Mapped[datetime | None] = mapped_column(DateTime)


class OrderLogistics(Base):
    """订单物流关联表（业务，多对多）。"""
    __tablename__ = "order_logistics"

    order_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    logistics_id: Mapped[str] = mapped_column(String(50), primary_key=True)


class LogisticsCompany(Base):
    """物流公司表（业务参考数据）。"""
    __tablename__ = "logistics_company"

    company_name: Mapped[str] = mapped_column(String(50), primary_key=True)


class LogisticsComplaint(Base):
    """物流投诉问题表（业务参考数据）。"""
    __tablename__ = "logistics_complaint"

    logistics_status: Mapped[str] = mapped_column(String(20), primary_key=True)
    logistics_complaint: Mapped[str] = mapped_column(String(50), primary_key=True)


class LogisticsComplaintsRecord(Base):
    """物流投诉记录表（业务）。"""
    __tablename__ = "logistics_complaints_record"

    record_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    logistics_id: Mapped[str | None] = mapped_column(String(50))
    complaint_time: Mapped[datetime | None] = mapped_column(DateTime)
    user_id: Mapped[str] = mapped_column(String(50), nullable=False)
    logistics_status: Mapped[str | None] = mapped_column(String(20))
    logistics_complaint: Mapped[str | None] = mapped_column(String(50))

    __table_args__ = (Index("idx_complaint_user", "user_id", "complaint_time"),)


class PostsaleStatus(Base):
    """售后状态表（业务参考数据）。"""
    __tablename__ = "postsale_status"

    postsale_status: Mapped[str] = mapped_column(String(20), primary_key=True)
    is_refund: Mapped[int] = mapped_column(TINYINT, default=0)
    is_return: Mapped[int] = mapped_column(TINYINT, default=0)
    is_exchange: Mapped[int] = mapped_column(TINYINT, default=0)


class PostsaleReason(Base):
    """售后原因表（业务参考数据）。"""
    __tablename__ = "postsale_reason"

    postsale_reason: Mapped[str] = mapped_column(String(50), primary_key=True)
    product_category: Mapped[str | None] = mapped_column(String(20))


class Postsale(Base):
    """售后表（业务，退款率特征的数据源）。"""
    __tablename__ = "postsale"

    postsale_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    order_detail_id: Mapped[str] = mapped_column(String(50), nullable=False)
    refund_amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    postsale_type: Mapped[str] = mapped_column(String(20), nullable=False)  # 退款/退货/换货
    postsale_status: Mapped[str] = mapped_column(String(20), nullable=False)
    postsale_reason: Mapped[str | None] = mapped_column(String(50))
    create_time: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (Index("idx_postsale_detail", "order_detail_id"),)


class PostsaleLogistics(Base):
    """售后物流关联表（业务，多对多）。"""
    __tablename__ = "postsale_logistics"

    postsale_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    logistics_id: Mapped[str] = mapped_column(String(50), primary_key=True)


# ============================================================================
# 第二部分：风控表 —— 配置类（读写）
# ============================================================================


class RiskRule(Base):
    """规则配置表（SPEC §3.3 / FR-31~36）。"""
    __tablename__ = "risk_rule"

    rule_id: Mapped[str] = mapped_column(String(50), primary_key=True)       # R-001
    rule_name: Mapped[str] = mapped_column(String(100), nullable=False)
    rule_category: Mapped[str] = mapped_column(String(20), nullable=False)   # 6 类场景
    event_type: Mapped[str] = mapped_column(String(20), nullable=False, default="通用")
    rule_condition: Mapped[dict] = mapped_column(JSON, nullable=False)       # JSON 条件树
    risk_level: Mapped[str] = mapped_column(String(10), nullable=False)      # 低/中/高/极高
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    action: Mapped[str] = mapped_column(String(20), nullable=False)          # 通过/标记/人工审核/拒绝
    challengeable: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_enabled: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    description: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str | None] = mapped_column(String(64))
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (Index("idx_rule_scene", "event_type", "is_enabled", "priority"),)


class RiskRuleScene(Base):
    """场景阈值配置表（SPEC §3.3 / FR-40a）。"""
    __tablename__ = "risk_rule_scene"

    scene_code: Mapped[str] = mapped_column(String(32), primary_key=True)
    scene_name: Mapped[str] = mapped_column(String(64), nullable=False)
    biz_event: Mapped[str] = mapped_column(String(32), nullable=False)
    pass_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    mark_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    reject_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=80)
    multi_rule_bonus: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    veto_min_score: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    enabled: Mapped[int] = mapped_column(TINYINT, nullable=False, default=1)


class RiskBlacklist(Base):
    """名单库表（SPEC §3.3 / FR-37~40）。五维 × 三类。"""
    __tablename__ = "risk_blacklist"

    blacklist_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    list_type: Mapped[str] = mapped_column(String(10), nullable=False)     # black/white/gray
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)   # user/phone/ip/device/address
    entity_value: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    operator: Mapped[str | None] = mapped_column(String(64))
    expire_time: Mapped[datetime | None] = mapped_column(DateTime)         # NULL = 永久
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("list_type", "entity_type", "entity_value", name="uk_list"),
        Index("idx_bl_lookup", "entity_type", "entity_value", "list_type"),
    )


class RiskUser(Base):
    """系统用户表（SPEC §11.2 / FR-66~70）。"""
    __tablename__ = "risk_user"

    user_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)  # bcrypt
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)            # 五角色
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    fail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 锁定开始时刻（SPEC R-AU-04）。缺此字段无法判断锁定是否已过期，
    # 账号一旦锁死只能改库解锁 —— 而提示文案写着「请 10 分钟后重试」。
    locked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (Index("idx_user_username", "username"),)


# ============================================================================
# 第三部分：风控表 —— 事件与决策（仅追加）
# ============================================================================


class RiskEvent(Base):
    """事件审计表（SPEC §1.3 / FR-01~05）。"""
    __tablename__ = "risk_event"

    event_id: Mapped[str] = mapped_column(String(50), primary_key=True)      # evt + ULID
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(64))
    phone: Mapped[str | None] = mapped_column(String(32))
    address_id: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    event_data: Mapped[dict | None] = mapped_column(JSON)
    event_time: Mapped[datetime | None] = mapped_column(DateTime)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_ev_user", "user_id", "create_time"),
        Index("idx_ev_type", "event_type", "create_time"),
        Index("idx_ev_device", "device_id", "create_time"),
        Index("idx_ev_ip", "ip", "create_time"),
        Index("idx_ev_time", "create_time"),
    )


class RiskFeature(Base):
    """特征快照表（SPEC §2.3 / FR-06~10）。一行一特征。"""
    __tablename__ = "risk_feature"

    feature_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(50), nullable=False)
    dimension: Mapped[str] = mapped_column(String(16), nullable=False)      # 用户/订单/地址/团伙
    feature_name: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_value: Mapped[float | None] = mapped_column(Numeric(15, 4))
    baseline_value: Mapped[float | None] = mapped_column(Numeric(15, 4))
    compute_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_feat_event", "event_id"),
        Index("idx_feat_entity", "dimension", "feature_name"),
        # SPEC R-FE-08 的历史基线查询按 compute_time 过滤 + 按 feature_name 分组，
        # 且挂在**每次风控检查**的主链路上。缺此索引时每个请求全表扫 risk_feature
        # （单事件 33 行，回放 1200 条即 4 万行），直接顶穿 G-01 的 P95 < 500 ms。
        Index("idx_feat_baseline", "compute_time", "feature_name"),
        ForeignKeyConstraint(["event_id"], ["risk_event.event_id"], name="fk_feat_event"),
    )


class RiskAssessment(Base):
    """评估结果表（SPEC §3.4 / FR-09、FR-14~17）。"""
    __tablename__ = "risk_assessment"

    assessment_id: Mapped[str] = mapped_column(String(50), primary_key=True)  # ast + ULID
    event_id: Mapped[str] = mapped_column(String(50), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_results: Mapped[list | None] = mapped_column(JSON)                  # 命中规则明细
    rule_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    final_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    risk_level: Mapped[str] = mapped_column(String(10), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)        # Pass/Mark/Challenge/Review/Reject
    is_veto: Mapped[int] = mapped_column(TINYINT, nullable=False, default=0)
    hit_source: Mapped[str | None] = mapped_column(String(16))               # whitelist/blacklist/gray/rule
    cost_ms: Mapped[float | None] = mapped_column(Numeric(10, 3))
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_ast_user", "user_id", "create_time"),
        Index("idx_ast_decision", "decision"),
        Index("idx_ast_time", "create_time"),
    )


class RiskChallengeRecord(Base):
    """二次验证记录表（SPEC §3.3 / FR-19）。"""
    __tablename__ = "risk_challenge_record"

    challenge_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(50), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="sms")
    target: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    issued_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("idx_ch_event", "event_id"),
        # SPEC R-RE-15 的超时兜底扫描按 (status, issued_at) 过滤待处理挑战，
        # 缺此索引时每次扫描都全表扫 risk_challenge_record。
        Index("idx_ch_pending", "status", "issued_at"),
        ForeignKeyConstraint(["event_id"], ["risk_event.event_id"], name="fk_ch_event"),
    )


# ============================================================================
# 第四部分：风控表 —— 案件与处置
# ============================================================================


class RiskCase(Base):
    """案件表（SPEC §4.3 / FR-21~24）。"""
    __tablename__ = "risk_case"

    case_id: Mapped[str] = mapped_column(String(50), primary_key=True)       # cas + ULID
    assessment_id: Mapped[str] = mapped_column(String(50), nullable=False)
    event_id: Mapped[str] = mapped_column(String(50), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    case_status: Mapped[str] = mapped_column(String(16), nullable=False, default="待审核")
    case_category: Mapped[str | None] = mapped_column(String(20))
    final_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    risk_level: Mapped[str] = mapped_column(String(10), nullable=False)
    assignee: Mapped[str | None] = mapped_column(String(64))
    conclusion: Mapped[str | None] = mapped_column(String(16))               # 放行/拦截/拉黑
    remark: Mapped[str | None] = mapped_column(Text)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    disposed_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        Index("idx_case_status", "case_status", "create_time"),
        Index("idx_case_user", "user_id"),
        Index("idx_case_cat", "case_category"),
        # 案件详情按 assessment_id 反查评估；二次验证升级建案按 event_id 做幂等查询。
        # 二者此前均无索引，是案件详情页与 Challenge 回调的主要慢查询来源。
        Index("idx_case_ast", "assessment_id"),
        Index("idx_case_event", "event_id"),
    )


class RiskDisposalRecord(Base):
    """处置动作明细表（SPEC §4.3 / FR-29）。"""
    __tablename__ = "risk_disposal_record"

    disposal_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(50), nullable=False)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operator: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)          # success/failed
    detail: Mapped[dict | None] = mapped_column(JSON)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_disp_case", "case_id"),
        ForeignKeyConstraint(["case_id"], ["risk_case.case_id"], name="fk_disp_case"),
    )


# ============================================================================
# 第五部分：风控表 —— 画像与图谱
# ============================================================================


class RiskUserProfile(Base):
    """用户风险画像表（累计，SPEC §5.3 / FR-25）。"""
    __tablename__ = "risk_user_profile"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    risk_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    risk_level: Mapped[str] = mapped_column(String(10), nullable=False, default="低")
    total_orders: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_amount: Mapped[float] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    total_refunds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    refund_rate: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, default=0)
    avg_order_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    address_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    complaint_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    assessment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    risk_tags: Mapped[list | None] = mapped_column(JSON)
    last_assessment_time: Mapped[datetime | None] = mapped_column(DateTime)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)


class RiskEntity(Base):
    """风险实体表（SPEC §5.3 / FR-27）。"""
    __tablename__ = "risk_entity"

    entity_id: Mapped[str] = mapped_column(String(200), primary_key=True)     # USR-/DEV-/IP-/ADDR-/PHONE-
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_value: Mapped[str] = mapped_column(String(200), nullable=False)
    risk_tag_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    risk_tags: Mapped[list | None] = mapped_column(JSON)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (UniqueConstraint("entity_type", "entity_value", name="uk_entity"),)


class RiskEntityRelation(Base):
    """实体关联表（图谱边，SPEC §5.3 / FR-27）。"""
    __tablename__ = "risk_entity_relation"

    relation_id: Mapped[str] = mapped_column(String(300), primary_key=True)
    source_entity_id: Mapped[str] = mapped_column(String(200), nullable=False)
    target_entity_id: Mapped[str] = mapped_column(String(200), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(24), nullable=False)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_rel_source", "source_entity_id"),
        Index("idx_rel_target", "target_entity_id"),
    )


# ============================================================================
# 第六部分：风控表 —— 审计与 Agent
# ============================================================================


class RiskAuditLog(Base):
    """审计日志表（仅追加，触发器禁止 UPDATE/DELETE，SPEC §6.3 / FR-53）。"""
    __tablename__ = "risk_audit_log"

    log_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    biz_type: Mapped[str] = mapped_column(String(32), nullable=False)
    biz_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    operator: Mapped[str] = mapped_column(String(64), nullable=False)
    before_value: Mapped[dict | None] = mapped_column(JSON)
    after_value: Mapped[dict | None] = mapped_column(JSON)
    detail: Mapped[dict | None] = mapped_column(JSON)
    role: Mapped[str | None] = mapped_column(String(16))
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_audit_biz", "biz_type", "biz_id"),
        Index("idx_audit_op", "operator", "create_time"),
        Index("idx_audit_time", "create_time"),
    )


class RiskAgentSession(Base):
    """AI 会话表（SPEC §9.3 / FR-56）。"""
    __tablename__ = "risk_agent_session"

    session_id: Mapped[str] = mapped_column(String(50), primary_key=True)     # sess_ + ULID
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    update_time: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (Index("idx_sess_user", "user_id", "update_time"),)


class RiskAgentMessage(Base):
    """AI 对话消息表（SPEC §9.3 / FR-56）。"""
    __tablename__ = "risk_agent_message"

    message_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(50), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)            # human/ai
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_calls: Mapped[list | None] = mapped_column(JSON)
    create_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("idx_msg_session", "session_id", "create_time"),
        ForeignKeyConstraint(["session_id"], ["risk_agent_session.session_id"],
                             name="fk_msg_session"),
    )
