"""模块 9：AI Agent —— LangGraph ReAct 循环 + 会话管理 + 降级处理。

对应文档：
- Demo Doc-03 §5.2  ReAct Agent（create_react_agent + 会话管理 + 系统提示词）
- SPEC §9.6        Agent 实现（温度 0.1 / 惰性单例 / 会话存储）
- SPEC §9.7        R-AG-01（LLM 未配置降级）/ R-AG-03（惰性单例 + 独立会话）
                   R-AG-04（消息持久化）/ R-AG-05（调用留痕）/ R-AG-08（会话标题）
"""

import json
import logging
import threading
import time

from sqlalchemy.orm import Session

from ..config import settings
from ..deps import ApiError
from ..models import RiskAgentMessage, RiskAgentSession, RiskUser
from ..utils import new_id, new_session_id, now_dt
from . import tools as agent_tools

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是电商风控系统的AI助手，负责协助风控分析师完成以下工作：
1. **风险检查**：对指定用户/订单执行实时风控评估
2. **案件管理**：查询和分析待审核/已处理的风控案件
3. **用户画像**：查看用户的风险评分、行为特征、历史记录
4. **名单管理**：查询、添加、移除黑名单
5. **数据分析**：统计仪表盘、趋势分析、规则命中效果
6. **业务查询**：查看订单、售后、地址等业务数据

工作原则：
- 根据用户问题，选择合适的工具获取数据
- 用中文回答，结果需要简洁清晰
- 对风险数据给出专业的分析解读
- 可以组合多个工具完成复杂分析任务
- 当数据不足时，主动补充查询相关信息
- **严禁编造数据**：所有数值必须来自工具返回结果，不得凭空推测

风控评价体系（可用于解读结果）：
- 决策五档：Pass(0~29) / Mark 标记观察(30~59) / Challenge 二次验证(60~79) /
  Review 人审(60~79 命中不可挑战规则) / Reject 拦截(≥80)
- 评分公式：final = min(max(命中分值) + 3 × (命中数-1), 100)
- 一票否决：命中任一「极高」风险等级规则时，最终分至少 90
"""

# 惰性单例（R-AG-03）
_agent_lock = threading.Lock()
_agent_instance = None
_agent_error: str | None = None

# 说明：会话历史**不再使用进程内缓存**。
# 早期版本用 _history_cache 加速读路径，但该缓存在降级路径下不会更新、
# 无上界会持续膨胀、且多线程读写存在竞态。risk_agent_message 已有
# idx_msg_session(session_id, create_time) 索引，单会话上下文最多
# AGENT_MAX_CONTEXT_MESSAGES 条，直接回库更简单也更可靠。


class DegradedError(RuntimeError):
    """LLM 未配置或初始化失败（R-AG-01）。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# ============================================================================
# Agent 创建（惰性单例）
# ============================================================================
def get_agent():
    """创建 / 复用 ReAct Agent（Demo Doc-03 §5.2.2）。

    ⚠ 未配置 LLM_API_KEY 时抛 DegradedError，由上层转为降级响应。
    """
    global _agent_instance, _agent_error

    if _agent_instance is not None:
        return _agent_instance

    with _agent_lock:
        if _agent_instance is not None:
            return _agent_instance

        if not settings.llm_configured:
            # 按密钥形状给出精确原因（缺失 / 填成 URL / 前缀异常）
            shape = settings.llm_key_shape
            _agent_error = {
                "missing": "LLM_API_KEY_MISSING",
                "looks_like_url": "LLM_API_KEY_INVALID",
            }.get(shape, "LLM_API_KEY_MISSING")
            raise DegradedError(_agent_error)

        try:
            from langchain_openai import ChatOpenAI
            from langgraph.prebuilt import create_react_agent

            llm_kwargs = dict(
                model=settings.LLM_MODEL_NAME,
                api_key=settings.LLM_API_KEY,
                base_url=settings.LLM_BASE_URL,
                temperature=settings.LLM_TEMPERATURE,   # 0.1：风控场景需确定性输出
                timeout=settings.LLM_TIMEOUT_SECONDS,
            )
            # 仅在需要「关闭」时才追加参数：这样换成不支持该字段的端点也不会报 400。
            # 关闭后模型不再输出 reasoning_content，实测单轮耗时约降 60%。
            if not settings.LLM_ENABLE_THINKING:
                llm_kwargs["extra_body"] = {"enable_thinking": False}
            llm = ChatOpenAI(**llm_kwargs)
            _agent_instance = create_react_agent(
                model=llm,
                tools=agent_tools.ALL_TOOLS,
                prompt=SYSTEM_PROMPT,
            )
            _agent_error = None
            log.info("AI Agent 就绪：model=%s base_url=%s thinking=%s",
                     settings.LLM_MODEL_NAME, settings.LLM_BASE_URL,
                     "on" if settings.LLM_ENABLE_THINKING else "off")
            return _agent_instance
        except DegradedError:
            raise
        except Exception as exc:  # noqa: BLE001
            _agent_error = f"AGENT_INIT_FAILED: {exc}"
            log.warning("AI Agent 初始化失败：%s", exc)
            raise DegradedError(_agent_error) from exc


