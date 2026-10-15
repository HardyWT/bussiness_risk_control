"""API 路由集成测试（SPEC §13.3.4 · TC-API-01 ~ TC-API-16）。

覆盖认证、权限矩阵、规则 CRUD、事件网关、案件、大盘、仿真、审计不可篡改。
"""

from contextlib import contextmanager

import pytest


# ================================================================ 认证（模块 11）
def test_login_ok(client):
    resp = client.post("/api/v1/auth/login",
                       json={"username": "reviewer", "password": "reviewer@123"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["user"]["role"] == "reviewer"
    assert data["access_token"]
    assert data["expires_in"] == 28800


def test_login_wrong_password(client):
    resp = client.post("/api/v1/auth/login",
                       json={"username": "reviewer", "password": "wrong"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "4001"


def test_login_does_not_leak_password_hash(client, auth_headers):
    resp = client.get("/api/v1/auth/me", headers=auth_headers["admin"])
    assert resp.status_code == 200
    assert "password_hash" not in resp.json()["data"]


def test_unauthenticated_returns_401(client):
    resp = client.get("/api/v1/cases")
    assert resp.status_code == 401
    assert resp.json()["code"] == "4001"


def test_bad_token_returns_401(client):
    resp = client.get("/api/v1/cases", headers={"Authorization": "Bearer not-a-token"})
    assert resp.status_code == 401


# ================================================================ 权限矩阵（R-AU-11）
def test_reviewer_cannot_write_rules(client, auth_headers):
    resp = client.post("/api/v1/rules", headers=auth_headers["reviewer"], json={
        "rule_name": "x", "rule_category": "账户风险", "event_type": "通用",
        "rule_condition": {"field": "amount", "op": ">", "value": 1},
        "risk_level": "中", "risk_score": 10, "action": "标记"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "4003"


def test_analyst_cannot_write_rules(client, auth_headers):
    resp = client.post("/api/v1/rules", headers=auth_headers["analyst"], json={
        "rule_name": "x", "rule_category": "账户风险", "event_type": "通用",
        "rule_condition": {"field": "amount", "op": ">", "value": 1},
        "risk_level": "中", "risk_score": 10, "action": "标记"})
    assert resp.status_code == 403


def test_admin_rules_read_only(client, auth_headers):
    """PRD §2.1.1：admin 对规则为 👁 只读，写操作返回 403。"""
    resp = client.post("/api/v1/rules", headers=auth_headers["admin"], json={
        "rule_name": "x", "rule_category": "账户风险", "event_type": "通用",
        "rule_condition": {"field": "amount", "op": ">", "value": 1},
        "risk_level": "中", "risk_score": 10, "action": "标记"})
    assert resp.status_code == 403


def test_strategist_cannot_access_cases(client, auth_headers):
    assert client.get("/api/v1/cases", headers=auth_headers["strategist"]).status_code == 403


def test_analyst_cannot_access_audit(client, auth_headers):
    assert client.get("/api/v1/audit/logs", headers=auth_headers["analyst"]).status_code == 403


def test_reviewer_cannot_access_health(client, auth_headers):
    assert client.get("/api/v1/system/health", headers=auth_headers["reviewer"]).status_code == 403


def test_ops_can_access_health(client, auth_headers):
    assert client.get("/api/v1/system/health", headers=auth_headers["ops"]).status_code == 200


def test_only_admin_can_change_thresholds(client, auth_headers):
    body = {"pass_threshold": 35}
    assert client.put("/api/v1/rules/scenes/order_create",
                      headers=auth_headers["strategist"], json=body).status_code == 403
    assert client.put("/api/v1/rules/scenes/order_create",
                      headers=auth_headers["admin"], json=body).status_code == 200


def test_analyst_cannot_use_agent(client, auth_headers):
    """analyst 可以；reviewer 不可以。"""
    assert client.get("/api/v1/agent/tools", headers=auth_headers["analyst"]).status_code == 200
    assert client.post("/api/v1/agent/chat", headers=auth_headers["reviewer"],
                       json={"message": "hi"}).status_code == 403


# ---------------------------------------------------------------- AI 会话管理
class _FakeAgent:
    """记录被传入模型的 messages，用于断言上下文拼装是否正确。"""

    def __init__(self):
        self.seen = []

    def invoke(self, payload):
        from langchain_core.messages import AIMessage
        self.seen = list(payload["messages"])
        return {"messages": self.seen + [AIMessage(content="ok")]}


@pytest.fixture
def degraded_agent(monkeypatch):
    """强制走降级路径，让会话读写测试不依赖真实 LLM 可用性。

    ⚠ 降级路径同样会创建会话并持久化 human/ai 消息，足以覆盖会话 CRUD。
    """
    from app.agent import chat as agent_chat

    def _raise(*_args, **_kwargs):
        raise agent_chat.DegradedError("LLM_API_KEY_MISSING")

    monkeypatch.setattr(agent_chat, "get_agent", _raise)


@pytest.fixture
def fake_agent(monkeypatch):
    """用假 Agent 替换真实 LLM，验证上下文拼装（不产生任何网络调用）。"""
    from app.agent import chat as agent_chat

    fake = _FakeAgent()
    monkeypatch.setattr(agent_chat, "get_agent", lambda: fake)
    return fake


def _new_session(client, headers, text="查看今天的风控统计数据") -> str:
    resp = client.post("/api/v1/agent/chat", headers=headers, json={"message": text})
    assert resp.status_code == 200
    return resp.json()["data"]["session_id"]


def test_agent_chat_creates_session_and_can_read_messages(client, auth_headers, degraded_agent):
    """发消息 → 会话进入列表 → 历史消息可读回（human + ai 两条）。"""
    headers = auth_headers["analyst"]
    sid = _new_session(client, headers)

    sessions = client.get("/api/v1/agent/sessions", headers=headers).json()["data"]
    assert any(s["session_id"] == sid for s in sessions)

    resp = client.get(f"/api/v1/agent/sessions/{sid}/messages", headers=headers)
    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert [r["role"] for r in rows] == ["me", "ai"]
    assert rows[0]["content"] == "查看今天的风控统计数据"


def test_agent_first_turn_question_not_duplicated(client, auth_headers, fake_agent):
    """回归：首轮提问只应传一次给模型。

    修复前 chat() 先落库再 _load_history，读回本轮提问后又 append 一次，
    模型会连续收到两条一模一样的用户消息。
    """
    headers = auth_headers["analyst"]
    assert client.post("/api/v1/agent/chat", headers=headers,
                       json={"message": "唯一提问"}).status_code == 200

    humans = [m for m in fake_agent.seen if getattr(m, "type", "") == "human"]
    assert len(humans) == 1
    assert humans[0].content == "唯一提问"


def test_agent_second_turn_keeps_history_order(client, auth_headers, fake_agent):
    """第二轮上下文应包含「首轮提问 → 首轮回答 → 本轮提问」，且顺序正确。"""
    headers = auth_headers["analyst"]
    sid = _new_session(client, headers, "第一问")
    client.post("/api/v1/agent/chat", headers=headers,
                json={"message": "第二问", "session_id": sid})

    kinds = [getattr(m, "type", "") for m in fake_agent.seen]
    assert kinds == ["human", "ai", "human"]
    assert [m.content for m in fake_agent.seen] == ["第一问", "ok", "第二问"]


def test_agent_session_messages_isolated_by_user(client, auth_headers, degraded_agent):
    """非 admin 读取他人会话 → 403。"""
    sid = _new_session(client, auth_headers["analyst"])
    resp = client.get(f"/api/v1/agent/sessions/{sid}/messages",
                      headers=auth_headers["strategist"])
    assert resp.status_code == 403
    assert resp.json()["code"] == "4003"


def test_agent_clear_keeps_session_row(client, auth_headers, degraded_agent):
    """清除会话仅清空消息，会话条目保留且消息数归零。"""
    headers = auth_headers["analyst"]
    sid = _new_session(client, headers)

    assert client.post("/api/v1/agent/clear", headers=headers,
                       json={"session_id": sid}).status_code == 200
    assert client.get(f"/api/v1/agent/sessions/{sid}/messages",
                      headers=headers).json()["data"] == []

    sessions = client.get("/api/v1/agent/sessions", headers=headers).json()["data"]
    row = next(s for s in sessions if s["session_id"] == sid)
    assert row["message_count"] == 0


def test_agent_delete_session_removes_row_and_messages(client, auth_headers, degraded_agent):
    """删除会话 → 会话条目与全部消息一并消失。"""
    headers = auth_headers["analyst"]
    sid = _new_session(client, headers)

    resp = client.delete(f"/api/v1/agent/sessions/{sid}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted_sessions"] == 1
    assert resp.json()["data"]["deleted_messages"] >= 1

    sessions = client.get("/api/v1/agent/sessions", headers=headers).json()["data"]
    assert not any(s["session_id"] == sid for s in sessions)
    assert client.get(f"/api/v1/agent/sessions/{sid}/messages",
                      headers=headers).status_code == 404


def test_agent_delete_others_session_forbidden(client, auth_headers, degraded_agent):
    """非 admin 删除他人会话 → 403。"""
    sid = _new_session(client, auth_headers["analyst"])
    assert client.delete(f"/api/v1/agent/sessions/{sid}",
                         headers=auth_headers["strategist"]).status_code == 403


def test_agent_session_not_found(client, auth_headers):
    """读取不存在的会话 → 404 + code 4004。"""
    resp = client.get("/api/v1/agent/sessions/sess_not_exists/messages",
                      headers=auth_headers["admin"])
    assert resp.status_code == 404
    assert resp.json()["code"] == "4004"


# ================================================================ 规则 CRUD（模块 3）
def test_list_rules(client, auth_headers):
    resp = client.get("/api/v1/rules", headers=auth_headers["strategist"])
    assert resp.status_code == 200
    rules = resp.json()["data"]
    assert len(rules) >= 1
    assert {"rule_id", "rule_name", "rule_condition", "risk_score",
            "challengeable", "priority"}.issubset(set(rules[0].keys()))


def test_get_rule_detail(client, auth_headers):
    assert client.get("/api/v1/rules/R-001",
                      headers=auth_headers["strategist"]).status_code == 200


def test_get_missing_rule_404(client, auth_headers):
    assert client.get("/api/v1/rules/NOTEXIST",
                      headers=auth_headers["strategist"]).status_code == 404


def test_create_and_delete_rule(client, auth_headers):
    resp = client.post("/api/v1/rules", headers=auth_headers["strategist"], json={
        "rule_name": "测试规则", "rule_category": "账户风险", "event_type": "通用",
        "rule_condition": {"field": "user_total_orders", "op": ">=", "value": 999},
        "risk_level": "中", "risk_score": 45, "action": "标记", "priority": 10})
    assert resp.status_code == 200, resp.text
    rule_id = resp.json()["data"]["rule_id"]
    assert rule_id

    toggled = client.put(f"/api/v1/rules/{rule_id}/toggle", headers=auth_headers["strategist"])
    assert toggled.status_code == 200

    assert client.delete(f"/api/v1/rules/{rule_id}",
                         headers=auth_headers["strategist"]).status_code == 200


def test_invalid_condition_returns_3002(client, auth_headers):
    resp = client.post("/api/v1/rules", headers=auth_headers["strategist"], json={
        "rule_name": "坏规则", "rule_category": "账户风险", "event_type": "通用",
        "rule_condition": {"field": "amount", "op": "magic", "value": 1},
        "risk_level": "中", "risk_score": 10, "action": "标记"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "3002"


def test_threshold_validation(client, auth_headers):
    """pass < mark < reject 且 veto >= reject。"""
    bad = client.put("/api/v1/rules/scenes/all", headers=auth_headers["admin"],
                     json={"pass_threshold": 90, "reject_threshold": 80})
    assert bad.status_code == 400 and bad.json()["code"] == "3003"


# ================================================================ 事件网关（模块 1）
def test_risk_check_ok(client):
    resp = client.post("/api/v1/risk/check", json={
        "event_type": "order_create", "source_id": "O-T-001", "user_id": "U-T-1",
        "device_id": "DEV-T-1", "amount": 500})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["decision"] in ("Pass", "Mark", "Challenge", "Review", "Reject")
    assert len(data["features"]) == 33
    assert data["event_type"] == "order_create"


def test_risk_check_missing_field_400(client):
    resp = client.post("/api/v1/risk/check",
                       json={"event_type": "order_create", "source_id": "X", "amount": 1})
    assert resp.status_code == 400
    assert "user_id" in resp.json()["message"]


def test_risk_check_invalid_event_type(client):
    resp = client.post("/api/v1/risk/check",
                       json={"event_type": "unknown", "source_id": "X", "user_id": "U-1"})
    assert resp.status_code == 400


def test_risk_check_invalid_ip(client):
    resp = client.post("/api/v1/risk/check", json={
        "event_type": "order_create", "source_id": "X", "user_id": "U-1",
        "device_id": "D-1", "ip": "999.1.1.1", "amount": 1})
    assert resp.status_code == 400


def test_risk_check_six_event_types(client):
    """六类事件各一条均被正确解析（FR-02）。"""
    samples = [
        ("login", {"device_id": "DEV-T-2"}),
        ("coupon_receive", {"device_id": "DEV-T-3", "source_id": "CPN-T-1"}),
        ("order_create", {"device_id": "DEV-T-4", "source_id": "O-T-2", "amount": 100}),
        ("order_pay", {"device_id": "DEV-T-5", "source_id": "O-T-3", "amount": 100}),
        ("after_sale_apply", {"source_id": "PS-T-1", "amount": 100}),
        ("logistics_complaint", {"source_id": "1"}),
    ]
    for i, (et, extra) in enumerate(samples):
        body = {"event_type": et, "user_id": f"U-SIX-{i}", "ip": f"10.88.0.{i}"}
        body.update(extra)
        body.setdefault("source_id", f"SIX-{i}")
        resp = client.post("/api/v1/risk/check", json=body)
        assert resp.status_code == 200, f"{et}: {resp.text}"
        assert resp.json()["code"] == 0, f"{et}: {resp.text}"


def test_service_id_validation(client):
    """R-AU-06：错误的 X-Service-Id 返回 401。"""
    resp = client.post("/api/v1/risk/check",
                       headers={"X-Service-Id": "wrong-service"},
                       json={"event_type": "login", "source_id": "X",
                             "user_id": "U-1", "device_id": "D-1"})
    assert resp.status_code == 401


# ================================================================ 特征字典（模块 2）
def test_features_dict(client, auth_headers):
    resp = client.get("/api/v1/features/dict", headers=auth_headers["reviewer"])
    assert resp.status_code == 200
    assert len(resp.json()["data"]) == 33


def test_features_dict_by_dimension(client, auth_headers):
    resp = client.get("/api/v1/features/dict?dimension=团伙", headers=auth_headers["reviewer"])
    assert len(resp.json()["data"]) == 8


def test_features_dict_requires_auth(client):
    assert client.get("/api/v1/features/dict").status_code == 401


# ================================================================ 大盘（模块 6）
def test_dashboard_overview(client, auth_headers):
    resp = client.get("/api/v1/dashboard/overview", headers=auth_headers["reviewer"])
    assert resp.status_code == 200
    data = resp.json()["data"]
    for key in ("assess_total", "high_risk_count", "pending_cases",
                "pass_rate", "loss_prevented", "decision_dist"):
        assert key in data
    assert set(data["decision_dist"].keys()) == {"Pass", "Mark", "Challenge", "Review", "Reject"}


def test_dashboard_risk_dist_four_levels(client, auth_headers):
    resp = client.get("/api/v1/dashboard/risk_dist", headers=auth_headers["reviewer"])
    assert len(resp.json()["data"]["items"]) == 4


def test_dashboard_trend_pads_missing_days(client, auth_headers):
    resp = client.get("/api/v1/dashboard/trend?days=7", headers=auth_headers["reviewer"])
    points = resp.json()["data"]["points"]
    assert len(points) == 7
    dates = [p["date"] for p in points]
    assert dates == sorted(dates)


def test_dashboard_rule_effectiveness(client, auth_headers):
    resp = client.get("/api/v1/dashboard/rule_effectiveness", headers=auth_headers["reviewer"])
    data = resp.json()["data"]
    assert "zero_hit_count" in data and isinstance(data["items"], list)


# ================================================================ 趋势分析页（模块 6 · FR-42/43/47 回归）
def _trend_points(client, headers, query):
    resp = client.get(f"/api/v1/dashboard/trend?{query}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["points"]


def _trend_total(client, headers, query):
    return sum(p["assess_cnt"] for p in _trend_points(client, headers, query))


def _rule_hit(client, headers, rule_id, query=""):
    resp = client.get(f"/api/v1/dashboard/rule_effectiveness?{query}", headers=headers)
    assert resp.status_code == 200, resp.text
    return next(i["hit_count"] for i in resp.json()["data"]["items"] if i["rule_id"] == rule_id)


@contextmanager
def _seed_trend_rows(db_session, rows, days_ago=0):
    """临时灌入「事件 + 评估」并保证清理。

    rows: [(event_type, risk_level, decision), …]；每条评估固定命中 R-009。
    days_ago 用于把 create_time 回拨，以便验证统计窗口。
    """
    from datetime import timedelta

    from app.models import RiskAssessment, RiskEvent
    from app.utils import now_dt

    stamp = now_dt() - timedelta(days=days_ago)
    ids = []
    for idx, (etype, level, decision) in enumerate(rows):
        eid, aid = f"evt-tr-{days_ago}-{idx}", f"ast-tr-{days_ago}-{idx}"
        ids.append((eid, aid))
        db_session.add(RiskEvent(
            event_id=eid, event_type=etype, source_id=f"src-{eid}", user_id="U-TR",
            amount=100, create_time=stamp,
        ))
        db_session.add(RiskAssessment(
            assessment_id=aid, event_id=eid, user_id="U-TR",
            rule_results=[{"rule_id": "R-009", "rule_category": "账户风险", "risk_score": 40}],
            rule_count=1, final_score=40, risk_level=level, decision=decision,
            is_veto=0, hit_source="rule", cost_ms=1.0, create_time=stamp,
        ))
    db_session.commit()
    try:
        yield ids
    finally:
        for eid, aid in ids:
            db_session.query(RiskAssessment) \
                .filter(RiskAssessment.assessment_id == aid).delete()
            db_session.query(RiskEvent).filter(RiskEvent.event_id == eid).delete()
        db_session.commit()


def test_trend_points_include_level_counts(client, auth_headers, db_session):
    """FR-43：趋势点位须带四档等级当日计数，且四档之和恒等于评估量。

    回归点：此前后端不返回等级字段，前端「风险等级分布趋势」只能退化为全量常数直线。
    """
    from app.utils import now_dt

    keys = ("low_cnt", "medium_cnt", "high_cnt", "critical_cnt")
    with _seed_trend_rows(db_session, [
        ("order_create", "低", "Pass"),
        ("order_create", "高", "Review"),
        ("login", "极高", "Reject"),
    ]):
        points = _trend_points(client, auth_headers["reviewer"], "days=2")
        assert all(k in points[0] for k in keys)
        assert all(sum(p[k] for k in keys) == p["assess_cnt"] for p in points)
        today = next(p for p in points if p["date"] == now_dt().strftime("%Y-%m-%d"))
        assert today["assess_cnt"] >= 3
        assert today["low_cnt"] >= 1 and today["high_cnt"] >= 1 and today["critical_cnt"] >= 1


def test_trend_event_type_filter(client, auth_headers, db_session):
    """事件类型筛选：过滤后只统计该类型（回归：此前参数被后端完全忽略）。"""
    h = auth_headers["reviewer"]
    base_all = _trend_total(client, h, "days=2")
    base_oc = _trend_total(client, h, "days=2&event_type=order_create")
    base_login = _trend_total(client, h, "days=2&event_type=login")
    with _seed_trend_rows(db_session, [
        ("order_create", "低", "Pass"),
        ("order_create", "高", "Review"),
        ("login", "极高", "Reject"),
    ]):
        assert _trend_total(client, h, "days=2") == base_all + 3
        assert _trend_total(client, h, "days=2&event_type=order_create") == base_oc + 2
        assert _trend_total(client, h, "days=2&event_type=login") == base_login + 1


def test_rule_effectiveness_respects_days_window(client, auth_headers, db_session):
    """FR-47：近 N 天窗口只统计窗口内评估（回归：此前 days 参数被忽略，标签是假的）。"""
    h = auth_headers["reviewer"]
    base_all = _rule_hit(client, h, "R-009")
    base_win = _rule_hit(client, h, "R-009", "days=2")
    with _seed_trend_rows(db_session, [("order_create", "中", "Mark")], days_ago=10):
        assert _rule_hit(client, h, "R-009") == base_all + 1      # 全量含窗口外那条
        assert _rule_hit(client, h, "R-009", "days=2") == base_win  # 窗口内不计


def test_dashboard_top_rules_is_paginated(client, auth_headers):
    resp = client.get("/api/v1/dashboard/top_rules?top=5", headers=auth_headers["reviewer"])
    assert "items" in resp.json()["data"]


# ================================================================ 仿真（模块 7）
def test_simulate_templates(client, auth_headers):
    resp = client.get("/api/v1/simulate/templates", headers=auth_headers["strategist"])
    assert len(resp.json()["data"]) == 4


@pytest.mark.parametrize("code,expect", [
    ("TPL-01", "Mark"), ("TPL-02", "Review"), ("TPL-03", "Reject"), ("TPL-04", "Pass"),
])
def test_simulate_templates_expected_decisions(client, auth_headers, code, expect):
    resp = client.post(f"/api/v1/simulate/run_template/{code}",
                       headers=auth_headers["strategist"])
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert len(data["steps"]) == 4
    assert all(s["status"] == "ok" for s in data["steps"])
    assert len(data["steps"][1]["detail"]["dimensions"]) == 4
    assert data["raw_response"]["decision"] == expect


def test_simulate_does_not_persist(client, auth_headers):
    """R-SM-01：仿真不写 risk_event 表。"""
    before = client.get("/api/v1/events?page_size=1",
                        headers=auth_headers["admin"]).json()["data"]["total"]
    client.post("/api/v1/simulate/run", headers=auth_headers["strategist"], json={
        "event_type": "order_create", "source_id": "SIM-NP-1",
        "user_id": "U-SIM-NP", "device_id": "DEV-SIM-NP", "amount": 100})
    after = client.get("/api/v1/events?page_size=1",
                       headers=auth_headers["admin"]).json()["data"]["total"]
    assert before == after


def test_simulate_invalid_param_skips_steps(client, auth_headers):
    """R-SM-06：校验失败时后续步骤标 skipped。"""
    resp = client.post("/api/v1/simulate/run", headers=auth_headers["strategist"],
                       json={"event_type": "order_create", "source_id": "X"})
    steps = resp.json()["data"]["steps"]
    assert steps[0]["status"] == "failed"
    assert all(s["status"] == "skipped" for s in steps[1:])


# ================================================================ 审计（模块 6）
def test_audit_delete_not_available(client, auth_headers):
    """FR-53 / AC：审计删除接口**故意不存在**。"""
    resp = client.delete("/api/v1/audit/logX", headers=auth_headers["admin"])
    assert resp.status_code in (404, 405)


def test_audit_scope_admin_all_others_self(client, auth_headers):
    admin = client.get("/api/v1/audit/logs", headers=auth_headers["admin"]).json()["data"]
    reviewer = client.get("/api/v1/audit/logs", headers=auth_headers["reviewer"]).json()["data"]
    assert admin["scope"] == "all"
    assert reviewer["scope"] == "self"
    assert all(x["operator"] == "reviewer" for x in reviewer["items"])


def test_audit_records_login_events(client, auth_headers):
    """R-AU-09：登录事件写审计。"""
    logs = client.get("/api/v1/audit/logs?biz_type=auth",
                      headers=auth_headers["admin"]).json()["data"]["items"]
    assert any(x["action"] == "login" for x in logs)


# ================================================================ 系统（模块 6）
def test_system_health_fields(client, auth_headers):
    resp = client.get("/api/v1/system/health", headers=auth_headers["admin"])
    data = resp.json()["data"]
    for key in ("event_qps", "api_error_rate", "api_avg_latency_ms",
                "pool_used", "pool_size", "queue_backlog",
                "business_table_count", "risk_table_count"):
        assert key in data


def test_system_stats_counts(client, auth_headers):
    resp = client.get("/api/v1/system/stats", headers=auth_headers["admin"])
    data = resp.json()["data"]
    assert data["business_table_count"] == 17
    assert len(data["business_tables"]) == 17


# ================================================================ 运维（admin）
def test_admin_health_is_public(client):
    """存活探针不鉴权。"""
    resp = client.get("/api/v1/admin/health")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "ok"


def test_admin_reset_requires_admin(client, auth_headers):
    assert client.post("/api/v1/admin/reset",
                       headers=auth_headers["reviewer"]).status_code == 403


# ================================================================ 案件筛选（英文枚举回归）
def test_cases_accept_english_filters_over_http(client, db_session, auth_headers):
    """回归：前端下拉框经 HTTP 发英文枚举（pending/critical），路由→服务层必须命中。"""
    from app.models import RiskAssessment, RiskCase
    from app.utils import now_dt

    now = now_dt()
    db_session.add(RiskAssessment(
        assessment_id="ast-http-1", event_id="evt-http-1", user_id="U-HTTP",
        rule_results=[{"rule_id": "R-001", "rule_category": "账户风险", "risk_score": 88}],
        rule_count=1, final_score=88, risk_level="极高", decision="Review",
        is_veto=0, hit_source="rule", cost_ms=0.5, create_time=now,
    ))
    db_session.add(RiskCase(
        case_id="cas-http-1", assessment_id="ast-http-1", event_id="evt-http-1",
        user_id="U-HTTP", case_status="待审核", case_category="账户风险",
        final_score=88, risk_level="极高", create_time=now,
    ))
    db_session.commit()
    try:
        resp = client.get("/api/v1/cases",
                          params={"case_status": "pending", "risk_level": "critical",
                                  "page_size": 100},
                          headers=auth_headers["reviewer"])
        assert resp.status_code == 200
        body = resp.json()["data"]
        row = next((c for c in body["items"] if c["case_id"] == "cas-http-1"), None)
        assert row is not None
        assert row["top_rule_id"] == "R-001"
        assert body["statistics"]["total"] >= 1        # 全量总数已下发
        assert isinstance(body["by_category"], dict)   # 分类分布已下发
    finally:
        db_session.query(RiskCase).filter(RiskCase.case_id == "cas-http-1").delete()
        db_session.query(RiskAssessment) \
            .filter(RiskAssessment.assessment_id == "ast-http-1").delete()
        db_session.commit()
