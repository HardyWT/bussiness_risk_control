"""init_db.py —— 建库 + 建表 + 灌入初始配置。

职责（SPEC §10.1）：
  1. 建业务表（17 张）+ 风控表（16 张）
  2. 创建审计不可篡改触发器
  3. 灌入 5 个演示账号（bcrypt 现场哈希）
  4. 灌入场景配置（7 条：六类事件 + all）
  5. 灌入 30 条默认规则（其中 6 条 challengeable = 0）
  6. 灌入初始名单：黑名单 3 / 白名单 2 / 灰名单 1
  7. 导出 DDL 到 sql/ 目录（便于评审与换库）
  8. 输出灌入结果汇总表到控制台与 data/init_report.json

用法：
    python scripts/init_db.py            # 建库建表 + 补齐缺失配置
    python scripts/init_db.py --reset    # 先清空风控数据再灌入
    python scripts/init_db.py --ddl-only # 仅导出 DDL（不连库写数据）
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func  # noqa: E402

from app import models  # noqa: E402
from app.config import DEMO_USERS  # noqa: E402
from app.database import (Base, create_all, drop_all, ensure_database,  # noqa: E402
                          engine, ping, session_scope)
from app.models import (RiskBlacklist, RiskRule, RiskRuleScene,  # noqa: E402
                        RiskUser)
from app.services.step0_auth import hash_password  # noqa: E402
from app.utils import new_id, now_dt  # noqa: E402

# Windows 控制台默认 GBK，打印 ✓ / ✗ / ⚠ 会抛 UnicodeEncodeError。
# 统一把标准输出强制为 UTF-8（与 run.py / start.py 一致）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL_DIR = os.path.join(BASE_DIR, "sql")
REPORT_FILE = os.path.join(BASE_DIR, "data", "init_report.json")

# ---------------------------------------------------------------- 场景（SPEC §10.1 第 4 项）
SCENES = [
    ("login", "登录", "login"),
    ("coupon_receive", "营销 · 领券", "coupon_receive"),
    ("order_create", "交易 · 下单", "order_create"),
    ("order_pay", "支付", "order_pay"),
    ("after_sale_apply", "售后 · 退款申请", "after_sale_apply"),
    ("logistics_complaint", "物流 · 投诉", "logistics_complaint"),
    ("all", "全场景", "*"),
]

# ---------------------------------------------------------------- 30 条规则（PRD 附录 C）
# (编码, 名称, 分类, 适用事件, 条件树, 等级, 分值, 优先级, 可挑战, 备注)
CH = {"and": lambda *xs: {"and": list(xs)}}
def LEAF(f, op, v):          # noqa: E704  叶子节点构造简写
    return {"field": f, "op": op, "value": v}


RULES: list[tuple] = [
    ("R-001", "高退款率用户", "售后滥用", "after_sale_apply",
     LEAF("user_refund_rate", ">", 0.5), "高", 70, 100, 1, "退款率超过 50%"),
    ("R-002", "新用户大额下单", "订单欺诈", "order_create",
     CH["and"](LEAF("user_total_orders", "<=", 2), LEAF("order_total_amount", ">=", 3000)),
     "高", 65, 100, 1, "历史订单 ≤ 2 且单笔 ≥ 3000"),
    ("R-003", "夜间大额下单", "订单欺诈", "order_create",
     CH["and"](LEAF("order_is_night", "==", 1), LEAF("order_total_amount", ">", 5000)),
     "极高", 90, 95, 0, "0:00-5:59 大额下单（不可挑战）"),
    ("R-004", "高频退款索赔", "售后滥用", "after_sale_apply",
     CH["and"](LEAF("user_refund_count", ">=", 5), LEAF("user_refund_rate", ">", 0.7)),
     "极高", 90, 95, 0, "退款次数 ≥ 5 且退款率 > 70%（不可挑战）"),
    ("R-005", "同设备多账号", "账户风险", "通用",
     LEAF("device_account_cnt_24h", ">", 5), "极高", 90, 95, 0,
     "同设备关联账号 > 5（设备农场，不可挑战）"),
    ("R-006", "地址团伙退款", "地址风险", "after_sale_apply",
     CH["and"](LEAF("addr_account_cnt_7d", ">", 3), LEAF("phone_refund_rate_24h", ">", 0.7)),
     "高", 70, 90, 0, "同地址多账号 + 同手机号高退款率（不可挑战）"),
    ("R-007", "设备高频下单", "账户风险", "order_create",
     LEAF("device_order_cnt_1h", ">", 5), "高", 75, 90, 0,
     "同设备 1h 下单 > 5 次（不可挑战）"),
    ("R-008", "短时高频领券", "账户风险", "coupon_receive",
     LEAF("device_coupon_cnt_1h", ">", 10), "中", 45, 85, 1, "同设备 1h 领券 > 10 次"),
    ("R-009", "IP 代理聚集", "账户风险", "通用",
     LEAF("ip_account_cnt_24h", ">", 8), "中", 40, 85, 1, "同 IP 关联账号 > 8"),
    ("R-010", "IP 聚集下单", "账户风险", "order_create",
     LEAF("ip_order_cnt_1h", ">=", 10), "高", 60, 85, 1, "同 IP 1h 下单 ≥ 10 次"),
    ("R-011", "新地址大额下单", "地址风险", "order_create",
     CH["and"](LEAF("addr_is_new", "==", 1), LEAF("order_total_amount", ">=", 3000)),
     "中", 50, 80, 1, "新收货地址 + 大额订单"),
    ("R-012", "地址数量异常", "地址风险", "通用",
     LEAF("addr_total_count", ">=", 10), "中", 40, 80, 1, "账号下地址数 ≥ 10"),
    ("R-013", "跨省地址异常", "地址风险", "通用",
     LEAF("addr_province_count", ">=", 5), "中", 35, 75, 1, "收货地址跨 ≥ 5 个省份"),
    ("R-014", "支付间隔过短", "支付风险", "order_pay",
     CH["and"](LEAF("order_pay_interval_sec", "<", 10), LEAF("order_pay_interval_sec", ">=", 0)),
     "中", 45, 80, 1, "下单到支付 < 10 秒（脚本行为）"),
    ("R-015", "高折扣异常", "支付风险", "order_create",
     LEAF("order_discount_rate", ">", 0.8), "中", 40, 75, 1, "折扣率 > 80%"),
    ("R-016", "单订单件数异常", "订单欺诈", "order_create",
     LEAF("order_sku_count", ">=", 50), "中", 45, 75, 1, "单笔订单商品件数 ≥ 50"),
    ("R-017", "单订单明细行数异常", "订单欺诈", "order_create",
     LEAF("order_item_count", ">=", 20), "中", 40, 70, 1, "单笔订单明细行 ≥ 20"),
    ("R-018", "商品类别异常分散", "订单欺诈", "order_create",
     LEAF("order_category_count", ">=", 8), "低", 25, 60, 1, "单笔订单涉及 ≥ 8 个品类"),
    ("R-019", "高频下单用户", "订单欺诈", "order_create",
     LEAF("user_orders_7d", ">=", 20), "高", 60, 85, 1, "近 7 天下单 ≥ 20 笔"),
    ("R-020", "高频售后用户", "售后滥用", "after_sale_apply",
     LEAF("user_postsale_count", ">=", 8), "高", 65, 85, 1, "累计售后 ≥ 8 次"),
    ("R-021", "取消订单异常", "账户风险", "通用",
     LEAF("user_cancel_count", ">=", 8), "中", 40, 70, 1, "累计取消订单 ≥ 8 次"),
    ("R-022", "物流投诉频繁", "物流风险", "logistics_complaint",
     LEAF("user_complaint_count", ">=", 5), "中", 45, 80, 1, "累计物流投诉 ≥ 5 次"),
    ("R-023", "退款金额异常", "售后滥用", "after_sale_apply",
     LEAF("user_refund_amount", ">=", 20000), "高", 60, 80, 1, "累计退款金额 ≥ 20000"),
    ("R-024", "售后率异常", "售后滥用", "after_sale_apply",
     LEAF("user_postsale_rate", ">", 0.6), "中", 50, 80, 1, "售后率 > 60%"),
    ("R-025", "零订单用户下单", "订单欺诈", "order_create",
     CH["and"](LEAF("user_total_orders", "==", 0), LEAF("order_total_amount", ">=", 1000)),
     "中", 55, 85, 1, "无历史订单却下大额单"),
    ("R-026", "新用户高频领券", "账户风险", "coupon_receive",
     CH["and"](LEAF("user_total_orders", "<=", 1), LEAF("device_coupon_cnt_1h", ">=", 3)),
     "中", 45, 80, 1, "新用户同设备高频领券"),
    ("R-027", "事件频次异常", "账户风险", "通用",
     LEAF("user_event_cnt_1h", ">=", 30), "中", 40, 70, 1, "单用户 1h 事件 ≥ 30 条"),
    ("R-028", "高价值订单夜间支付", "支付风险", "order_pay",
     CH["and"](LEAF("order_is_night", "==", 1), LEAF("order_total_amount", ">=", 3000)),
     "高", 60, 85, 1, "夜间支付高价值订单"),
    ("R-029", "平均订单金额异常", "订单欺诈", "通用",
     LEAF("user_avg_order_amount", ">=", 8000), "中", 40, 65, 1, "平均订单金额 ≥ 8000"),
    ("R-030", "手机号团伙退款", "售后滥用", "after_sale_apply",
     CH["and"](LEAF("phone_refund_rate_24h", ">=", 0.9), LEAF("user_refund_count", ">=", 3)),
     "极高", 90, 95, 0, "同手机号退款率 ≥ 90%（不可挑战）"),
]

# ---------------------------------------------------------------- 初始名单（SPEC §10.1 第 6 项）
WATCHLIST = [
    ("black", "device", "DEV-FARM1", "设备农场 · 多账号聚集"),
    ("black", "phone", "13800001234", "恶意退款索赔"),
    ("black", "ip", "10.22.33.44", "代理 IP 聚集"),
    ("white", "user", "U-10086", "可信大客户"),
    ("white", "user", "U-10087", "内部测试账号"),
    ("gray", "device", "DEV-GRAY01", "观望期设备，仅记录"),
]


# ================================================================ 灌入
def seed_config_only() -> dict:
    """仅灌入配置类数据（规则 / 场景 / 名单 / 账号），不动业务数据。"""
    now = now_dt()
    with session_scope() as db:
        # 3. 演示账号
        for u in DEMO_USERS:
            if db.query(RiskUser).filter(RiskUser.username == u["username"]).first() is None:
                db.add(RiskUser(
                    user_id=u["user_id"], username=u["username"],
                    password_hash=hash_password(u["password"]),
                    display_name=u["display_name"], role=u["role"],
                    status="active", fail_count=0, create_time=now,
                ))

        # 4. 场景配置
        for code, name, biz in SCENES:
            if db.query(RiskRuleScene).filter(RiskRuleScene.scene_code == code).first() is None:
                db.add(RiskRuleScene(
                    scene_code=code, scene_name=name, biz_event=biz,
                    pass_threshold=30, mark_threshold=60, reject_threshold=80,
                    multi_rule_bonus=3, veto_min_score=90, enabled=1,
                ))

        # 5. 30 条默认规则
        for code, name, cat, evt, cond, level, score, prio, ch, desc in RULES:
            if db.query(RiskRule).filter(RiskRule.rule_id == code).first() is None:
                db.add(RiskRule(
                    rule_id=code, rule_name=name, rule_category=cat, event_type=evt,
                    rule_condition=cond, risk_level=level, risk_score=score,
                    action=_action_of(level), challengeable=ch, priority=prio,
                    is_enabled=1, version=1, description=desc,
                    updated_by="system", create_time=now, update_time=now,
                ))

        # 6. 初始名单
        for list_type, entity_type, value, reason in WATCHLIST:
            exists = (db.query(RiskBlacklist)
                      .filter(RiskBlacklist.list_type == list_type)
                      .filter(RiskBlacklist.entity_type == entity_type)
                      .filter(RiskBlacklist.entity_value == value).first())
            if exists is None:
                db.add(RiskBlacklist(
                    list_type=list_type, entity_type=entity_type, entity_value=value,
                    reason=reason, operator="system", expire_time=None,
                    hit_count=0, create_time=now,
                ))

    return report()


def _action_of(level: str) -> str:
    """风险等级 → 触发动作（Demo Doc-02 §3.3.1 枚举）。"""
    return {"低": "通过", "中": "标记", "高": "人工审核", "极高": "拒绝"}.get(level, "标记")


def report() -> dict:
    """统计各表行数。"""
    from app.models import (RiskAssessment, RiskBlacklist, RiskCase,
                            RiskChallengeRecord, RiskEntity,
                            RiskEntityRelation, RiskEvent, RiskFeature,
                            RiskRule, RiskRuleScene, RiskUser, RiskUserProfile)

    tracked = [RiskRuleScene, RiskRule, RiskBlacklist, RiskUser, RiskEvent,
               RiskFeature, RiskAssessment, RiskCase, RiskUserProfile,
               RiskEntity, RiskEntityRelation, RiskChallengeRecord]
    out = {}
    with session_scope() as db:
        for model in tracked:
            out[model.__tablename__] = db.query(func.count()).select_from(model).scalar() or 0
    return out


# ================================================================ DDL 导出
def export_ddl() -> dict[str, str]:
    """把 ORM 元数据导出为 MySQL DDL，写入 sql/ 目录（单一事实来源）。"""
    from sqlalchemy.schema import CreateIndex, CreateTable

    os.makedirs(SQL_DIR, exist_ok=True)

    business_tables = ["user_info", "region", "receive_info", "product_category",
                       "sku_info", "order_status", "order_info", "order_detail",
                       "logistics", "order_logistics", "logistics_company",
                       "logistics_complaint", "logistics_complaints_record",
                       "postsale_status", "postsale_reason", "postsale",
                       "postsale_logistics"]
    risk_tables = [t.name for t in Base.metadata.sorted_tables
                   if t.name not in business_tables]

    def dump(tables: list[str], title: str, extra: str = "") -> str:
        lines = [f"-- ERCS v3.1 · {title}",
                 "-- 由 scripts/init_db.py --ddl-only 从 ORM 元数据生成（单一事实来源）",
                 "-- 含建表语句、索引与外键约束。",
                 "",
                 "SET FOREIGN_KEY_CHECKS = 0;", ""]
        for name in tables:
            table = Base.metadata.tables.get(name)
            if table is None:
                continue
            lines.append(str(CreateTable(table).compile(engine)).strip() + ";")
            for idx in table.indexes:
                lines.append(str(CreateIndex(idx).compile(engine)).strip() + ";")
            lines.append("")
        lines.append("SET FOREIGN_KEY_CHECKS = 1;")
        if extra:
            lines.append("")
            lines.append(extra)
        return "\n".join(lines)

    # 审计触发器不属于 ORM 元数据（由 database.create_audit_triggers() 建），
    # 但导出的 DDL 要能被**独立执行**（SPEC §10.5 的手工建库路径）。
    # v3.0 导出的 sql/*.sql 只有建表语句，照着跑出来的库没有触发器 →
    # 「审计不可篡改」只在应用建表路径下成立，手工建库则形同虚设。
    AUDIT_TRIGGERS = """-- ============================================================
