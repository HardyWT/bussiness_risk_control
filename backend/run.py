"""后端启动脚本。

用法：
    python run.py                 # 默认 127.0.0.1:8000
    python run.py --port 8001
    python run.py --host 0.0.0.0 --reload

启动前做一次预检（连库 + 数据就绪 + LLM 配置），把常见问题在启动阶段就暴露出来。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ⚠ Windows 控制台默认是 GBK（cp936），打印 `✓` / `✗` / `⚠` 会直接抛
#   `UnicodeEncodeError: 'gbk' codec can't encode character '✓'`，
#   而预检里的 print 没有兜底 —— 结果是「脚本在第一步就崩，看起来像数据库挂了」。
#   把标准输出强制为 UTF-8，与 `start.py` 保持一致。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # 已被重定向为不支持 reconfigure 的对象
        pass

import uvicorn  # noqa: E402

from app.config import settings  # noqa: E402


def probe_llm(timeout: float = 8.0) -> tuple[bool, str]:
    """轻量探测 LLM 端点连通性（`GET {base_url}/models`）。

    用最短路径回答一个问题：**本进程**能不能出网访问 LLM 服务。
    只做 health-check 级别的探测，不发起真实推理，失败也不阻断启动。

    返回 (是否可达, 人类可读描述)。
    """
    url = (settings.LLM_BASE_URL or "").rstrip("/") + "/models"
    try:
        import httpx

        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"},
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"不可达（{type(exc).__name__}: {exc}）"

    if resp.status_code == 200:
        return True, f"可达 · {url}"
    if resp.status_code in (401, 403):
        return False, f"密钥被拒（HTTP {resp.status_code}）—— 检查 LLM_API_KEY"
    if resp.status_code == 404:
        return False, (f"端点不存在（HTTP 404）—— 检查 LLM_BASE_URL 是否以 "
                       f"/v1 结尾：{url}")
    return True, f"可达（HTTP {resp.status_code}）"


def preflight() -> None:
    print("\n" + "-" * 62)
    print("  启动预检")
    print("-" * 62)

    # 1. 数据库连通性
    try:
        from app.database import ping
        ok_db, desc = ping()
        print(f"  {'✓' if ok_db else '✗'} 数据库：{desc}")
        if not ok_db:
            print("\n  ⚠ 请检查 backend/.env 的 DB_* 配置（尤其是 DB_PASSWORD），")
            print("    并确认 MySQL 已启动、目标库已创建：")
            print("      python scripts/init_db.py")
            return
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 数据库：{exc}")
        return

    # 2. 数据就绪度
    try:
        from sqlalchemy import func

        from app.database import session_scope
        from app.models import RiskEvent, RiskRule, RiskRuleScene, RiskUser

        with session_scope() as db:
            users = db.query(func.count(RiskUser.user_id)).scalar() or 0
            rules = db.query(func.count(RiskRule.rule_id)).scalar() or 0
            scenes = db.query(func.count(RiskRuleScene.scene_code)).scalar() or 0
            events = db.query(func.count(RiskEvent.event_id)).scalar() or 0

        print(f"  ✓ 演示账号 {users} 个 · 规则 {rules} 条 · 场景 {scenes} 个 · 事件 {events} 条")
        if users == 0 or rules == 0:
            print("    ⚠ 配置数据缺失，请执行：python scripts/init_db.py")
        if events == 0:
            print("    ⚠ 无事件数据，大盘将为空；建议执行：")
            print("      python scripts/gen_business_data.py")
            print("      python scripts/gen_mock_events.py")
            print("      python scripts/replay_events.py")
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠ 预检查询失败（不影响启动）：{exc}")

    # 3. LLM 配置
    if settings.llm_configured:
        print(f"  ✓ LLM 配置：{settings.LLM_MODEL_NAME} @ {settings.LLM_BASE_URL}")
        # 3.1 真实连通性探测 —— 提前暴露「本进程出网被拦」这类问题，
        #     否则要等用户开口对话才发现 AI 助手在降级（LLM_UNREACHABLE）。
        ok, desc = probe_llm()
        print(f"  {'✓' if ok else '✗'} LLM 连通性：{desc}")
        if not ok:
            print("    ⚠ AI 助手将返回降级提示（LLM_UNREACHABLE）。")
            print("      若密钥/地址无误却连不通，通常是**启动本进程的环境**无法访问外网：")
            print("      在沙箱 / 容器 / 受限终端里启动时，子进程继承不到出网权限 ——")
            print("      换个普通终端（或在本机 CMD / PowerShell）重启后端即可。")
    else:
        print("  ⚠ LLM：LLM_API_KEY 未配置 —— AI 助手走降级路径（R-AG-01）")
        print("    风控主链路与其余页面不受影响；填入 backend/.env 后即可启用")

    print("-" * 62)


def main() -> None:
    parser = argparse.ArgumentParser(description="ERCS 后端启动")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="开发模式自动重载")
    args = parser.parse_args()

    preflight()
    # print 默认全缓冲：启动日志重定向到文件时预检结论会被吞掉，显式刷一次
    sys.stdout.flush()
    print(f"\n  接口文档： http://{args.host}:{args.port}/docs")
    print(f"  存活探针： http://{args.host}:{args.port}/api/v1/admin/health\n")

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        app_dir=os.path.dirname(os.path.abspath(__file__)),
    )


if __name__ == "__main__":
    main()
