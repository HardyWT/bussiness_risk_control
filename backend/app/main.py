"""FastAPI 应用入口：路由注册、CORS、指标中间件、统一异常处理、启动预热。

对应文档：
- SPEC §0.4.3 全局目录结构（main.py = 应用入口，注册路由）
- SPEC §0.4.1 系统架构（模块 11 以依赖注入挂载到受保护接口）
- SPEC §8.1   页面 → API 映射
- SPEC R-MA-12 接口调用计数器（metrics 中间件）
"""

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import metrics
from .config import settings
from .database import create_all, ping, session_scope
from .deps import ApiError
from .engine.step3_rule import clear_rule_cache
from .routers import (admin, agent, audit, auth, case, dashboard, profile,
                      risk, rule, simulate)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
)
log = logging.getLogger("ercs")


# 二次验证超时兜底扫描的周期（秒）。R-RE-15 要求「超时按失败处理」，
# 而超时是**时间的函数**、不是请求的函数 —— 用后台定时任务实现，
# 而不是挂在某个接口的请求路径上（见下面 lifespan 的说明）。
CHALLENGE_SWEEP_INTERVAL_SECONDS = 30


async def _challenge_sweep_loop() -> None:
    """周期性把超时的 pending 二次验证升级为 Review（R-RE-15）。

    ⚠ **不要把它塞进决策请求路径**。曾经的做法是在 `run_risk_check()` 返回前扫一次，
    于是「任意一个事件」都会顺手改写**历史上所有**已超窗口的挑战：
    111 条评估被静默改成 Review，大盘「决策分布」里的 Challenge 一次请求就从 111 变 0，
    而且审计里查不到是谁改的 —— 副作用不属于发起它的那个请求。

    正确形态就是这里：进程启动即扫一次（覆盖停机期间积累的），之后每 30 秒一次。
    """
    import asyncio

    from .services import step3_challenge as challenge_svc

    while True:
        await asyncio.sleep(CHALLENGE_SWEEP_INTERVAL_SECONDS)
        try:
            with session_scope() as db:
                swept = challenge_svc.sweep_expired(db)
            if swept:
                log.info("二次验证兜底扫描：%d 条超时挑战已升级为 Review", swept)
        except Exception as exc:  # noqa: BLE001  周期性任务不能因单次失败而退出
            log.warning("二次验证兜底扫描失败（下个周期重试）：%s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动：建表 + 创建审计触发器 + 预热滑动窗口 + 启动超时兜底扫描。"""
    import asyncio

    try:
        create_all()
        ok_db, desc = ping()
        if ok_db:
            log.info("数据库就绪：%s", desc)
        else:
            log.warning("数据库连接异常（不影响启动，接口会报错）：%s", desc)
    except Exception as exc:  # noqa: BLE001
        log.warning("建表/连接失败：%s", exc)

    try:
        from .engine.step2_feature import warmup_from_db
        with session_scope() as db:
            n = warmup_from_db(db)
        clear_rule_cache()
        log.info("滑动窗口预热完成，回灌历史事件 %d 条", n)
    except Exception as exc:  # noqa: BLE001
        log.warning("预热失败（不影响启动）：%s", exc)

    # 二次验证超时兜底：先立刻扫一次（补上停机期间积累的），再交给周期任务
    try:
        from .services import step3_challenge as challenge_svc
        with session_scope() as db:
            swept = challenge_svc.sweep_expired(db)
        if swept:
            log.info("启动兜底扫描：%d 条超时二次验证已升级为 Review", swept)
    except Exception as exc:  # noqa: BLE001
        log.warning("启动兜底扫描失败（不影响启动）：%s", exc)

    sweep_task = asyncio.create_task(_challenge_sweep_loop())

    if not settings.llm_configured:
        log.warning("LLM_API_KEY 未配置 —— AI 助手将走降级路径（R-AG-01），"
                    "风控主链路不受影响")

    yield

    sweep_task.cancel()
    try:
        await sweep_task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
    log.info("服务关闭")


app = FastAPI(
    title="电商风险控制系统 ERCS",
    description="实时事件接入 → 33 特征计算 → 名单+规则引擎 → 评分与一票否决 → 五档决策 → 案件复核 → 处置留痕；集成 LangGraph AI 助手",
    version="3.1",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173",
                   "http://localhost:4173", "http://127.0.0.1:4173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ================================================================ 指标中间件
@app.middleware("http")
async def count_api_calls(request: Request, call_next):
    """按「方法 + 路由模板」累加调用数 / 错误数 / 耗时（R-MA-12）。

    ⚠ 路由模板要等 `call_next` 之后才能从 `request.scope["route"]` 拿到，
       所以先计时、后取键。
    """
    t0 = time.perf_counter()
    response = await call_next(request)
    if metrics.should_count(request.url.path):
        elapsed_ms = (time.perf_counter() - t0) * 1000
        route = request.scope.get("route")
        template = getattr(route, "path", None)
        metrics.record(metrics.make_key(request.method, template, request.url.path),
                       response.status_code, elapsed_ms)
    return response


# ================================================================ 统一响应包装
@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    detail = exc.detail if isinstance(exc.detail, dict) else {"code": "5000", "message": str(exc.detail)}
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": detail.get("code", "5000"),
                 "message": detail.get("message", "error"), "data": None},
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    """Pydantic 校验失败 → 400 + code 1002/1003，message 指出字段名（R-EV-02）。"""
    errors = exc.errors()
    first = errors[0] if errors else {}
    loc = [str(x) for x in first.get("loc", []) if x not in ("body", "query", "path")]
    field = ".".join(loc) or "参数"
    msg = first.get("msg", "参数校验失败")
    return JSONResponse(
        status_code=400,
        content={"code": "1002",
                 "message": f"缺少必填字段或格式错误：{field}（{msg}）", "data": None},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """保证 404/405 也走统一包装。

    ⚠ 审计删除接口（DELETE /api/v1/audit/{id}）**故意不存在** → 返回 404/405。
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": str(exc.status_code), "message": str(exc.detail), "data": None},
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    log.exception("未处理异常 %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500,
                        content={"code": "5000", "message": f"内部错误：{exc}", "data": None})


# ================================================================ 路由注册
app.include_router(auth.router)          # 模块 11 认证与授权
app.include_router(risk.router)          # 模块 1  事件网关
app.include_router(rule.router)          # 模块 3  规则 / 场景 / 特征 / 名单 / 挑战
app.include_router(case.router)          # 模块 4  案件流转
app.include_router(profile.router)       # 模块 5  画像与图谱
app.include_router(dashboard.router)     # 模块 6  大盘与健康度
app.include_router(audit.router)         # 模块 6  审计日志
app.include_router(simulate.router)      # 模块 7  仿真测试
app.include_router(agent.router)         # 模块 9  AI 智能助手
app.include_router(admin.router)         # 运维辅助


@app.get("/", include_in_schema=False)
def index():
    return {"code": 0, "message": "ERCS 后端运行中", "data": {
        "version": "3.1", "docs": "/docs", "health": "/api/v1/admin/health",
        "llm_configured": settings.llm_configured}}
