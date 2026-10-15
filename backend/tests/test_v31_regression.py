"""v3.1 落地修正的回归测试（SPEC §13.3「v3.1 新增回归用例」TC-API-17 ~ TC-API-27）。

这些用例的目的**不是**提高覆盖率，而是让八类**已经真实发生过**的缺陷不再复发。
每条的 docstring 里写了它守的是哪个坑 —— 回归测试最有价值的部分是这段「为什么」。

对应 SPEC 模块 12「第 10 组 · v3.1 落地修正」的 T-61 ~ T-74。
"""

from datetime import timedelta

import pytest
from sqlalchemy import text as sql_text

from app.config import settings
from app.models import (RiskAssessment, RiskBlacklist, RiskCase,
                        RiskChallengeRecord, RiskDisposalRecord, RiskEvent,
                        RiskUser)
from app.utils import now_dt


# ================================================================ 决策链路
def test_risk_check_response_assessment_id_exists_in_db(client, db_session):
    """TC-API-17 / T-61：响应里的 assessment_id 必须能在 risk_assessment 表查到。

    回归的缺陷：`_build_response()` 在非 dry_run 分支又调了一次
    `new_assessment_id()`，于是响应里的评估编号与落库主键**不是同一个值** ——
    客户端拿它反查永远查不到，案件详情与事件详情也无法据此串联。
    """
    resp = client.post("/api/v1/risk/check", json={
        "event_type": "order_create", "source_id": "O-TC17",
        "user_id": "U-TC17", "device_id": "DEV-TC17",
    })
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["assessment_id"] != "simulate", "非仿真路径不应返回占位 ID"

    row = (db_session.query(RiskAssessment)
           .filter(RiskAssessment.assessment_id == data["assessment_id"]).first())
    assert row is not None, "响应 assessment_id 在库中查不到（响应标识 ≠ 落库标识）"
    assert row.event_id == data["event_id"]

    db_session.query(RiskAssessment).filter(
        RiskAssessment.assessment_id == data["assessment_id"]).delete()
    db_session.commit()


# ================================================================ 认证
def test_login_lock_expires_after_window(client, db_session):
    """TC-API-18 / T-62：锁定必须在 LOGIN_LOCK_SECONDS 后自动解除。

    回归的缺陷：`status='locked'` 是永久状态，`LOGIN_LOCK_SECONDS` 从未被读取；
    提示写着「请 10 分钟后重试」，10 分钟后重试仍是 4002。
    演示环境下误输 5 次密码，这个演示账号在整场演示里就废了。
    """
    user = db_session.query(RiskUser).filter(RiskUser.username == "ops").first()
    original = (user.status, user.fail_count, user.locked_at)

    try:
        for _ in range(settings.LOGIN_MAX_FAIL):
            client.post("/api/v1/auth/login", json={"username": "ops", "password": "bad"})

        blocked = client.post("/api/v1/auth/login",
                              json={"username": "ops", "password": "ops@123"})
        assert blocked.status_code == 401
        assert blocked.json()["code"] == "4002"

        # ⚠ MySQL 默认隔离级别是 REPEATABLE READ：db_session 若在本测试开头已读过一次，
        #   它的事务快照就固定在那里，之后 expire_all() 也只是重新 SELECT 同一个快照。
        #   必须 rollback() 结束当前事务，下一次查询才会拿到新快照。
        db_session.rollback()
        user = db_session.query(RiskUser).filter(RiskUser.username == "ops").first()
        assert user.status == "locked"
        assert user.locked_at is not None, "锁定未记录 locked_at，无法判断是否过期"

        # 把锁定时刻回拨到窗口之外 → 应自动解锁
        user.locked_at = now_dt() - timedelta(seconds=settings.LOGIN_LOCK_SECONDS + 60)
        db_session.commit()

        ok_resp = client.post("/api/v1/auth/login",
                              json={"username": "ops", "password": "ops@123"})
        assert ok_resp.status_code == 200, f"锁定未自动解除：{ok_resp.text}"

        db_session.rollback()
        user = db_session.query(RiskUser).filter(RiskUser.username == "ops").first()
        assert user.status == "active"
        assert user.fail_count == 0
        assert user.locked_at is None
    finally:
        # 无论断言在哪一步失败，都必须把 ops 账号还原，否则后续用例的
        # auth_headers 拿不到 ops 的 token（测试间的状态污染）。
        db_session.rollback()
        user = db_session.query(RiskUser).filter(RiskUser.username == "ops").first()
        if user is not None:
            user.status, user.fail_count, user.locked_at = original
            db_session.commit()