def reset_agent() -> None:
    """重置 Agent 单例（配置变更后调用）。"""
    global _agent_instance, _agent_error
    with _agent_lock:
        _agent_instance = None
        _agent_error = None


# ============================================================================
# 会话管理（R-AG-04 / R-AG-08）
# ============================================================================
def _ensure_session(db: Session, session_id: str | None, user: RiskUser | None,
                    first_message: str) -> str:
    if session_id:
        exists = (db.query(RiskAgentSession)
                  .filter(RiskAgentSession.session_id == session_id).first())
        if exists:
            # 归属校验：非 admin 不得续写他人的会话（数据范围隔离）
            if user and user.role != "admin" and exists.user_id != user.user_id:
                raise ApiError("4003", "无权访问该会话", status_code=403)
            return session_id

    new_sid = session_id or new_session_id()
    db.add(RiskAgentSession(
        session_id=new_sid,
        user_id=user.user_id if user else "anonymous",
        title=(first_message or "新会话")[:50],
        message_count=0,
        create_time=now_dt(),
        update_time=now_dt(),
    ))
    db.commit()
    return new_sid


def _load_history(db: Session, session_id: str) -> list:
    """加载会话历史（每次回库；会话消息量很小，索引已覆盖）。

    排序用 (create_time, message_id)：message_id 是 ULID，字典序即写入顺序，
    可避免同一秒内多条消息时 MySQL 排序不稳定导致的乱序。
    """
    from langchain_core.messages import AIMessage, HumanMessage

    rows = (db.query(RiskAgentMessage)
            .filter(RiskAgentMessage.session_id == session_id)
            .order_by(RiskAgentMessage.create_time, RiskAgentMessage.message_id).all())
    return [HumanMessage(content=r.content) if r.role == "human"
            else AIMessage(content=r.content) for r in rows]


def _persist_message(db: Session, session_id: str, role: str, content: str,
                     tool_calls: list | None = None) -> None:
    db.add(RiskAgentMessage(
        message_id=new_id("msg_"),
        session_id=session_id,
        role=role,
        content=content,
        tool_calls=tool_calls or None,
        create_time=now_dt(),
    ))
    session = (db.query(RiskAgentSession)
               .filter(RiskAgentSession.session_id == session_id).first())
    if session:
        session.message_count = int(session.message_count or 0) + 1
        session.update_time = now_dt()
    db.commit()


def list_sessions(db: Session, user: RiskUser, limit: int = 20) -> list[dict]:
    """会话列表（按更新时间倒序）。"""
    query = db.query(RiskAgentSession)
    if user.role != "admin":
        query = query.filter(RiskAgentSession.user_id == user.user_id)
    rows = query.order_by(RiskAgentSession.update_time.desc()).limit(limit).all()
    return [{
        "session_id": s.session_id,
        "title": s.title,
        "message_count": s.message_count,
        "create_time": s.create_time.isoformat() if s.create_time else None,
        "update_time": s.update_time.isoformat() if s.update_time else None,
    } for s in rows]


def assert_owner(db: Session, session_id: str, user: RiskUser) -> RiskAgentSession:
    """会话归属校验（防越权读写）。admin 可管理全部会话。"""
    session = (db.query(RiskAgentSession)
               .filter(RiskAgentSession.session_id == session_id).first())
    if not session:
        raise ApiError("4004", "会话不存在", status_code=404)
    if user.role != "admin" and session.user_id != user.user_id:
        raise ApiError("4003", "无权访问该会话", status_code=403)
    return session


