"""replay_events.py —— 按 event_time 顺序回放模拟事件（SPEC §10.4）。

让大盘有真实统计数据。支持 `--limit` 控制回放条数。

两种模式：
  （默认）进程内回放 —— 直接走决策流水线写库，**无需先启动服务**，适合一键初始化
  --http            通过 HTTP 调用 POST /api/v1/risk/check（需服务已启动）

用法：
    python scripts/replay_events.py                     # 全量回放
    python scripts/replay_events.py --limit 200         # 只回放 200 条
    python scripts/replay_events.py --http --url http://127.0.0.1:8000
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import create_all, session_scope  # noqa: E402
from app.models import RiskEvent  # noqa: E402
from app.schemas import RiskCheckRequest  # noqa: E402
from app.services import step1_event as event_svc  # noqa: E402

# Windows 控制台默认 GBK，打印 ✓ / ✗ / ⚠ 会抛 UnicodeEncodeError。
# 统一把标准输出强制为 UTF-8（与 run.py / start.py 一致）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


MOCK_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "mock_events.json")


def _backend_running(url: str = "http://127.0.0.1:8000", timeout: float = 1.5) -> bool:
    """后端是否在运行？（用于拦截「回放与后端并发」这个坑，见 main() 的说明）"""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/v1/admin/health",
                                    timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001  连不上就是没在跑
        return False


def load_events(limit: int | None) -> list[dict]:
    if not os.path.exists(MOCK_FILE):
        print(f"✗ 未找到数据集：{MOCK_FILE}")
        print("  请先执行：python scripts/gen_mock_events.py")
        sys.exit(1)

    with open(MOCK_FILE, encoding="utf-8") as fh:
        events = json.load(fh)
    events.sort(key=lambda e: e["event_time"])       # 按事件时间升序（SPEC §10.4）
    return events[:limit] if limit else events


def _to_request(raw: dict) -> RiskCheckRequest:
    return RiskCheckRequest(
        event_type=raw["event_type"],
        source_id=raw.get("source_id") or f"RPL-{raw['event_id']}",
        user_id=raw["user_id"],
        device_id=raw.get("device_id"),
        ip=raw.get("ip"),
        phone=raw.get("phone"),
        receive_id=raw.get("receive_id"),
        event_time=raw.get("event_time"),
        event_data=raw.get("event_data"),
        dry_run=False,
    )


# 回放会产生/改写的风控侧产物。**每次回放前先清掉自己上一轮的痕迹**，
# 否则「单跑 replay」得到的结果取决于之前跑过几次 —— 那不是可复现的数据集。
SELF_ARTIFACTS = [
    "risk_disposal_record", "risk_challenge_record", "risk_feature",
    "risk_assessment", "risk_case", "risk_user_profile",
    "risk_entity_relation", "risk_entity", "risk_event",
]


def reset_risk_artifacts(quiet: bool = False) -> None:
    """清空回放自己产出的风控数据（保留规则 / 场景 / 名单 / 账号等配置）。

    ⚠ 注意它**不动业务表**：`dispose_case` 的「拦截订单」会把
      `order_info.order_status` 改成「已取消」，「拉黑」会改 `postsale_status`。
      原始值只有 `gen_business_data` 知道，无法在这里还原。
      所以要让整条链路的输出**逐位一致**，请整条流水线重跑：
        init_db --recreate → gen_business_data → gen_mock_events → replay_events
      （`python start.py --reset`）
    """
    from sqlalchemy import text as _text

    from app.models import RiskBlacklist

    with session_scope() as db:
        for table in SELF_ARTIFACTS:
            db.execute(_text(f"DELETE FROM {table}"))
        # 名单里由「回放作业」写入的条目（operator 标记）也一并清掉
        db.query(RiskBlacklist).filter(
            RiskBlacklist.operator == "demo-reviewer").delete(synchronize_session=False)
    if not quiet:
        print("  （已清空上一轮回放的风控产物，保证本次结果不依赖历史运行）")


def replay_in_process(events: list[dict]) -> Counter:
    """进程内回放：直接调用决策流水线。"""
    create_all()
    reset_risk_artifacts()
    decisions: Counter = Counter()
    failed = 0
    # ⚠ 按**原因归类**统计被跳过的事件（v3.1）。
    #   只打一句「跳过 45 条」是不够的 —— 曾经 10 类画像里 9 类的全部事件
    #   因为手机号少了一位被整批拒收，却只体现在这一行数字里，
    #   直到发现「零命中规则一条没少」才回头查。原因必须成组暴露出来。
    skip_reasons: Counter = Counter()
    skip_samples: dict[str, tuple] = {}
    challenge_seq = [0]          # 已处理的二次验证序号（决定通过 / 失败 / 不响应）
    challenge_stats: Counter = Counter()   # 内联回调的结果统计

    with session_scope() as db:
        for i, raw in enumerate(events, start=1):
            try:
                request = _to_request(raw)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                skip_reasons[str(exc)[:90]] += 1
                if skip_samples.get(str(exc)[:90]) is None:
                    skip_samples[str(exc)[:90]] = (raw.get("event_id"), raw.get("event_type"),
                                                   raw.get("user_id"))
                continue

            try:
                result = event_svc.process_event(db, request, dry_run=False)
                decisions[result["decision"]] += 1
                # ⚠ 二次验证必须**当场**回调，不能攒到最后批量做（v3.1 修正）：
                #   挑战记录落库时是 `pending`，而其 `issued_at` 是**历史事件时间**。
                #   后端有一个每 30 秒跑的「超时兜底扫描」，它看到任何 pending 挑战
                #   都会判定「早就超窗了」并升级成 timeout → Review —— 这是符合
                #   R-RE-15 的正确行为，但它会把我们攒着的挑战全部抢走。
                #   于是批量回调时挑战已不在 pending，统计出来是「通过 0 / 失败 0」，
                #   而且**回放不报任何错**（这个 bug 藏了很久）。
                #   每次 Challenge 决策后立刻走一遍真实回调路径，窗口就不存在了。
                if result["decision"] == "Challenge":
                    outcome, err = _resolve_one(db, result["event_id"], challenge_seq[0])
                    challenge_seq[0] += 1
                    if err is None:
                        challenge_stats[outcome or "ignored"] += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                if failed <= 3:
                    print(f"  ⚠ 回放失败 {raw.get('event_id')}：{exc}")

            if i % 200 == 0:
                print(f"  ...已回放 {i}/{len(events)}")

        # 收尾：把内联回调的结果与漏网的 pending 合并统计
        tail = _resolve_challenges(db)
        for key in ("pass", "fail", "ignored"):
            challenge_stats[key] = challenge_stats.get(key, 0) + tail.get(key, 0)
        work_stats = _work_the_case_queue(db)
        role_stats = _role_activity(db)

    # ⚠ 回放结束必须**核对条数**（v3.1 踩过的坑）：
    #   曾经两个回放并发跑，各自 `reset_risk_artifacts()` 清空后互相清掉对方的行，
    #   库里只剩 617 条（数据集 1840 条）且时间轴截断在中途 ——
    #   表现是「最近几天完全没有数据」，但**回放自己一条错都不报**。
    #   落库行数与数据集条数不等就是明确的失败信号，必须显式打出来。
    with session_scope() as check_db:
        written = check_db.query(func.count(RiskEvent.event_id)).scalar() or 0
    expected = len(events)
    if written != expected:
        print(f"\n  ✗ 回放不完整：数据集 {expected} 条，实际落库 {written} 条"
              f"（差 {expected - written} 条）")
        print("    常见原因：**同时有另一个回放在跑**，两边互相清空对方的数据；")
        print("    或回放被中断（Ctrl+C / 进程被杀）。")
        print("    处理：确认没有其它后端 / 回放在跑，然后重跑本脚本。")
        raise SystemExit(2)

    if failed:
        print(f"  ⚠ 跳过 {failed} 条非法/失败事件 —— 按原因：")
        for reason, cnt in skip_reasons.most_common(6):
            sample = skip_samples.get(reason) or ("?", "?", "?")
            print(f"      ×{cnt:<5} {reason}")
            print(f"             例：{sample[0]} / {sample[1]} / user={sample[2]}")
        if len(skip_reasons) > 6:
            print(f"      （另有 {len(skip_reasons) - 6} 类原因未列出）")
    if challenge_stats:
        print(f"  二次验证：通过 {challenge_stats['pass']} 条（→Pass 放行）· "
              f"失败 {challenge_stats['fail']} 条（→Review 人审）· "
              f"未响应 {challenge_stats['ignored']} 条（留给超时兜底 →  Review）")
    if role_stats:
        print(f"  角色作业：策略师改规则 {role_stats['rule_change']} 条 / "
              f"维护名单 {role_stats['blacklist_change']} 条 · "
              f"管理员调阈值 {role_stats['system_config']} 次 · "
              f"分析师调用工具 {role_stats['agent_call']} 次 · "
              f"登录 {role_stats['auth']} 次")
    if work_stats:
        print(f"  审核作业：处置 {work_stats['disposed']} 件"
              f"（其中 {work_stats['kept_disposed']} 件停留在「已处置」，等异步归档）"
              f"（放行 {work_stats['pass']} / 拦截 {work_stats['reject']} / 拉黑 {work_stats['blacklist']}）· "
              f"关闭误报 {work_stats['closed']} 件 · 认领待办 {work_stats['reviewing']} 件 · "
              f"余 {work_stats['pending']} 件仍待审核")
        if work_stats.get("blacklist_rows_rolled_back"):
            print(f"       （其中「拉黑」动作写入的 "
                  f"{work_stats['blacklist_rows_rolled_back']} 条名单已回滚 ——")
            print("        名单是配置，不该由回放作业产生，否则下次回放会走名单短路）")
    return decisions


def _role_activity(db) -> dict:
    """回放末尾模拟**各角色的日常作业**，让审计日志的各类业务都有真实留痕。

    ⚠ 为什么需要：
      只跑「案件处置」的话，`risk_audit_log` 里只有 `case_dispose` 一种 biz_type，
      而审计页的「业务类型」筛选有 7 个选项 —— 点进去 6 个是空的，
      和「案件状态卡全是 0」是同一类问题（页面看起来像功能没做）。

      这里**真的执行**那些操作再写审计（而不是直接插审计行），
      所以每条记录都能对得上真实的变更前后快照：

        · 策略师：改 3 条规则的描述与最后修改人（`rule_change`）
          ⚠ 刻意只改 `description` / `updated_by`，**不动分值、条件与启停** ——
            回放已经跑完，但留下的配置要仍然是可判定的基线，改了分值会让
            文档里的规则表与线上行为对不上。
        · 管理员：把场景阈值做一次**往返调整**（60 → 58 → 60，`system_config`）。
          往返能留下真实的变更前后 diff，且最终值与基线一致。
        · 分析师：调用一次 Agent 工具并留痕（`agent_call`）。
        · 五个演示账号：各登录一次（`auth`）。

    返回各类操作的条数。
    """
    from app.config import DEMO_USERS
    from app.models import RiskRule, RiskRuleScene
    from app.services import step0_auth
    from app.services.step6_audit import write_audit
    from app.utils import now_dt

    stats = {"rule_change": 0, "system_config": 0, "agent_call": 0,
             "auth": 0, "blacklist_change": 0}

    # ---- 策略师：编辑 3 条规则的描述（不影响判定）----
    for rule in (db.query(RiskRule).order_by(RiskRule.rule_id).limit(3).all()):
        before = {"description": rule.description, "updated_by": rule.updated_by}
        rule.description = (rule.description or "") + "（策略师复核通过）"
        rule.updated_by = "strategist"
        rule.update_time = now_dt()
        db.commit()
        write_audit(db, "rule_change", rule.rule_id, "update", "strategist",
                    role="strategist", before=before,
                    after={"description": rule.description, "updated_by": "strategist"},
                    detail={"note": "回放内置的策略复核"})
        stats["rule_change"] += 1

    # ---- 管理员：阈值往返调整（留下真实的 diff，最终值不变）----
    for code in ("all",):
        scene = (db.query(RiskRuleScene)
                 .filter(RiskRuleScene.scene_code == code).first())
        if scene is None:
            continue
        for target in (scene.mark_threshold - 2, scene.mark_threshold):
            before = {"mark_threshold": scene.mark_threshold}
            scene.mark_threshold = target
            db.commit()
            write_audit(db, "system_config", f"scenes:{code}", "update", "admin",
                        role="admin", before=before, after={"mark_threshold": target},
                        detail={"note": "回放内置的阈值调整（已往返还原）"})
            stats["system_config"] += 1

    # ---- 策略师：往名单里加 3 条**合成标识**条目（留 blacklist_change 审计）----
    # ⚠ 必须用合成标识（WL 前缀）：名单是**前置短路** —— 命中黑名单直接 Reject、
    #   连规则都不走。若把数据集在用的实体加进去，下一轮回放时那些事件会被
    #   名单整批短路（这个坑真的踩过：Reject 从 8% 跳到 33%，且命中规则为空）。
    #   合成条目既留下真实的审计与名单数据，又不会命中任何事件。
    try:
        from app.services import step4_case as case_svc

        for i in range(1, 4):
            row, err = case_svc.add_blacklist(
                db, "gray", "user", f"U-WL-DEMO{i:02d}",
                f"回放内置：观察期账号 #{i}", None, "strategist")
            if err is None:
                write_audit(db, "blacklist_change", str(row["blacklist_id"]), "create",
                            "strategist", role="strategist", after=row,
                            detail={"note": "回放内置的名单维护示例"})
                stats["blacklist_change"] += 1
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ 名单维护留痕跳过：{exc}")

    # ---- 分析师：调用一次 Agent 工具并留痕 ----
    try:
        from app.agent import tools as agent_tools

        result = agent_tools.query_dashboard_stats.invoke({})
        write_audit(db, "agent_call", "replay-session", "tool", "analyst",
                    role="analyst",
                    detail={"tools": ["query_dashboard_stats"],
                            "result_summary": str(result)[:200]})
        stats["agent_call"] += 1
    except Exception as exc:  # noqa: BLE001  工具不可用不影响回放
        print(f"  ⚠ Agent 工具留痕跳过：{exc}")

    # ---- 五个演示账号各登录一次（真实校验密码）----
    for demo in DEMO_USERS:
        user, err = step0_auth.step0_authenticate(db, demo["username"], demo["password"])
        if err is None and user is not None:
            step0_auth.step0_on_login_success(db, user)
            write_audit(db, "auth", user.username, "login", user.username,
                        role=user.role, after={"reason": "回放内置的示例登录"})
            stats["auth"] += 1

    return stats


def _work_the_case_queue(db) -> dict:
    """模拟风控审核团队的**日常作业**，把案件推进到各个状态（v3.1 新增）。

    ⚠ 为什么必须补这一步：
      v3.0 的回放只负责「创建案件」，于是库里 230 件案件里 226 件都是「待审核」、
      4 件是处置后归档 —— 工作台顶部的五个状态卡有**三个永远是 0**
      （审核中 / 已处置 / 已关闭），处置结论「放行 / 拉黑」也永远不出现，
      筛选下拉里一半选项点了没数据。

      真实系统里工单是被**持续处理**的：大部分有结论，一部分被认领后正在办，
      一部分误报被关掉，只留一部分在队列里等人领。这里按固定比例把这件事做出来，
      顺带让 `risk_disposal_record`、审计日志（`case_dispose` / 三种结论）
      都有真实的业务痕迹。

    比例（固定，保证可复现）：
      已处置 40%（放行 / 拦截 / 拉黑 三选一）· 已关闭 15% · 审核中 15% · 待审核 30%

    ⚠⚠ **本函数有副作用，必须把它们收回去**（v3.1 踩过的坑）：
      `dispose_case` 的「拉黑」结论会往 `risk_blacklist` 写用户与设备，
      「拦截」会改 `order_info.order_status`。这些是**真实处置动作**，
      但对「回放数据集」来说是副作用：

        · 名单条目会在**下一次回放**时生效 → 大量事件走黑名单短路
          （表现：Reject 从 8% 暴涨到 33%，且 `rule_results` 为空 ——
           一度看起来像「规则误命中」，其实是上一轮回放的名单还留在库里）；
        · 订单状态被改成「已取消」→ `user_cancel_count` 累积 → R-021 命中率漂移。

      所以这里把**名单**回滚（记下起始 `blacklist_id`，事后删掉新增的），
      保证「名单是配置，不由回放作业产生」。
      订单/售后状态的改动无法在此精确还原（原始值只有 `gen_business_data` 知道），
      因此**要完全可复现，请整条流水线重跑**：
        `init_db --recreate` → `gen_business_data` → `gen_mock_events` → `replay_events`
      （`python start.py --reset` 就是这条链路）
    """
    from app.models import RiskBlacklist, RiskCase
    from app.services import step4_case as case_svc
    from app.services.step6_audit import write_audit

    stats = {"disposed": 0, "pass": 0, "reject": 0, "blacklist": 0,
             "closed": 0, "reviewing": 0, "pending": 0,
             "kept_disposed": 0, "disposed_seq": 0}

    max_bl_id = db.query(func.max(RiskBlacklist.blacklist_id)).scalar() or 0

    rows = (db.query(RiskCase)
            .filter(RiskCase.case_status == "待审核")
            .order_by(RiskCase.create_time).all())

    for idx, case in enumerate(rows):
        bucket = idx % 100
        try:
            if bucket < CASE_DISPOSED_RATIO * 100:
                # 三选一：按索引固定轮转，保证三种结论都会出现
                conclusion = ("pass", "reject", "blacklist")[idx % 3]
                # 处置前先把状态推到「审核中」——状态机要求 待审核→审核中→已处置
                case.case_status = "审核中"
                db.flush()
                data, err = case_svc.dispose_case(
                    db, case.case_id, conclusion,
                    f"回放作业：{conclusion}（演示数据集）", "demo-reviewer")
                if err is None:
                    stats["disposed"] += 1
                    stats[conclusion] += 1
                    # ⚠ 审计必须写（R-CF-05 / R-MA-02）。
                    #   接口路径上是 `routers/case.py` 负责写审计，而回放直接调
                    #   服务层，绕过了那一层 —— 结果跑完回放，审计日志页是**空的**，
                    #   「审计不可篡改」这条演示路径（PRD §7 演示步骤 4）没东西可看。
                    #   这里补上与路由一致的审计记录。
                    write_audit(db, "case_dispose", case.case_id, "dispose",
                                "demo-reviewer", role="reviewer",
                                before=data.get("_before") if isinstance(data, dict) else None,
                                after=data.get("_after") if isinstance(data, dict) else None,
                                detail={"disposals": data.get("disposals") if isinstance(data, dict) else None,
                                        "note": "回放内置的审核作业"})
                    # 处置后异步归档：按**已处置子集**的比例归档，其余停在「已处置」。
                    # ⚠ 这里踩过一次：原先用 `idx % 100 < 66` 判断，但已处置的案件
                    #   本身就是 `idx % 100 < 40` 的那批 —— 恒满足 <66，于是**全部**被归档，
                    #   「已处置」依旧为 0。分比例一定要在同一口径内算。
                    stats["disposed_seq"] += 1
                    if stats["disposed_seq"] % 100 < CASE_ARCHIVE_RATIO * 100:
                        case_svc.archive_case(db, case.case_id, "demo-reviewer")
                        write_audit(db, "case_dispose", case.case_id, "archive",
                                    "demo-reviewer", role="reviewer",
                                    after={"case_status": "已归档"})
                    else:
                        stats["kept_disposed"] += 1
                else:
                    stats["pending"] += 1
            elif bucket < (CASE_DISPOSED_RATIO + CASE_CLOSED_RATIO) * 100:
                before_close = {"case_status": case.case_status}
                _, err = case_svc.close_case(db, case.case_id,
                                             "回放作业：确认为误报，关闭",
                                             "demo-reviewer")
                if err is None:
                    stats["closed"] += 1
                    write_audit(db, "case_dispose", case.case_id, "close",
                                "demo-reviewer", role="reviewer",
                                before=before_close, after={"case_status": "已关闭"})
                else:
                    stats["pending"] += 1
            elif bucket < (CASE_DISPOSED_RATIO + CASE_CLOSED_RATIO
                           + CASE_REVIEWING_RATIO) * 100:
                # 认领：待审核 → 审核中，停在办结前
                case.case_status = "审核中"
                case.assignee = "demo-reviewer"
                db.commit()
                stats["reviewing"] += 1
                write_audit(db, "case_dispose", case.case_id, "hold",
                            "demo-reviewer", role="reviewer",
                            after={"case_status": "审核中", "assignee": "demo-reviewer"})
            else:
                stats["pending"] += 1
        except Exception as exc:  # noqa: BLE001  单件失败不影响整体作业
            db.rollback()
            print(f"  ⚠ 案件 {case.case_id} 作业失败：{exc}")
            stats["pending"] += 1

    # 回滚本函数写入的名单条目（见 docstring 的说明）
    removed = (db.query(RiskBlacklist)
               .filter(RiskBlacklist.blacklist_id > max_bl_id)
               .delete(synchronize_session=False))
    db.commit()
    if removed:
        stats["blacklist_rows_rolled_back"] = removed
    return stats


# 二次验证结果的模拟比例（固定，保证可复现）
CHALLENGE_PASS_RATIO = 0.35          # 通过 → 决策升级 Pass，不建案
CHALLENGE_FAIL_RATIO = 0.45          # 失败 → 决策升级 Review，建案
# 余下 20% 不回调，留给 R-RE-15 的超时兜底扫描处理


# 审核作业比例：真实风控团队会持续处理工单，而不是让案件全堆在「待审核」
CASE_DISPOSED_RATIO = 0.40           # 已处置（放行 / 拦截 / 拉黑）
CASE_CLOSED_RATIO = 0.15             # 关闭为误报
CASE_REVIEWING_RATIO = 0.15          # 已认领、正在处理（停在「审核中」）
# 余下 30% 保持「待审核」—— 演示时要能看到"队列里还有活"

# 已处置的案件里，约 2/3 继续异步归档到「已归档」，其余**停在「已处置」**。
# ⚠ 为什么不能全部归档（v3.1）：
#   R-CF-03 规定「处置后**异步**归档」。若回放把每件都立刻归档到底，
#   工作台的五个状态卡里「已处置」永远是 0，筛「已处置」点了没数据 ——
#   与「审核中 / 已关闭」曾经为 0 是同一类问题。
#   停在「已处置」正好对应「异步任务还没跑到」这一刻，是合法中间态。
CASE_ARCHIVE_RATIO = 0.66


def _resolve_one(db, event_id: str, seq: int) -> tuple[str | None, str | None]:
    """对**单条**事件的二次验证走一遍真实回调路径（R-RE-14）。

    按固定比例分配结果（`seq` 决定，可复现）：
      前 35% → 通过（→ 决策升级 Pass，不建案）
      35%~80% → 失败（→ 升级 Review，建案）
      余下 20% → **不回调**，留给 R-RE-15 的超时兜底链路

    返回 `(result, err)`；`result` 为 None 表示本条按「不响应」处理。
    """
    from app.models import RiskChallengeRecord
    from app.services import step3_challenge as challenge_svc
    from app.utils import now_dt

    bucket = seq % 100
    if bucket < CHALLENGE_PASS_RATIO * 100:
        result = "pass"
    elif bucket < (CHALLENGE_PASS_RATIO + CHALLENGE_FAIL_RATIO) * 100:
        result = "fail"
    else:
        return None, None

    row = (db.query(RiskChallengeRecord)
           .filter(RiskChallengeRecord.event_id == event_id).first())
    if row is None or row.status != "pending":
        return None, "3005"

    # ⚠ 必须把 issued_at 视为「回放当刻下发」：
    #   回放的是历史事件，其 issued_at 必然早已超出 120s 窗口，
    #   而 `resolve_challenge` 会用 `is_expired()` 判断 ——
    #   不改的话连传 `pass` 都会被降级成 `timeout`，永远测不到「验证通过」这条分支。
    row.issued_at = now_dt()
    db.flush()
    _data, err = challenge_svc.resolve_challenge(db, event_id, result, "replay")
    if err is None:
        # 与 `routers/rule.py` 的挑战回调**一致地**写审计（R-RE-16 / R-MA-02）。
        # 回放直接调服务层会绕过路由那一层；不补这一步，审计页的
        # 「二次验证」这一类业务就永远是空的（筛选里有这个选项，点进去没数据）。
        from app.services.step6_audit import write_audit
        write_audit(db, "challenge", event_id, "resolve", "demo-reviewer",
                    role="strategist",
                    before={"challenge_status": "pending"},
                    after={"result": result,
                           "decision": (_data or {}).get("decision"),
                           "case_id": (_data or {}).get("case_id")},
                    detail={"note": "回放内置的二次验证回调"})
    return result, err


def _resolve_challenges(db) -> dict:
    """为回放中产生的二次验证**模拟真实回调**（R-RE-14 的两条分支）。

    ⚠ 为什么不能全都留给超时兜底：
      回放的是历史事件，事件发生时的二次验证早已超窗（没人会响应 30 天前的验证码），
      于是所有 Challenge 都会被超时兜底按「超时按失败处理」升级为 Review。
      结果就是**演示数据集里永远看不到「验证通过 → 放行」这条分支**，
      R-RE-14 的两条路径只被覆盖了一半。

      这里按固定比例显式走一遍真实回调路径：
        通过 → 决策升级 Pass，不建案；失败 → 升级 Review 并建案；
        剩下 20% 故意不回调，用来验证超时兜底链路（`main.py` 的后台扫描）。
    """
    from app.models import RiskChallengeRecord

    stats = {"pass": 0, "fail": 0, "ignored": 0}
    rows = (db.query(RiskChallengeRecord)
            .filter(RiskChallengeRecord.status == "pending")
            .order_by(RiskChallengeRecord.issued_at).all())

    for idx, row in enumerate(rows):
        result, err = _resolve_one(db, row.event_id, idx)
        if result is None and err is None:
            stats["ignored"] += 1
        elif err is None:
            stats[result] += 1
    return stats


def replay_http(events: list[dict], url: str) -> Counter:
    """HTTP 回放：调用 POST /api/v1/risk/check（服务标识鉴权）。"""
    reset_risk_artifacts()
    endpoint = url.rstrip("/") + "/api/v1/risk/check"
    decisions: Counter = Counter()
    failed = 0

    for i, raw in enumerate(events, start=1):
        body = json.dumps({k: v for k, v in raw.items() if k != "event_id"}).encode("utf-8")
        request = urllib.request.Request(
            endpoint, data=body,
            headers={"Content-Type": "application/json",
                     "X-Service-Id": settings.SERVICE_ID},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            decisions[data["data"]["decision"]] += 1
        except (urllib.error.URLError, KeyError, ValueError) as exc:
            failed += 1
            if failed == 1:
                print(f"  ⚠ 首次失败：{exc}")
        if i % 200 == 0:
            print(f"  ...已回放 {i}/{len(events)}")

    if failed:
        print(f"  ⚠ 失败 {failed} 条（请确认服务已启动且可访问 {url}）")
    return decisions


def main() -> None:
    parser = argparse.ArgumentParser(description="ERCS 模拟事件回放")
    parser.add_argument("--limit", type=int, default=None, help="回放条数上限")
    parser.add_argument("--http", action="store_true", help="通过 HTTP 调用 /risk/check")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="服务地址（--http 时生效）")
    args = parser.parse_args()

    events = load_events(args.limit)

    # ⚠ 进程内回放必须在**后端停止**时进行（v3.1 踩过的坑）：
    #   后端有一个每 30 秒跑一次的「二次验证超时兜底扫描」（见 main.py 的 lifespan）。
    #   回放会写入大量 `pending` 挑战，其 `issued_at` 是**历史事件时间** ——
    #   在后端眼里全都早就超窗，于是被兜底扫描抢先升级成 `timeout` → Review。
    #   结果：`_resolve_challenges()` 再去回调时挑战已不在 pending，
    #   统计出来是「通过 0 / 失败 0 / 未响应 0」，而**回放不报任何错**。
    if not args.http and _backend_running(args.url):
        print("\n⚠ 检测到后端正在运行。")
        print("  回放已改为「每条 Challenge 决策后**当场**回调」，不再把 pending 挑战攒到最后，")
        print("  因此不会与后端的 30 秒超时兜底扫描抢记录。")
        print("  但两个进程同时写库仍存在极小竞态 —— 追求严格一致请先停后端再回放。")
        print("  继续执行 …\n")

    print(f"\n开始回放 {len(events)} 条事件"
          f"（{'HTTP' if args.http else '进程内'}模式）...\n")

    decisions = (replay_http(events, args.url) if args.http
                 else replay_in_process(events))

    total = sum(decisions.values())
    print("\n" + "=" * 62)
    print("  回放完成 —— 决策分布（决策当刻的五档仲裁结果）")
    print("=" * 62)
    for key in ("Pass", "Mark", "Challenge", "Review", "Reject"):
        n = decisions.get(key, 0)
        ratio = f"{n / total * 100:5.1f}%" if total else "  -  "
        print(f"  {key:<12}{n:>6}  {ratio}")
    print("-" * 62)
    print(f"  {'合计':<12}{total:>6}")
    print("=" * 62)
    if total:
        reject_rate = decisions.get("Reject", 0) / total * 100
        high_rate = (decisions.get("Reject", 0) + decisions.get("Review", 0)) / total * 100
        print(f"  拦截率(Reject) = {reject_rate:.2f}%")
        print(f"  高风险占比(Review+Reject) = {high_rate:.2f}%")

    _print_final_state(int(decisions.get("Challenge", 0)))
    print()


def _print_final_state(challenge_at_decision: int) -> None:
    """打印**当前库内**的决策状态分布，并解释它为何与上面不同。

    ⚠ 为什么必须打印两遍（v3.1）：
      `risk_assessment.decision` 是**当前状态**，不是「决策当刻的结论」。
      按 R-RE-14 / R-RE-15，二次验证通过 → 升级 Pass，失败或**超时** → 升级 Review。
      回放的是历史事件，事件发生时的二次验证早已超窗（没人会去响应一份 30 天前的验证码），
      因此 Challenge 会被超时兜底扫描按「超时按失败处理」升级为 Review。

      这不是缺陷，但**只看一个数字会让人以为 Challenge 那一档从来没被触发过**。
      两行并排展示，才能同时回答「仲裁命中了几档」与「现在各是多少」。
    """
    try:
        from sqlalchemy import func, text

        from app.database import session_scope
        from app.models import RiskAssessment
    except Exception as exc:  # noqa: BLE001  仅打印，失败不影响回放
        print(f"\n  （最终状态统计不可用：{exc}）")
        return

    with session_scope() as db:
        rows = (db.query(RiskAssessment.decision, func.count(RiskAssessment.assessment_id))
                .group_by(RiskAssessment.decision).all())
        now_dist = {d: int(c) for d, c in rows}
        pending = db.execute(text(
            "SELECT COUNT(*) FROM risk_challenge_record WHERE status='pending'")).scalar() or 0

    grand = sum(now_dist.values())
    print("\n" + "-" * 62)
    print("  最终状态分布（risk_assessment.decision 的当前值）")
    print("-" * 62)
    for key in ("Pass", "Mark", "Challenge", "Review", "Reject"):
        n = now_dist.get(key, 0)
        ratio = f"{n / grand * 100:5.1f}%" if grand else "  -  "
        print(f"  {key:<12}{n:>6}  {ratio}")
    print("-" * 62)
    print(f"  {'合计':<12}{grand:>6}")
    if challenge_at_decision:
        print(f"  说明：决策当刻有 {challenge_at_decision} 条落入 Challenge。"
              "二次验证是**瞬态**档位 ——")
        print("        通过 → 升级 Pass（不建案）；失败/超时 → 升级 Review（建案）。")
        print("        因此它的「最终状态」只会剩下尚未到期的那些，回放的历史数据基本都已到期。")
        print("        要在大盘上看到 Challenge，请跑仿真页的边界样本或用刚发生的事件发起验证。")
    if pending:
        print(f"  ⚠ 仍有 {pending} 条 pending 挑战（issued_at 在 120s 窗口内）。")
        print("     后端启动后由超时兜底扫描（每 30s）按「超时按失败处理」升级为 Review。")


if __name__ == "__main__":
    main()