def test_never_logged_in_user_has_null_last_login(client, db_session, auth_headers):
    """T-62 附带：从未登录过的账号 `last_login_at` 必须是 null，不能拿当前时间顶替。"""
    user = db_session.query(RiskUser).filter(RiskUser.username == "ops").first()
    original = user.last_login_at
    try:
        user.last_login_at = None
        db_session.commit()
        rows = client.get("/api/v1/auth/users", headers=auth_headers["admin"]).json()["data"]
        ops = next(u for u in rows if u["username"] == "ops")
        assert ops["last_login_at"] is None, "未登录过的账号被填上了假的「上次登录」时间"
    finally:
        db_session.rollback()
        user = db_session.query(RiskUser).filter(RiskUser.username == "ops").first()
        user.last_login_at = original
        db_session.commit()


# ================================================================ 仿真
def test_template_replay_stable_after_long_uptime(client, auth_headers, monkeypatch):
    """TC-API-20 / T-63：模板种子以「回放当刻」为基准，服务运行越久也不漂移。

    回归的缺陷：模板里的种子时间在模块导入时就算成常量，
    运行约 95 分钟后 1h 窗口内的种子全部老化脱落，
    TPL-01 的判定由 Mark 漂移为 Pass，页面报「与预期不一致」。
    """
    from app.engine import step2_feature as fe
    from app.routers import simulate

    headers = auth_headers["strategist"]
    expect = simulate._TEMPLATE_INDEX["TPL-01"]["expect"]

    first = client.post("/api/v1/simulate/run_template/TPL-01", headers=headers)
    assert first.json()["data"]["raw_response"]["decision"] == expect

    # 模拟服务已连续运行 95 分钟。
    # ⚠ 必须把「记录种子」（simulate）与「读窗口」（step2_feature）两处的时钟**一起**后移
    #   —— 它们各自持有 `now_dt` 的导入绑定，只改一处会把种子人为地变成「95 分钟前的旧事件」，
    #   那是测试造出来的假漂移，不是产品缺陷。
    base = fe.now_dt()
    shifted = (lambda: base + timedelta(minutes=95))
    monkeypatch.setattr(simulate, "now_dt", shifted)
    monkeypatch.setattr(fe, "now_dt", shifted)

    later = client.post("/api/v1/simulate/run_template/TPL-01", headers=headers)
    observed = later.json()["data"]["raw_response"]["decision"]
    assert observed == expect, f"运行 95 分钟后模板回放漂移：{expect} → {observed}"


def test_template_seed_offsets_resolved_at_replay_time():
    """T-63 结构性验证：模板里存的是**分钟偏移**，不是导入时算好的绝对时间。

    只要这条成立，服务运行多久都不会让种子滑出滑动窗口（G-11）。
    """
    from app.routers import simulate
    from app.utils import parse_iso

    tpl = simulate._TEMPLATE_INDEX["TPL-01"]
    raw_times = [item["event_time"] for item in tpl["seed"]]
    assert all(isinstance(t, dict) and "offset_min" in t for t in raw_times), \
        "模板种子不得内联绝对时间，必须存「相对回放当刻」的分钟偏移"

    resolved = simulate._resolve_seed(tpl)
    assert all(isinstance(r["event_time"], str) for r in resolved)

    # 现算出的种子时间应落在「当前时刻之前不久」，而不是某个固定的导入时刻
    from app.utils import now_dt
    now = now_dt()
    ages = [(now - parse_iso(r["event_time"])).total_seconds() / 60 for r in resolved]
    # 1h 窗口内必须还有种子（否则 TPL-01 的 device_coupon_cnt_1h 会掉到 1）
    assert sum(1 for a in ages if a < 60) >= 10, f"1h 窗口内种子过少：{sorted(ages)}"