def clear_session(db: Session, session_id: str) -> dict:
    """清空会话消息（FR-57）—— 保留会话条目本身。

    ⚠ 刻意**不刷新** update_time：否则清空后会话会被顶到列表最前，
       造成「越清越靠前、像删不掉」的错觉。要整个删除请用 delete_session。
    """
    deleted = (db.query(RiskAgentMessage)
               .filter(RiskAgentMessage.session_id == session_id).delete())
    session = (db.query(RiskAgentSession)
               .filter(RiskAgentSession.session_id == session_id).first())
    if session:
        session.message_count = 0
    db.commit()
    return {"session_id": session_id, "deleted_messages": deleted}


def get_session_messages(db: Session, session_id: str) -> list[dict]:
    """读取会话历史消息（供前端切换会话时回填）。

    返回结构与前端 `messages` 一致：`{role, content, tool_calls}`，
    这样前端切换会话后能完整还原气泡与工具调用卡片。
    """
    rows = (db.query(RiskAgentMessage)
            .filter(RiskAgentMessage.session_id == session_id)
            .order_by(RiskAgentMessage.create_time, RiskAgentMessage.message_id).all())
    return [{
        "role": "me" if r.role == "human" else "ai",
        "content": r.content,
        "tool_calls": r.tool_calls or [],
    } for r in rows]


def delete_session(db: Session, session_id: str) -> dict:
    """彻底删除会话（会话行 + 全部消息）。"""
    deleted_messages = (db.query(RiskAgentMessage)
                        .filter(RiskAgentMessage.session_id == session_id).delete())
    deleted_sessions = (db.query(RiskAgentSession)
                        .filter(RiskAgentSession.session_id == session_id).delete())
    db.commit()
    return {"session_id": session_id,
            "deleted_messages": deleted_messages,
            "deleted_sessions": deleted_sessions}


# ============================================================================
# 工具调用提取
# ============================================================================
def _this_turn(messages: list) -> list:
    """只取**本轮**产生的消息（最后一条人类消息之后的部分）。

    否则历史轮次的工具调用会被重复计入，前端每轮都会看到累积的工具列表。
    """
    last_human = -1
    for idx, msg in enumerate(messages):
        if getattr(msg, "type", "") == "human":
            last_human = idx
    return messages[last_human + 1:] if last_human >= 0 else messages


def _extract_tool_calls(messages: list) -> list[dict]:
    """从 LangGraph 返回的消息序列里提取**本轮**工具调用（FR-65 / R-FE2-18）。"""
    calls: list[dict] = []
    pending: dict[str, dict] = {}
    messages = _this_turn(messages)

    for msg in messages:
        msg_type = getattr(msg, "type", "")
        if msg_type == "ai":
            for tc in (getattr(msg, "tool_calls", None) or []):
                call_id = tc.get("id") or tc.get("name")
                record = {
                    "tool": tc.get("name", ""),
                    "args": tc.get("args", {}) or {},
                    "result_summary": "",
                    "cost_ms": 0,
                    "status": "ok",
                }
                pending[call_id] = record
                calls.append(record)
        elif msg_type == "tool":
            call_id = getattr(msg, "tool_call_id", None)
            content = getattr(msg, "content", "")
            record = pending.get(call_id)
            if record is not None:
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                record["result_summary"] = text[:200] + ("…" if len(text) > 200 else "")
                if text.startswith(("风险检查失败", "案件查询失败", "画像查询失败",
                                    "名单管理失败", "仪表盘统计查询失败", "趋势分析失败",
                                    "规则效果分析失败", "业务数据查询失败")):
                    record["status"] = "error"
    return calls


def _summarize_tools(calls: list[dict]) -> list[str]:
    """工具调用 → 审计 detail 用的摘要行。"""
    return [f"{c['tool']}({json.dumps(c['args'], ensure_ascii=False)}) → {c['status']}"
            for c in calls]