-- 审计日志不可篡改保护（SPEC §6.3 / R-MA-03）
-- ⚠ 这两条触发器是「审计不可篡改」承诺的**数据库侧**保障，
--    与应用层「不提供改删接口」构成双保护，缺一不可。
-- ============================================================
DROP TRIGGER IF EXISTS trg_audit_no_update;
DROP TRIGGER IF EXISTS trg_audit_no_delete;

DELIMITER $$
CREATE TRIGGER trg_audit_no_update BEFORE UPDATE ON risk_audit_log
FOR EACH ROW BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '审计日志不可修改';
END$$
CREATE TRIGGER trg_audit_no_delete BEFORE DELETE ON risk_audit_log
FOR EACH ROW BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '审计日志不可删除';
END$$
DELIMITER ;"""

    files = {
        "init_business_tables.sql": dump(business_tables, "业务表 17 张（只读）"),
        "init_risk_tables.sql": dump(sorted(risk_tables), "风控表 16 张（读写）",
                                     AUDIT_TRIGGERS),
    }
    for filename, content in files.items():
        with open(os.path.join(SQL_DIR, filename), "w", encoding="utf-8") as fh:
            fh.write(content)
    return {k: f"{len(v.splitlines())} 行" for k, v in files.items()}


# ================================================================ 主入口
def main() -> None:
    parser = argparse.ArgumentParser(description="ERCS 建库与初始化")
    parser.add_argument("--reset", action="store_true",
                        help="先清空风控**数据行**再灌入（表结构不动）")
    parser.add_argument("--recreate", action="store_true",
                        help="**删表重建**：按 ORM 元数据重建全部表结构（丢弃全部数据）。"
                             "改了模型（新字段 / 新索引 / 新外键）后必须用它，"
                             "因为 create_all() 不会 ALTER 已存在的表")
    parser.add_argument("--ddl-only", action="store_true", help="仅导出 DDL")
    args = parser.parse_args()

    # 1. 确保数据库存在
    try:
        ensure_database()
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 建库失败（请检查 .env 的 DB_* 配置）：{exc}")
        sys.exit(1)

    ok_db, desc = ping()
    print(f"  {desc}")
    if not ok_db:
        print("✗ 数据库不可用，请检查 backend/.env 的 DB_PASSWORD 等配置")
        sys.exit(1)

    # 2. 建表 + 触发器
    #
    # ⚠ `create_all()` 只建**不存在**的表，不会给已存在的表加字段 / 索引 / 外键。
    #   改了 models.py 后必须用 `--recreate` 走 drop_all → create_all，
    #   否则会出现「模型里有 locked_at、库里没有」这类
    #   `Unknown column` 错误（而且报错点往往在别处，很难定位）。
    if args.recreate:
        drop_all()
        create_all()
        print("  已删表重建（全部表结构按 ORM 元数据重建）")
    else:
        create_all()

    # 7. 导出 DDL
    ddl = export_ddl()

    if args.reset and not args.recreate:
        from sqlalchemy import delete
        from app.models import (RiskAssessment, RiskCase, RiskChallengeRecord,
                                RiskDisposalRecord, RiskEntity,
                                RiskEntityRelation, RiskEvent, RiskFeature,
                                RiskUserProfile)
        # 顺序先子后父，与风控表之间的外键一致（SPEC §4.3）
        with session_scope() as db:
            for model in (RiskDisposalRecord, RiskChallengeRecord, RiskFeature,
                          RiskAssessment, RiskCase, RiskUserProfile,
                          RiskEntityRelation, RiskEntity, RiskEvent):
                db.execute(delete(model))
        print("  已清空历史风控数据（规则 / 名单 / 账号保留）")

    counts = seed_config_only()

    print("\n" + "=" * 62)
    print("  ERCS 初始化完成 —— 灌入结果汇总")
    print("=" * 62)
    print(f"  {'表名':<30}{'行数':>8}")
    print("-" * 62)
    for table, n in counts.items():
        print(f"  {table:<30}{n:>8}")
    print("-" * 62)
    print("  DDL 导出：")
    for name, info in ddl.items():
        print(f"    sql/{name:<28}{info:>10}")
    print("=" * 62)

    assert counts.get("risk_rule", 0) >= 30, "默认规则应至少 30 条"
    assert counts.get("risk_rule_scene", 0) >= 7, "场景配置应至少 7 条"
    assert counts.get("risk_user", 0) >= 5, "演示账号应至少 5 个"

    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as fh:
        json.dump({**counts, "_ddl": ddl}, fh, ensure_ascii=False, indent=2)
    print(f"  汇总已写入：{REPORT_FILE}\n")

    print("  演示账号：")
    for u in DEMO_USERS:
        print(f"    {u['username']:<12}{u['password']:<18}{u['display_name']}（{u['role']}）")
    print()


if __name__ == "__main__":
    main()