def test_all_templates_match_expectation(client, auth_headers):
    """T-63 附带：4 个内置模板的实测判定都必须与 `expect` 标注一致（G-11）。"""
    from app.routers import simulate

    headers = auth_headers["strategist"]
    mismatched = []
    for tpl in simulate.TEMPLATES:
        resp = client.post(f"/api/v1/simulate/run_template/{tpl['template_code']}",
                           headers=headers)
        info = resp.json()["data"]["template"]
        if not info["matched"]:
            mismatched.append(f"{info['template_code']}: 期望 {info['expect']} 实得 {info['observed']}")
    assert not mismatched, "模板回放与预期不符：\n" + "\n".join(mismatched)


def test_simulate_does_not_persist_events(client, db_session, auth_headers):
    """R-SM-01：仿真执行后 risk_event 记录数不增加。"""
    before = db_session.query(RiskEvent).count()
    client.post("/api/v1/simulate/run", json={
        "event_type": "order_create", "source_id": "O-NOPERSIST",
        "user_id": "U-NOPERSIST", "device_id": "DEV-NOPERSIST",
    }, headers=auth_headers["strategist"])
    db_session.expire_all()
    assert db_session.query(RiskEvent).count() == before


def test_simulate_applies_enrichment_like_production(client, db_session, auth_headers):
    """T-63 附带 / R-SM-02：仿真的参数补全结果必须与线上一致。

    回归的缺陷：`process_event` 只在 `not dry_run` 时把补全出的 `receive_id` 回写请求对象，
    于是仿真的地址维度上下文丢失 —— 「仿真走完整决策链路、与线上一致」的承诺失效。
    """
    resp = client.post("/api/v1/simulate/run", json={
        "event_type": "order_create", "source_id": "O-ENRICH",
        "user_id": "U-ENRICH", "device_id": "DEV-ENRICH",
        "receive_id": "RCV-ENRICH",
    }, headers=auth_headers["strategist"])
    assert resp.status_code == 200
    features = resp.json()["data"]["raw_response"]["features"]
    # 带了 receive_id 时地址团伙窗口才会记录本事件 → 该值为 1.0（本账号自身）
    assert features["addr_account_cnt_7d"] >= 1.0


# ================================================================ 案件流转
def test_challenge_timeout_swept_to_review(client, db_session):
    """TC-API-26 / T-68：超窗口的 pending 挑战由兜底扫描升级为 Review 并建案。

    回归的缺陷：`is_expired()` 只在回调到达时求值。用户既不点验证也不回调时，
    挑战永久停在 pending、事件永久停在 Challenge —— 既不放行也不人审，
    是「悬空事件」，与 PRD §6.1「失败/超时 → Review 人审」矛盾。

    ⚠ 兜底扫描是**周期性后台任务**（`main.py` 的 lifespan），**不再挂在决策请求路径上**：
      挂在请求路径时，任意一个登录事件都会顺手改写历史上所有已超窗口的挑战 ——
      副作用不属于发起它的那个请求，且审计里查不到。这里直接调服务层函数验证逻辑。
    """
    from app.services import step3_challenge as challenge_svc

    now = now_dt()
    event_id = "evt-sweep-1"
    db_session.add(RiskEvent(event_id=event_id, event_type="order_create",
                             source_id="O-SWEEP-1", user_id="U-SWEEP-1",
                             device_id="DEV-SWEEP-1", create_time=now, event_time=now))
    db_session.flush()
    db_session.add(RiskAssessment(
        assessment_id="ast-sweep-1", event_id=event_id, user_id="U-SWEEP-1",
        rule_results=[{"rule_id": "R-002", "rule_category": "订单欺诈", "risk_score": 65}],
        rule_count=1, final_score=65, risk_level="高", decision="Challenge",
        is_veto=0, hit_source="rule", cost_ms=1.0, create_time=now,
    ))
    db_session.add(RiskChallengeRecord(
        challenge_id="chl-sweep-1", event_id=event_id, channel="sms",
        target="138****0000", status="pending",
        issued_at=now - timedelta(seconds=200),      # 已超出 120s 窗口
    ))
    db_session.commit()

    try:
        swept = challenge_svc.sweep_expired(db_session)
        assert swept >= 1, "兜底扫描没有处理这条超时挑战"

        db_session.rollback()
        row = (db_session.query(RiskChallengeRecord)
               .filter(RiskChallengeRecord.challenge_id == "chl-sweep-1").first())
        assert row.status == "timeout", f"超时挑战未被兜底扫描处理：{row.status}"

        ast = (db_session.query(RiskAssessment)
               .filter(RiskAssessment.assessment_id == "ast-sweep-1").first())
        assert ast.decision == "Review"

        case = db_session.query(RiskCase).filter(RiskCase.event_id == event_id).first()
        assert case is not None and case.case_status == "待审核"
    finally:
        db_session.query(RiskCase).filter(RiskCase.event_id == event_id).delete()
        db_session.query(RiskChallengeRecord).filter(
            RiskChallengeRecord.challenge_id == "chl-sweep-1").delete()
        db_session.query(RiskAssessment).filter(
            RiskAssessment.assessment_id == "ast-sweep-1").delete()
        db_session.query(RiskEvent).filter(RiskEvent.event_id == event_id).delete()
        db_session.commit()


