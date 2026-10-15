"""模块 9 路由：AI 智能助手。

对应 SPEC §9.4 API-9.1 ~ API-9.4。权限：strategist / analyst / admin（R-AU-11）。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..agent import chat as agent_chat
from ..agent import tools as agent_tools
from ..config import settings
from ..database import get_db
from ..deps import AGENT_ROLES, ok
from ..models import RiskUser
from ..schemas import AgentChatRequest, AgentClearRequest
from ..services import step6_audit as audit_svc

router = APIRouter(prefix="/api/v1/agent", tags=["AI 智能助手"])


@router.post("/chat")
def chat(body: AgentChatRequest, user: RiskUser = Depends(AGENT_ROLES),
         db: Session = Depends(get_db)):
    """API-9.1 Agent 对话（FR-56）。

    ⚠ LLM 未配置时返回**降级提示**（`degraded=true`），不抛异常（R-AG-01），
       且不影响风控主链路与其余页面。
    """
    result = agent_chat.chat(db, body.message, body.session_id, user)
    return ok(result)


@router.post("/clear")
def clear(body: AgentClearRequest, user: RiskUser = Depends(AGENT_ROLES),
          db: Session = Depends(get_db)):
    """API-9.2 清除会话历史（FR-57）—— 仅清空消息，保留会话条目。

    ⚠ 归属校验：非 admin 只能清空自己的会话（R-AU-11 数据范围）。
    """
    agent_chat.assert_owner(db, body.session_id, user)
    result = agent_chat.clear_session(db, body.session_id)
    audit_svc.write_audit(db, "agent_call", body.session_id, "clear", user.username,
                          role=user.role, after={"reason": "清除会话历史", **result})
    return ok({"cleared": True, **result})


@router.get("/sessions/{session_id}/messages")
def session_messages(session_id: str, user: RiskUser = Depends(AGENT_ROLES),
                     db: Session = Depends(get_db)):
    """API-9.5 读取指定会话的历史消息（供前端切换会话时回填）。

    修复前前端拿不到历史消息，切换会话后只能清空数组，表现为「之前的对话消失」。
    """
    agent_chat.assert_owner(db, session_id, user)
    return ok(agent_chat.get_session_messages(db, session_id))


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, user: RiskUser = Depends(AGENT_ROLES),
                   db: Session = Depends(get_db)):
    """API-9.6 删除会话及其全部消息。

    修复前只有「清除消息」，会话条目永远留在列表里，表现为「对话删不掉」。
    """
    agent_chat.assert_owner(db, session_id, user)
    result = agent_chat.delete_session(db, session_id)
    audit_svc.write_audit(db, "agent_call", session_id, "delete", user.username,
                          role=user.role, after={"reason": "删除会话", **result})
    return ok(result)


@router.get("/sessions")
def sessions(limit: int = 20, user: RiskUser = Depends(AGENT_ROLES),
             db: Session = Depends(get_db)):
    """API-9.3 会话列表。"""
    return ok(agent_chat.list_sessions(db, user, limit))


@router.get("/tools")
def tools(_: RiskUser = Depends(AGENT_ROLES)):
    """API-9.4 可用工具清单（供前端展示，FR-64）。"""
    return ok(agent_tools.tool_catalog())


@router.get("/status")
def status(user: RiskUser = Depends(AGENT_ROLES)):
    """Agent 运行状态（供前端判断是否展示降级横幅）。"""
    return ok({
        "llm_configured": settings.llm_configured,
        # 配置问题的可操作提示（缺失 / 填成 URL / 前缀异常），供前端展示精确指引
        "config_hint": settings.llm_config_hint,
        "key_shape": settings.llm_key_shape,
        "model": settings.LLM_MODEL_NAME,
        "base_url": settings.LLM_BASE_URL,
        "temperature": settings.LLM_TEMPERATURE,
        "tool_count": len(agent_tools.ALL_TOOLS),
        "role": user.role,
    })
