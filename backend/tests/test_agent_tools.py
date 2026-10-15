"""AI Agent 工具测试（SPEC §13.3.6 · TC-AT-01 ~ TC-AT-07）。

⚠ 不依赖真实 LLM：只验证 8 个工具本身可独立调用并返回有效结果，
   以及 LLM 未配置时的降级路径。
"""

import json

import pytest

from app.agent import chat as agent_chat
from app.agent import tools as agent_tools


def _parse(result: str) -> dict:
    return json.loads(result)


# ---------------------------------------------------------------- 工具注册
def test_tool_catalog_eight_tools():
    catalog = agent_tools.tool_catalog()
    assert len(catalog) == 8


def test_tool_groups():
    assert len(agent_tools.RISK_TOOLS) == 4
    assert len(agent_tools.DATA_TOOLS) == 4
    assert len(agent_tools.ALL_TOOLS) == 8


def test_tool_names():
    names = {t.name for t in agent_tools.ALL_TOOLS}
    assert names == {
        "risk_check", "query_cases", "query_user_profile", "manage_blacklist",
        "query_dashboard_stats", "analyze_risk_trend",
        "analyze_rule_effectiveness", "query_business_data",
    }


def test_tools_have_descriptions():
    """描述是 LLM 选择工具的唯一依据，必须非空。"""
    for t in agent_tools.ALL_TOOLS:
        assert t.description and len(t.description.strip()) > 10


# ---------------------------------------------------------------- 风控决策工具
def test_risk_check_tool(client, test_engine):
    """TC-AT-01：risk_check 返回评分/决策。"""
    result = agent_tools.risk_check.invoke(
        {"user_id": "U-TOOL-1", "event_type": "order_create", "source_id": "O-TOOL-1"})
    data = _parse(result)
    assert "final_score" in data and "decision" in data
    assert data["decision"] in ("Pass", "Mark", "Challenge", "Review", "Reject")


def test_query_user_profile_tool(client, test_engine):
    """TC-AT-02：画像工具返回画像或降级特征。"""
    result = agent_tools.query_user_profile.invoke({"user_id": "U-TOOL-NONE"})
    data = _parse(result)
    assert data["user_id"] == "U-TOOL-NONE"
    assert "has_profile" in data


def test_manage_blacklist_tool_add_check_remove(client, test_engine):
    """TC-AT-03：add → check → remove 闭环。"""
    added = _parse(agent_tools.manage_blacklist.invoke(
        {"action": "add", "entity_type": "user", "value": "U-TOOL-BL",
         "reason": "工具测试"}))
    assert added["ok"] is True

    checked = _parse(agent_tools.manage_blacklist.invoke(
        {"action": "check", "entity_type": "user", "value": "U-TOOL-BL"}))
    assert checked["in_blacklist"] is True

    removed = _parse(agent_tools.manage_blacklist.invoke(
        {"action": "remove", "entity_type": "user", "value": "U-TOOL-BL"}))
    assert removed["ok"] is True


def test_manage_blacklist_add_requires_value(client, test_engine):
    result = agent_tools.manage_blacklist.invoke({"action": "add", "entity_type": "user"})
    assert "失败" in result


def test_query_cases_tool(client, test_engine):
    data = _parse(agent_tools.query_cases.invoke({"case_status": "", "page": 1}))
    assert "total" in data and "items" in data


# ---------------------------------------------------------------- 数据分析工具
def test_query_dashboard_stats_tool(client, test_engine):
    """TC-AT-04：仪表盘统计返回评估量与高风险数。"""
    data = _parse(agent_tools.query_dashboard_stats.invoke({}))
    assert "overview" in data
    assert "assess_total" in data["overview"]
    assert "high_risk_count" in data["overview"]


def test_analyze_risk_trend_tool(client, test_engine):
    """TC-AT-05：趋势分析返回每日数据与分布。"""
    data = _parse(agent_tools.analyze_risk_trend.invoke({"days": 7}))
    assert data["days"] == 7
    assert len(data["daily"]) == 7
    assert "decision_distribution" in data