def test_block_order_skips_shipped_order(client, db_session, auth_headers):
    """TC-API-22 / T-65 + R-CF-09：已发货订单不被风控回改为「已取消」。

    回归的缺陷：`_exec_action("block_order")` 无条件 `SET order_status='已取消'`。
    货已出门，回改状态会让库存、物流与对账数据自相矛盾。
    """
    now = now_dt()
    event_id, case_id = "evt-disp-1", "cas-disp-1"
    db_session.add(RiskEvent(event_id=event_id, event_type="order_create",
                             source_id="O-DISP-1", user_id="U-DISP-1",
                             device_id="DEV-DISP-1", create_time=now, event_time=now))
    db_session.flush()
    db_session.add(RiskAssessment(
        assessment_id="ast-disp-1", event_id=event_id, user_id="U-DISP-1",
        rule_results=[], rule_count=0, final_score=85, risk_level="极高",
        decision="Reject", is_veto=1, hit_source="rule", cost_ms=1.0, create_time=now,
    ))
    db_session.add(RiskCase(
        case_id=case_id, assessment_id="ast-disp-1", event_id=event_id,
        user_id="U-DISP-1", case_status="审核中", case_category="订单欺诈",
        final_score=85, risk_level="极高", create_time=now,
    ))
    db_session.execute(sql_text(
        "INSERT INTO order_info (order_id, create_time, user_id, order_status) "
        "VALUES ('O-DISP-1', :t, 'U-DISP-1', '已发货')"), {"t": now})
    db_session.commit()

    try:
        resp = client.post(f"/api/v1/cases/{case_id}/dispose",
                           json={"conclusion": "reject", "remark": "测试"},
                           headers=auth_headers["reviewer"])
        assert resp.status_code == 200
        data = resp.json()["data"]

        blocked = next(d for d in data["disposals"] if d["action_type"] == "block_order")
        assert blocked["result"] == "success", "「本条无新动作」不应报 failed"
        assert blocked["detail"]["skipped"] is True
        assert "不可取消" in blocked["detail"]["reason"]

        status = db_session.execute(sql_text(
            "SELECT order_status FROM order_info WHERE order_id='O-DISP-1'")).scalar()
        assert status == "已发货", "已发货订单被错误回改为已取消"
    finally:
        db_session.execute(sql_text("DELETE FROM order_info WHERE order_id='O-DISP-1'"))
        db_session.query(RiskDisposalRecord).filter(
            RiskDisposalRecord.case_id == case_id).delete()
        db_session.query(RiskCase).filter(RiskCase.case_id == case_id).delete()
        db_session.query(RiskAssessment).filter(
            RiskAssessment.assessment_id == "ast-disp-1").delete()
        db_session.query(RiskEvent).filter(RiskEvent.event_id == event_id).delete()
        db_session.commit()