# ============================================================================
# 对话主入口
# ============================================================================
def chat(db: Session, message: str, session_id: str | None,
         user: RiskUser | None = None) -> dict:
    """Agent 对话（API-9.1）。

    返回 `{reply, session_id, tool_calls, degraded, degrade_reason}`。
    ⚠ LLM 未配置时返回**降级提示**而非抛异常（R-AG-01）。
    """
    sid = _ensure_session(db, session_id, user, message)

    # ⚠ 顺序很关键：**先读历史，再落库本轮提问**。
    # 若先落库再读，_load_history 会把本轮提问一并读出，随后再 append 一次，
    # 会导致首轮用户消息重复进入模型上下文（模型看到两遍同一个问题）。
    history = _load_history(db, sid)
    history = history[-settings.AGENT_MAX_CONTEXT_MESSAGES:]
    _persist_message(db, sid, "human", message)

    operator = user.username if user else "agent"
    agent_tools.current_operator.set(operator)

    # ---- 降级路径 ----
    try:
        agent = get_agent()
    except DegradedError as exc:
        reply = _degraded_reply(exc.reason) + _degraded_tail()
        _persist_message(db, sid, "ai", reply)
        return {"reply": reply, "session_id": sid, "tool_calls": [],
                "degraded": True, "degrade_reason": exc.reason}

    # ---- 正常路径 ----
    # history 已在上方读取（且不含本轮提问），此处只需追加本轮提问
    try:
        from langchain_core.messages import HumanMessage

        history.append(HumanMessage(content=message))
        t0 = time.perf_counter()
        try:
            result = agent.invoke({"messages": history})
        except Exception as conn_exc:  # noqa: BLE001
            # 连接类失败 → 重建 Agent 单例后重试一次（自愈）。
            # 触发场景：进程长时间运行后遭遇系统休眠 / 网络切换，进程内连接池
            # 的 socket 全部失效，而进程自身不会重建 —— 表现为「昨天还好好的，
            # 今天一直连不上」，以往只能靠重启后端。重建单例即可恢复。
            if not _is_connection_error(conn_exc):
                raise
            log.warning("Agent 调用连接失败，重建客户端后重试一次：%s", conn_exc)
            reset_agent()
            result = get_agent().invoke({"messages": list(history)})
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        messages = result.get("messages", [])
        reply = messages[-1].content if messages else "抱歉，未能生成回复。"
        if not isinstance(reply, str):
            reply = json.dumps(reply, ensure_ascii=False)

        tool_calls = _extract_tool_calls(messages)
        if tool_calls and elapsed_ms:
            tool_calls[-1]["cost_ms"] = elapsed_ms

        # 持久化（历史下次直接从库读，无需进程内缓存）
        _persist_message(db, sid, "ai", reply, tool_calls)

        # R-AG-05：工具调用留痕（biz_type = agent_call）
        if tool_calls:
            from ..services.step6_audit import write_audit
            write_audit(db, biz_type="agent_call", biz_id=sid, action="tool",
                        operator=operator,
                        detail={"tools": _summarize_tools(tool_calls),
                                "elapsed_ms": elapsed_ms})

        return {"reply": reply, "session_id": sid, "tool_calls": tool_calls,
                "degraded": False, "degrade_reason": None}

    except Exception as exc:  # noqa: BLE001  工具异常 / LLM 异常都不中断会话
        # 归类为可操作的原因：401 → 密钥无效、超时/连接失败 → 网络不通…
        reason = classify_llm_error(exc)
        log.warning("Agent 执行异常（归类为 %s）：%s", reason, exc)
        reply = _degraded_reply(reason) + _degraded_tail()
        _persist_message(db, sid, "ai", reply)
        return {"reply": reply, "session_id": sid, "tool_calls": [],
                "degraded": True, "degrade_reason": reason}


def _is_connection_error(exc: Exception) -> bool:
    """是否为「连不上」类错误（区别于「纯粹太慢」的超时）。

    为什么要分开：连接失败是**可自愈**的 —— 重建客户端即可恢复；
    而超时重试只会让用户再等一个超时周期，是纯粹的伤害。

    场景：进程长时间运行后遭遇系统休眠 / 网络切换，进程内 httpx 连接池
    里的 socket 会全部失效，且进程自身不会重建 → 表现为「昨天好好的，
    今天一直连不上」。此时重建单例即可恢复。
    """
    # 超时类：明确不重试
    timeout_names = {"APITimeoutError", "ReadTimeout", "TimeoutException",
                     "PoolTimeout", "WriteTimeout"}
    # 连接类：重试
    conn_names = {"APIConnectionError", "ConnectError", "ConnectTimeout",
                  "ConnectionError", "ConnectionResetError",
                  "ConnectionAbortedError", "RemoteProtocolError"}

    if type(exc).__name__ in timeout_names:
        return False
    if type(exc).__name__ in conn_names:
        return True

    # 沿异常链向下找 httpx / 标准库的底层异常
    cur: BaseException | None = exc
    for _ in range(6):
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
        if cur is None:
            break
        name = type(cur).__name__
        if name in timeout_names:
            return False
        if name in conn_names:
            return True

    text = str(exc).lower()
    if "timed out" in text or "timeout" in text:
        return False
    return "connection error" in text or "connection reset" in text


