"""ERCS 一键启动（v3.1 · MySQL + AI Agent）。

    python start.py                # 初始化（按需）+ 起后端 + 起前端
    python start.py --reset        # **删表重建** 并重新灌入全部数据，然后启动
    python start.py --backend      # 只起后端
    python start.py --skip-seed    # 跳过数据灌入（数据已就绪时）

前置：
  1. backend/.venv 已就绪（见 README「首次准备」）
  2. frontend/node_modules 已就绪
  3. backend/.env 已填写 DB_PASSWORD（必填）与 LLM_API_KEY（AI 助手可选）
  4. MySQL 已启动

⚠ `--reset` 走的是 `init_db.py --recreate`（删表重建），不只是清数据行。
  因为 `create_all()` 只建**不存在**的表，**不会**给已存在的表加字段 / 索引 / 外键 ——
  改过 `models.py` 后若只清数据行，会留下「模型里有字段、库里没有」的状态，
  表现为别处莫名其妙的 `Unknown column`。
"""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
VENV_PY = BACKEND / ".venv" / "Scripts" / "python.exe"
VENV_PY_POSIX = BACKEND / ".venv" / "bin" / "python"
ENV_FILE = BACKEND / ".env"

IS_WIN = os.name == "nt"


def python_exe() -> Path:
    return VENV_PY if VENV_PY.exists() else VENV_PY_POSIX


def run(cmd, cwd=None) -> int:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.call([str(c) for c in cmd], cwd=cwd, env=env)


def log(msg: str) -> None:
    print(f"\n>> {msg}" if IS_WIN else f"\n\033[36m▶ {msg}\033[0m")


def ensure_python() -> Path:
    exe = python_exe()
    if not exe.exists():
        print("✗ 未找到虚拟环境：backend/.venv")
        print("  请先执行：")
        print("    cd backend && python -m venv .venv")
        print("    .venv/Scripts/python.exe -m pip install -r requirements.txt")
        sys.exit(1)
    return exe


def check_env() -> bool:
    """检查 .env 是否已配置数据库密码（必填）。"""
    if not ENV_FILE.exists():
        print("✗ 未找到 backend/.env")
        print("  请先复制模板：cp backend/.env.example backend/.env")
        print("  然后填写 DB_PASSWORD（MySQL 密码）与 LLM_API_KEY（可选）")
        return False

    content = ENV_FILE.read_text(encoding="utf-8", errors="ignore")
    for line in content.splitlines():
        if line.strip().startswith("DB_PASSWORD="):
            value = line.split("=", 1)[1].strip()
            if not value:
                print("✗ backend/.env 的 DB_PASSWORD 为空")
                print("  请填写你本机 MySQL 的连接密码后重试")
                return False
    return True