def test_block_order_cancels_unpaid_order(client, db_session, auth_headers):
    """R-CF-09 正向：未发货订单仍应被正常拦截取消（PRD §9.3 FR-30 AC）。"""
    now = now_dt()
    event_id, case_id = "evt-disp-2", "cas-disp-2"
    db_session.add(RiskEvent(event_id=event_id, event_type="order_create",
                             source_id="O-DISP-2", user_id="U-DISP-2",
                             device_id="DEV-DISP-2", create_time=now, event_time=now))
    db_session.flush()
    db_session.add(RiskAssessment(
        assessment_id="ast-disp-2", event_id=event_id, user_id="U-DISP-2",
        rule_results=[], rule_count=0, final_score=85, risk_level="极高",
        decision="Reject", is_veto=1, hit_source="rule", cost_ms=1.0, create_time=now,
    ))
    db_session.add(RiskCase(
        case_id=case_id, assessment_id="ast-disp-2", event_id=event_id,
        user_id="U-DISP-2", case_status="审核中", case_category="订单欺诈",
        final_score=85, risk_level="极高", create_time=now,
    ))
    db_session.execute(sql_text(
        "INSERT INTO order_info (order_id, create_time, user_id, order_status) "
        "VALUES ('O-DISP-2', :t, 'U-DISP-2', '待付款')"), {"t": now})
    db_session.commit()

    try:
        resp = client.post(f"/api/v1/cases/{case_id}/dispose",
                           json={"conclusion": "reject", "remark": "测试"},
                           headers=auth_headers["reviewer"])
        assert resp.status_code == 200

        status = db_session.execute(sql_text(
            "SELECT order_status FROM order_info WHERE order_id='O-DISP-2'")).scalar()
        assert status == "已取消"
    finally:
        db_session.execute(sql_text("DELETE FROM order_info WHERE order_id='O-DISP-2'"))
        db_session.query(RiskDisposalRecord).filter(
            RiskDisposalRecord.case_id == case_id).delete()
        db_session.query(RiskCase).filter(RiskCase.case_id == case_id).delete()
        db_session.query(RiskAssessment).filter(
            RiskAssessment.assessment_id == "ast-disp-2").delete()
        db_session.query(RiskEvent).filter(RiskEvent.event_id == event_id).delete()
        db_session.commit()


def test_archive_case_moves_to_terminal_state(client, db_session, auth_headers):
    """TC-API-21 / T-64 + R-CF-03 / G-12：处置后案件能真正到达「已归档」终态。

    回归的缺陷：状态机声明了 `已处置 → 已归档` 这条边，但**没有任何代码触发它**
    —— 案件永久停在「已处置」，「已归档」统计恒为 0，终态覆盖率永远不达标。
    这里直接调服务层函数验证归档本身（接口侧的异步派发见 selfcheck）。
    """
    from app.services import step4_case as case_svc

    now = now_dt()
    case_id = "cas-arch-1"
    db_session.add(RiskCase(
        case_id=case_id, assessment_id="ast-arch-1", event_id="evt-arch-1",
        user_id="U-ARCH-1", case_status="已处置", case_category="订单欺诈",
        final_score=90, risk_level="极高", create_time=now,
    ))
    db_session.commit()

    try:
        data, err = case_svc.archive_case(db_session, case_id, "reviewer")
        assert err is None and data["case_status"] == "已归档"
        assert data["already"] is False

        db_session.expire_all()
        row = db_session.query(RiskCase).filter(RiskCase.case_id == case_id).first()
        assert row.case_status == "已归档"

        # 幂等：重复归档不报错
        again, err2 = case_svc.archive_case(db_session, case_id, "reviewer")
        assert err2 is None and again["already"] is True
    finally:
        db_session.query(RiskCase).filter(RiskCase.case_id == case_id).delete()
        db_session.commit()


def test_archive_rejects_unprocessed_case(client, db_session):
    """R-CF-03 边界：仍在「待审核 / 审核中」的案件不得被越级归档。"""
    from app.services import step4_case as case_svc

    now = now_dt()
    case_id = "cas-arch-2"
    db_session.add(RiskCase(
        case_id=case_id, assessment_id="ast-arch-2", event_id="evt-arch-2",
        user_id="U-ARCH-2", case_status="待审核", case_category="订单欺诈",
        final_score=90, risk_level="极高", create_time=now,
    ))
    db_session.commit()

    try:
        data, err = case_svc.archive_case(db_session, case_id, "system")
        assert data is None and err == "2003"
    finally:
        db_session.query(RiskCase).filter(RiskCase.case_id == case_id).delete()
        db_session.commit()