def classify_llm_error(exc: Exception) -> str:
    """把底层异常归类成可操作的降级原因（R-AG-01 / R-AG-02）。

    - `LLM_API_KEY_INVALID` 密钥无效（401 / invalid_api_key）
    - `LLM_UNREACHABLE`     网络不通 / 超时
    - `LLM_MODEL_NOT_FOUND` 模型名不存在（404 / model_not_found）
    - `AGENT_RUNTIME_ERROR` 其他
    """
    text = str(exc)
    lowered = text.lower()
    if "401" in text or "invalid_api_key" in lowered or "incorrect api key" in lowered:
        return "LLM_API_KEY_INVALID"
    if "model_not_found" in lowered or "404" in text:
        return "LLM_MODEL_NOT_FOUND"
    if any(k in lowered for k in ("timeout", "timed out", "connection", "unreachable",
                                  "getaddrinfo", "ssl")):
        return "LLM_UNREACHABLE"
    return "AGENT_RUNTIME_ERROR"


def _degraded_reply(reason: str) -> str:
    """降级提示文案 —— 每种原因都给出**可操作**的修复步骤（R-AG-01）。"""
    header = "AI 助手当前**不可用**。\n\n"

    if reason == "LLM_API_KEY_MISSING":
        return header + (
            "**原因**：未配置 `LLM_API_KEY`。\n\n"
            "**修复**：在 `backend/.env` 中填入阿里云百炼（DashScope）API Key 后重启服务：\n"
            "```\nLLM_API_KEY=sk-xxxxxxxx\n"
            "LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1\n"
            "LLM_MODEL_NAME=qwen-plus\n```"
        )

    if reason == "LLM_API_KEY_INVALID":
        return header + (
            "**原因**：`LLM_API_KEY` 无效 —— 服务端返回 `401 Incorrect API key`。\n\n"
            "**常见误填**：把**接口地址**填进了密钥字段。\n\n"
            "**正确写法**（`.env`）：\n"
            "```\n"
            "# 密钥：只放 sk- 开头的这一串\n"
            "LLM_API_KEY=sk-xxxxxxxxxxxxxxxx\n"
            "# 地址：放接口端点（若用私有 MaaS 端点，换成你自己的地址）\n"
            "LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1\n"
            "```\n\n"
            "密钥可在 [阿里云百炼控制台](https://bailian.console.aliyun.com/) → API-KEY 管理中获取。"
        )

    if reason == "LLM_MODEL_NOT_FOUND":
        return header + (
            f"**原因**：模型 `{settings.LLM_MODEL_NAME}` 不存在或当前账号无权访问。\n\n"
            "**修复**：在 `.env` 中改用可用模型（`qwen-turbo` / `qwen-plus` / `qwen-max`）后重启服务。"
        )

    if reason == "LLM_UNREACHABLE":
        return header + (
            f"**原因**：无法连接 LLM 服务（`{settings.LLM_BASE_URL}`）。\n\n"
            "**排查**：① 本机网络是否可访问该地址；② 是否需要走代理；\n"
            "③ `LLM_BASE_URL` 是否写错（注意结尾的 `/v1`）。"
        )

    return header + (
        f"**原因**：{reason}。\n\n"
        "**排查**：检查 `backend/.env` 的 LLM 配置与网络连通性。"
    )


def _degraded_tail() -> str:
    """降级文案的统一收尾：强调不影响主链路。"""
    return ("\n\n---\n"
            "⚠ 以上问题**不影响**风控主链路与其余 12 个页面 —— "
            "可改用「态势大盘」「趋势分析」「审核工作台」等页面完成分析。")