def test_analyze_rule_effectiveness_tool(client, test_engine):
    """TC-AT-06：规则效果分析识别零命中规则。"""
    data = _parse(agent_tools.analyze_rule_effectiveness.invoke({}))
    assert "zero_hit_count" in data
    assert isinstance(data["items"], list)


@pytest.mark.parametrize("query_type,extra", [
    ("user_orders", {"user_id": "U-TOOL-1"}),
    ("user_postsales", {"user_id": "U-TOOL-1"}),
    ("user_addresses", {"user_id": "U-TOOL-1"}),
    ("order_detail", {"order_id": "O-NOT-EXIST"}),
    ("recent_orders", {}),
    ("high_value_orders", {}),
])
def test_query_business_data_六种类型(client, test_engine, query_type, extra):
    payload = {"query_type": query_type, "limit": 5}
    payload.update(extra)
    data = _parse(agent_tools.query_business_data.invoke(payload))
    assert data["query_type"] == query_type
    assert "items" in data


def test_query_business_data_invalid_type(client, test_engine):
    result = agent_tools.query_business_data.invoke({"query_type": "no_such_type"})
    assert "不支持的 query_type" in result


# ---------------------------------------------------------------- 工具容错（R-AG-02）
def test_tool_never_raises(client, test_engine):
    """工具内部异常一律转为友好字符串，不中断 Agent 对话。"""
    for tool, payload in [
        (agent_tools.risk_check, {"user_id": "", "event_type": "order_create", "source_id": ""}),
        (agent_tools.query_user_profile, {"user_id": ""}),
        (agent_tools.query_cases, {"case_status": "不存在的状态", "page": 1}),
    ]:
        result = tool.invoke(payload)
        assert isinstance(result, str) and len(result) > 0


# ---------------------------------------------------------------- 降级路径（R-AG-01）
def test_degraded_when_llm_not_configured(client, test_engine, monkeypatch):
    """TC-AT-07：LLM 未配置时返回降级提示而非抛异常。"""
    from app.config import settings

    monkeypatch.setattr(type(settings), "llm_configured",
                        property(lambda self: False), raising=False)
    agent_chat.reset_agent()

    result = agent_chat.chat(client_db(), "你好", None, None)
    assert result["degraded"] is True
    assert result["degrade_reason"] == "LLM_API_KEY_MISSING"
    assert "LLM_API_KEY" in result["reply"]


def client_db():
    """取一个测试库会话（供直接调用 agent_chat 使用）。"""
    from app.database import SessionLocal
    return SessionLocal()


def test_session_persisted_and_listable(client, auth_headers, test_engine):
    """R-AG-04：会话与消息落库，服务重启不丢。"""
    resp = client.post("/api/v1/agent/chat", headers=auth_headers["analyst"],
                       json={"message": "你好", "session_id": None})
    assert resp.status_code == 200
    sid = resp.json()["data"]["session_id"]
    assert sid

    sessions = client.get("/api/v1/agent/sessions",
                          headers=auth_headers["analyst"]).json()["data"]
    assert any(s["session_id"] == sid for s in sessions)


def test_agent_clear_session(client, auth_headers):
    resp = client.post("/api/v1/agent/chat", headers=auth_headers["analyst"],
                       json={"message": "清理测试", "session_id": None})
    sid = resp.json()["data"]["session_id"]

    cleared = client.post("/api/v1/agent/clear", headers=auth_headers["analyst"],
                          json={"session_id": sid})
    assert cleared.status_code == 200
    assert cleared.json()["data"]["cleared"] is True


def test_agent_status_endpoint(client, auth_headers):
    resp = client.get("/api/v1/agent/status", headers=auth_headers["analyst"])
    data = resp.json()["data"]
    assert "llm_configured" in data
    assert data["tool_count"] == 8