def test_manage_blacklist_remove_only_touches_black(client, db_session, auth_headers):
    """T-65 + SPEC §9.5.1：Agent 的名单移除只能删黑名单，不得误删同值的白名单条目。

    回归的缺陷：`manage_blacklist` 的 remove 只按 (entity_type, entity_value) 查，
    把「移除黑名单」做成了「删除白名单」—— 三个名单类型共享这两列，
    唯一约束是 (list_type, entity_type, entity_value) 三元组。
    """
    headers = auth_headers["strategist"]
    assert client.post("/api/v1/blacklist", json={
        "list_type": "white", "entity_type": "user", "entity_value": "U-AGW-1",
        "reason": "白名单测试",
    }, headers=headers).status_code == 200
    assert client.post("/api/v1/blacklist", json={
        "list_type": "black", "entity_type": "user", "entity_value": "U-AGB-1",
        "reason": "黑名单测试",
    }, headers=headers).status_code == 200

    try:
        from app.agent.tools import manage_blacklist

        out = manage_blacklist.invoke({"action": "remove", "entity_type": "user",
                                      "value": "U-AGW-1"})
        assert "未找到" in out, f"黑名单移除误伤白名单条目：{out}"

        db_session.expire_all()
        white = (db_session.query(RiskBlacklist)
                 .filter(RiskBlacklist.list_type == "white")
                 .filter(RiskBlacklist.entity_value == "U-AGW-1").first())
        assert white is not None, "白名单条目被误删"
    finally:
        db_session.query(RiskBlacklist).filter(
            RiskBlacklist.entity_value.in_(["U-AGW-1", "U-AGB-1"])).delete(
            synchronize_session=False)
        db_session.commit()


def test_after_sale_event_with_order_id_in_source_id(client, db_session):
    """T-77：`after_sale_apply` 的 `source_id` 装了**订单号**时，仍应按订单补全上下文。

    回归的缺陷：数据集里 70 条售后事件的 `source_id` 是订单号（`O-000264`）而不是
    售后单号（`PS-xxxxx`）—— 生成器在「该用户没有售后单」时退化为绑定其订单。
    补全逻辑当时只按 `postsale_id` 查，查不到就记 warn 并放弃，后果是：
      · 每回放一次刷一屏「售后单 O-000xxx 未关联到订单」的告警，看起来像系统有病；
      · 这 70 条事件的订单维度 8 个特征与金额全为 0。
    同样的信息本来就在 `order_info` 里，没有理由取不到 —— 按订单号再补一次即可。
    """
    from sqlalchemy import text as sql_text

    now = now_dt()
    order_id = "O-AFTSALE-1"
    db_session.execute(sql_text(
        "INSERT INTO order_info (order_id, create_time, user_id, receive_id, order_status) "
        "VALUES (:o, :t, 'U-1001', 'RCV-0001', '已完成')"), {"o": order_id, "t": now})
    db_session.execute(sql_text(
        "INSERT INTO order_detail (order_detail_id, order_id, sku_id, sku_name, sku_count,"
        " total_amount, discount_amount, final_amount) "
        "VALUES ('OD-AFTSALE-1', :o, 'SKU-0001', '测试商品', 2, 500, 50, 450)"),
        {"o": order_id})
    db_session.commit()

    try:
        resp = client.post("/api/v1/risk/check", json={
            "event_type": "after_sale_apply", "source_id": order_id,
            "user_id": "U-1001", "device_id": "DEV-AFTSALE-1",
            "receive_id": "RCV-0001",
        })
        assert resp.status_code == 200
        data = resp.json()["data"]

        # 订单上下文被补全 → 订单维度特征有真实值
        assert data["features"]["order_total_amount"] == 450.0, \
            f"售后事件未按订单补全：order_total_amount={data['features']['order_total_amount']}"
        # 金额也随补全落到事件上（供资损统计使用）
        assert data["features"]["order_item_count"] == 1.0
    finally:
        from app.models import RiskAssessment, RiskEvent

        db_session.execute(sql_text("DELETE FROM risk_feature WHERE event_id IN "
                                    "(SELECT event_id FROM risk_event WHERE source_id=:o)"),
                           {"o": order_id})
        db_session.query(RiskAssessment).filter(
            RiskAssessment.event_id.in_(
                db_session.query(RiskEvent.event_id).filter(RiskEvent.source_id == order_id))
        ).delete(synchronize_session=False)
        db_session.query(RiskEvent).filter(RiskEvent.source_id == order_id).delete(
            synchronize_session=False)
        db_session.execute(sql_text("DELETE FROM order_detail WHERE order_id=:o"), {"o": order_id})
        db_session.execute(sql_text("DELETE FROM order_info WHERE order_id=:o"), {"o": order_id})
        db_session.commit()