def preflight(exe: Path) -> bool:
    """连库 + 数据就绪度预检。"""
    result = subprocess.run(
        [str(exe), "-c", "import sys; sys.path.insert(0,'.');"
         "from app.database import ping; ok,desc=ping();"
         "print(('OK|' if ok else 'FAIL|')+desc)"],
        cwd=BACKEND, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    out = (result.stdout or "").strip().splitlines()
    line = out[-1] if out else "FAIL|无法启动 Python"
    ok = line.startswith("OK|")
    print(f"  {'✓' if ok else '✗'} {line.split('|', 1)[-1]}")
    return ok


def check_llm(exe: Path) -> None:
    """提示 LLM 配置问题（不阻断启动）。"""
    result = subprocess.run(
        [str(exe), "-c", "import sys; sys.path.insert(0,'.');"
         "from app.config import settings as s;"
         "print(s.llm_key_shape + '|' + (s.llm_config_hint or ''))"],
        cwd=BACKEND, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    line = (result.stdout or "").strip().splitlines()
    if not line:
        return
    shape, _, hint = line[-1].partition("|")
    if shape == "ok":
        print("  ✓ LLM：密钥形状正常")
    elif shape == "unexpected_prefix":
        print(f"  ⚠ LLM：{hint}")
    else:
        print(f"  ⚠ LLM：{hint}")


def prepare_data(exe: Path, reset: bool, skip_seed: bool) -> None:
    """按需建库、建表、灌配置、生成业务数据、生成并回放事件。

    ⚠ `--reset` 用 `--recreate`（删表重建）而不是 `--reset`（只清数据行）：
      只清行不会 ALTER 表结构，改了 `models.py` 后跑它等于没修 schema。
    """
    log("初始化数据库结构 + 配置数据"
        + ("（--recreate：删表重建，结构按 ORM 元数据对齐）" if reset else ""))
    run([exe, "scripts/init_db.py"] + (["--recreate"] if reset else []), cwd=BACKEND)

    if skip_seed:
        return

    log("生成 17 张业务表模拟数据（默认先清空，保证可复现）")
    run([exe, "scripts/gen_business_data.py"], cwd=BACKEND)

    log("生成模拟事件数据集")
    run([exe, "scripts/gen_mock_events.py"], cwd=BACKEND)

    log("回放事件（让大盘有数据）")
    run([exe, "scripts/replay_events.py"], cwd=BACKEND)


def main() -> None:
    parser = argparse.ArgumentParser(description="ERCS 一键启动")
    parser.add_argument("--reset", action="store_true",
                        help="删表重建并重新灌入全部数据（结构按 ORM 元数据对齐）")
    parser.add_argument("--backend", action="store_true", help="只启动后端")
    parser.add_argument("--skip-seed", action="store_true", help="跳过数据生成与回放")
    parser.add_argument("--port", type=int, default=8000, help="后端端口（默认 8000）")
    args = parser.parse_args()

    print("\n" + "=" * 64)
    print("  ERCS v3.1 一键启动")
    print("=" * 64)

    exe = ensure_python()
    if not check_env():
        sys.exit(1)

    print("\n  环境预检")
    if not preflight(exe):
        print("\n  ✗ 数据库不可用。请确认：")
        print("    1) MySQL 服务已启动（Windows: services.msc → MySQL80）")
        print("    2) backend/.env 的 DB_USER / DB_PASSWORD 正确")
        sys.exit(1)

    prepare_data(exe, args.reset, args.skip_seed)

    if not args.backend and not (FRONTEND / "node_modules").exists():
        print("\n✗ 未找到 frontend/node_modules")
        print("  请先执行：cd frontend && npm install")
        sys.exit(1)

    procs = []
    log(f"启动后端  http://127.0.0.1:{args.port}   （接口文档 /docs）")
    procs.append(subprocess.Popen([str(exe), "run.py", "--port", str(args.port)],
                                  cwd=BACKEND,
                                  env={**os.environ, "PYTHONIOENCODING": "utf-8"}))
    time.sleep(3)

    if not args.backend:
        log("启动前端  http://localhost:5173")
        npm = shutil.which("npm") or "npm"
        procs.append(subprocess.Popen([npm, "run", "dev"], cwd=FRONTEND, shell=IS_WIN))

    print("\n" + "=" * 64)
    print("  ERCS 已启动")
    print("=" * 64)
    print("  前端      http://localhost:5173")
    print(f"  后端      http://127.0.0.1:{args.port}")
    print(f"  接口文档  http://127.0.0.1:{args.port}/docs")
    print("  演示账号  reviewer / strategist / analyst / admin / ops")
    print("            统一密码：<用户名>@123")
    print("=" * 64)
    print("  端到端自检：python backend/scripts/selfcheck.py")
    print("  按 Ctrl+C 停止全部服务\n")

    try:
        for p in procs:
            p.wait()
    except KeyboardInterrupt:
        print("\n正在停止服务…")
        for p in procs:
            if p.poll() is None:
                if IS_WIN:
                    p.terminate()
                else:
                    p.send_signal(signal.SIGTERM)
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
        print("已停止。")


if __name__ == "__main__":
    main()