# ================================================================ 监控与审计
def test_dashboard_metrics_share_scope(client, auth_headers):
    """TC-API-24 / T-66 + R-MA-11：指标卡的值与变化量必须同口径。

    回归的缺陷：`assess_total` 是今日值，`high_risk_count` / `pass_rate` /
    `loss_prevented` 却是全量值，同一张卡上的数字互相之间没有关系；
    `pass_rate_delta` 硬编码 0.0、`loss_delta` 写成 `loss × 0.14` 的估算常量。
    """
    resp = client.get("/api/v1/dashboard/overview", headers=auth_headers["admin"])
    assert resp.status_code == 200
    data = resp.json()["data"]

    assert "scope" in data, "缺少口径标注，前端无法区分「今日 / 累计 / 实时」"
    assert data["scope"]["assess_total"] == "today"
    assert data["scope"]["high_risk_count"] == "today"
    assert data["scope"]["loss_prevented"] == "all"
    assert data["scope"]["pending_cases"] == "realtime"
    assert 0.0 <= data["pass_rate"] <= 1.0
    assert set(data["decision_dist"]) == {"Pass", "Mark", "Challenge", "Review", "Reject"}


def test_api_health_uses_real_counters(client, auth_headers):
    """TC-API-25 / T-67 + R-MA-12：接口健康度必须来自真实计数器，不是写死的常量。

    回归的缺陷：该接口返回的是硬编码示例值（calls: 8240 / errors: 12），
    页面看起来正常但**从不变化** —— 运维按它排障会被误导。
    """
    from app import metrics

    headers = auth_headers["admin"]
    metrics.reset()

    fresh = client.get("/api/v1/system/health", headers=headers).json()["data"]
    route = "POST /api/v1/risk/check"
    baseline = next((i["calls"] for i in fresh["api_health"] if i["path"] == route), 0)

    for _ in range(3):
        client.post("/api/v1/risk/check", json={
            "event_type": "login", "source_id": "S-METRIC", "user_id": "U-METRIC",
            "device_id": "DEV-METRIC",
        })

    after = client.get("/api/v1/system/health", headers=headers).json()["data"]
    calls = next(i["calls"] for i in after["api_health"] if i["path"] == route)
    assert calls >= baseline + 3, f"接口计数未随真实调用增长（baseline={baseline}, now={calls}）"
    assert after["queue_supported"] is False, "本架构无队列，应显式声明而非让用户误读为健康"


def test_system_config_requires_ops_or_admin(client, auth_headers):
    """T-67：`/system/config` 暴露运行参数，应收敛到 admin / ops（与 SPEC R-AU-11 一致）。"""
    assert client.get("/api/v1/system/config",
                      headers=auth_headers["analyst"]).status_code == 403
    assert client.get("/api/v1/system/config",
                      headers=auth_headers["reviewer"]).status_code == 403
    assert client.get("/api/v1/system/config",
                      headers=auth_headers["ops"]).status_code == 200

    body = client.get("/api/v1/system/config",
                      headers=auth_headers["admin"]).json()["data"]
    assert "login_lock_seconds" in body, "锁定策略应可自查（否则无法解释 4002 的时限）"


def test_audit_delete_still_blocked_at_db_level(client, db_session):
    """T-69：审计不可篡改由**数据库触发器**兜底（应用层不提供接口只是第一道）。

    手工 `DELETE FROM risk_audit_log` 必须被数据库拒绝 ——
    这正是 v3.0 导出的 `sql/*.sql` 缺失触发器时留下的口子。
    """
    from sqlalchemy.exc import DatabaseError

    now = now_dt()
    db_session.execute(sql_text(
        "INSERT INTO risk_audit_log (log_id, biz_type, biz_id, action, operator, create_time) "
        "VALUES ('log-trig-1', 'auth', 'x', 'login', 'tester', :t)"), {"t": now})
    db_session.commit()
    try:
        with pytest.raises(DatabaseError):
            db_session.execute(sql_text("DELETE FROM risk_audit_log WHERE log_id='log-trig-1'"))
            db_session.commit()
    finally:
        db_session.rollback()


# ================================================================ 权限矩阵（T-71）
def test_rule_and_watchlist_read_open_to_four_roles(client, auth_headers):
    """TC-API-18b / T-71：规则 / 名单 / 特征字典的**读**对 4 个角色放开，ops 不可读。

    回归的缺陷：前端路由只放 strategist / admin，与 PRD §2.1.1
    「reviewer / analyst 只读可见」不符 —— 后端允许读、前端却进不去。
    """
    for role in ("reviewer", "strategist", "analyst", "admin"):
        assert client.get("/api/v1/rules", headers=auth_headers[role]).status_code == 200
        assert client.get("/api/v1/blacklist", headers=auth_headers[role]).status_code == 200
        assert client.get("/api/v1/features/dict",
                          headers=auth_headers[role]).status_code == 200
    assert client.get("/api/v1/rules", headers=auth_headers["ops"]).status_code == 403
    assert client.get("/api/v1/features/dict", headers=auth_headers["ops"]).status_code == 403


def test_rule_write_is_strategist_only(client, auth_headers):
    """T-71 / PRD §2.1.1：规则与名单的**写**仅 strategist，admin 亦不可写。"""
    payload = {"rule_name": "T-71 临时规则", "rule_category": "账户风险",
               "event_type": "通用",
               "rule_condition": {"field": "user_total_orders", "op": ">=", "value": 1},
               "risk_level": "低", "risk_score": 10, "action": "标记"}

    for role in ("reviewer", "analyst", "admin", "ops"):
        resp = client.post("/api/v1/rules", json=payload, headers=auth_headers[role])
        assert resp.status_code == 403, f"{role} 不应能新建规则"

    resp = client.post("/api/v1/rules", json=payload, headers=auth_headers["strategist"])
    assert resp.status_code == 200
    rule_id = resp.json()["data"]["rule_id"]
    assert rule_id.startswith("R-"), "规则编码应由服务端生成"
    client.delete(f"/api/v1/rules/{rule_id}", headers=auth_headers["strategist"])


def test_threshold_update_syncs_all_scenes(client, auth_headers):
    """T-73 / SPEC R-FE2-22：阈值是全局配置，改一次即同步全部 7 条场景。"""
    headers = auth_headers["admin"]
    original = client.get("/api/v1/rules/scenes", headers=headers).json()["data"]

    try:
        resp = client.put("/api/v1/rules/scenes/all",
                          json={"mark_threshold": 50}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["synced_scenes"] == 7

        scenes = client.get("/api/v1/rules/scenes", headers=headers).json()["data"]
        assert {s["mark_threshold"] for s in scenes} == {50}, "存在未同步的场景"

        # 非法关系被拒
        assert client.put("/api/v1/rules/scenes/all",
                          json={"pass_threshold": 90}, headers=headers).status_code == 400
    finally:
        base = next((s for s in original if s["scene_code"] == "all"), original[0])
        client.put("/api/v1/rules/scenes/all", json={
            "pass_threshold": base["pass_threshold"],
            "mark_threshold": base["mark_threshold"],
            "reject_threshold": base["reject_threshold"],
            "multi_rule_bonus": base["multi_rule_bonus"],
            "veto_min_score": base["veto_min_score"],
        }, headers=headers)


def test_admin_reset_works_with_foreign_keys(client, auth_headers):
    """TC-CM-09 / T-70：加外键后 `POST /admin/reset` 的删除顺序必须仍然可执行。

    外键就位后，删表顺序必须是**先子后父**；顺序写反会直接抛
    `Cannot delete or update a parent row`，数据重置功能整体失效。
    """
    headers = auth_headers["admin"]
    client.post("/api/v1/risk/check", json={
        "event_type": "order_create", "source_id": "O-RESET-1",
        "user_id": "U-RESET-1", "device_id": "DEV-RESET-1",
    })
    assert client.get("/api/v1/admin/health").json()["data"]["counts"]["risk_event"] >= 1

    resp = client.post("/api/v1/admin/reset?keep_config=true", headers=headers)
    assert resp.status_code == 200, f"重置失败（外键顺序问题？）：{resp.text}"

    counts = client.get("/api/v1/admin/health").json()["data"]["counts"]
    assert counts["risk_event"] == 0
    assert counts["risk_rule"] == 30, "keep_config=true 时规则应保留"
    assert counts["risk_user"] == 5, "账号永不清空"
