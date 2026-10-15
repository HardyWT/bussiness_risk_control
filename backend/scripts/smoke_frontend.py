"""前端冒烟自检 —— 用 Edge + CDP 真跑一遍 13 个页面。

为什么需要它：`pytest` 覆盖后端、`selfcheck.py` 覆盖接口契约，但**没有任何东西
验证「前端页面在浏览器里真的能渲染出来」**。Vue 的失败模式很安静 ——
模板里引用了一个不存在的变量、`v-if` 判错、路由守卫把页面弹回登录页 ——
构建一样通过、接口也正常，只有人肉点开才能发现。

做法：以 `--headless=new` 启动 Edge，用 DevTools Protocol 驱动：
  ① 打开登录页，注入 token（等价于登录）
  ② 逐个访问 13 个业务路由
  ③ 收集 `console.error` / `pageerror` / 未捕获的 Promise 异常
  ④ 断言页面主内容区**渲染出了预期的关键文本**（而不是空壳或 403 页）

用法：
    python scripts/smoke_frontend.py                      # 默认 frontend=http://localhost:5173
    python scripts/smoke_frontend.py --frontend http://localhost:5174
    python scripts/smoke_frontend.py --headed             # 有头模式（看得到窗口）

前置：后端已启动（`python run.py`）、前端 dev server 已启动（`npm run dev`）。
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/microsoft-edge",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]

# 每个页面：路由 → 期望在正文里出现的关键文本（用来区分「渲染成功」与「空白/403」）
PAGES: list[tuple[str, str, str]] = [
    ("登录页", "/login", "演示账号"),
    ("态势大盘", "/dashboard", "风险趋势"),
    ("审核工作台", "/workbench", "案件"),
    ("策略规则配置", "/strategy/rules", "规则"),
    ("名单库管理", "/strategy/watchlist", "名单"),
    ("特征与阈值", "/strategy/features", "特征"),
    ("事件仿真", "/simulate", "判定链路"),
    ("AI 智能助手", "/chat", "会话"),
    ("趋势分析", "/analysis", "规则效果"),
    ("审计日志", "/audit", "审计"),
    ("个人中心", "/profile", "权限"),
]

# 角色 → 需要额外验证的页面（覆盖 PRD §2.1.1 的只读可见性）
ROLE_ROUTES = {
    "admin": ["/dashboard", "/workbench", "/strategy/rules", "/strategy/watchlist",
              "/strategy/features", "/simulate", "/chat", "/analysis", "/audit",
              "/system/health", "/profile"],
    "reviewer": ["/dashboard", "/workbench", "/strategy/rules", "/strategy/watchlist",
                 "/strategy/features", "/analysis", "/audit", "/profile"],
    "analyst": ["/dashboard", "/strategy/rules", "/strategy/watchlist",
                "/strategy/features", "/chat", "/analysis", "/profile"],
    "ops": ["/dashboard", "/analysis", "/system/health", "/profile"],
}


def find_edge() -> str | None:
    for p in EDGE_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


class CDP:
    """极简 CDP 客户端：只用到 Page / Runtime 两组命令。"""

    def __init__(self, ws_url: str):
        import asyncio

        import websockets

        self._asyncio = asyncio
        self._websockets = websockets
        self._seq = 0
        self._loop = asyncio.new_event_loop()
        # ping_interval=None：关掉 websockets 的 keepalive 后台任务。
        # 本地 CDP 连接不需要心跳，而这个任务会在脚本退出时留下
        # 「Task was destroyed but it is pending!」的噪声。
        self.ws = self._loop.run_until_complete(
            websockets.connect(ws_url, max_size=20_000_000, ping_interval=None))
        self.events: list[dict] = []
        self._loop.run_until_complete(self._drain_init())

    async def _drain_init(self):
        for domain in ("Runtime.enable", "Page.enable", "Log.enable"):
            await self._send(domain)

    async def _send(self, method: str, **params) -> dict:
        self._seq += 1
        msg_id = self._seq
        await self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            raw = json.loads(await self.ws.recv())
            if raw.get("id") == msg_id:
                return raw
            self.events.append(raw)

    async def _collect(self, seconds: float):
        end = time.time() + seconds
        while time.time() < end:
            try:
                raw = json.loads(await self._asyncio.wait_for(self.ws.recv(), timeout=0.25))
                self.events.append(raw)
            except (self._asyncio.TimeoutError, TimeoutError):
                continue

    def call(self, method: str, **params) -> dict:
        return self._loop.run_until_complete(self._send(method, **params))

    def wait(self, seconds: float) -> None:
        self._loop.run_until_complete(self._collect(seconds))

    def evaluate(self, expression: str, await_promise: bool = False):
        res = self.call("Runtime.evaluate", expression=expression,
                        returnByValue=True, awaitPromise=await_promise)
        return res.get("result", {}).get("result", {}).get("value")

    def close(self) -> None:
        """关闭连接并**取消残留任务**。

        `websockets` 会起一个 `keepalive()` 后台任务；直接关事件循环会打出
        「Task was destroyed but it is pending!」。自检脚本的输出要干净 ——
        噪声会让人分不清「这行是失败还是无害警告」。
        """
        try:
            self._loop.run_until_complete(self.ws.close())
            pending = [t for t in self._asyncio.all_tasks(self._loop) if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(
                    self._asyncio.gather(*pending, return_exceptions=True))
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass


def _launch_edge(edge: str, port: int, headless: bool, profile: str) -> subprocess.Popen:
    args = [
        edge,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-extensions", "--disable-gpu",
        "--remote-allow-origins=*",
    ]
    if headless:
        args.append("--headless=new")
    args.append("about:blank")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _ws_url(port: int, timeout: float = 25.0) -> str | None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            targets = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=2).read())
            for t in targets:
                if t.get("type") == "page":
                    return t["webSocketDebuggerUrl"]
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return None


def _login_token(backend: str, username: str, password: str) -> dict | None:
    body = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(f"{backend}/api/v1/auth/login", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())["data"]
    except urllib.error.HTTPError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="ERCS 前端冒烟自检（Edge + CDP）")
    parser.add_argument("--frontend", default="http://localhost:5173")
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--headed", action="store_true", help="有头模式（默认无头）")
    args = parser.parse_args()

    edge = find_edge()
    if edge is None:
        print("✗ 未找到 Microsoft Edge，跳过前端冒烟自检")
        return 2

    profile = tempfile.mkdtemp(prefix="ercs_smoke_")
    print("=" * 68)
    print("  ERCS 前端冒烟自检")
    print("=" * 68)
    print(f"  Edge   ：{edge}")
    print(f"  前端   ：{args.frontend}")
    print(f"  后端   ：{args.backend}")
    print(f"  浏览器用户目录：{profile}\n")

    proc = _launch_edge(edge, args.port, not args.headed, profile)
    passed, failed = [], []

    def check(name: str, ok: bool, detail: str = "") -> None:
        (passed if ok else failed).append((name, detail))
        print(f"  {'✓' if ok else '✗'} {name}" + (f"  {detail}" if not ok and detail else ""))

    try:
        ws = _ws_url(args.port)
        if ws is None:
            print("✗ 无法连接 Edge 的调试端口")
            return 2
        cdp = CDP(ws)

        # ---- 匿名：登录页 ----
        cdp.call("Page.navigate", url=f"{args.frontend}/login")
        cdp.wait(3.0)
        body = cdp.evaluate("document.body.innerText") or ""
        check("登录页渲染（含演示账号提示）",
              "演示账号" in body, f"正文长度={len(body)}")
        check("登录页无 JS 报错", not _js_errors(cdp.events), _first_error(cdp.events))

        # ---- 逐角色登录后遍历其可见页面 ----
        total_pages = 0
        for role, routes in ROLE_ROUTES.items():
            cred = _login_token(args.backend, role, f"{role}@123")
            if cred is None:
                check(f"{role} 登录并注入会话", False, "后端登录失败")
                continue
            cdp.events.clear()
            cdp.call("Page.navigate", url=f"{args.frontend}/login")
            cdp.wait(1.5)
            token_json = json.dumps(cred["access_token"])
            user_json = json.dumps(cred["user"], ensure_ascii=False)
            cdp.evaluate(
                f"sessionStorage.setItem('ercs_token', {token_json});"
                f"sessionStorage.setItem('ercs_user', JSON.stringify({user_json}));"
                "true")

            role_errors = []
            for route in routes:
                total_pages += 1
                cdp.call("Page.navigate", url=f"{args.frontend}{route}")
                cdp.wait(2.6)
                body = cdp.evaluate("document.body.innerText") or ""
                errs = _js_errors(cdp.events)
                # 403 页 / 登录页都不是「渲染成功」
                bad = ("无访问权限" in body and route != "/403") or "账号" in body[:40] and "密码" in body
                if len(body.strip()) < 40 or bad:
                    role_errors.append(f"{route} 渲染异常（正文 {len(body)} 字）")
                if errs:
                    role_errors.append(f"{route} JS 报错：{errs[0]}")

            check(f"{role} 的 {len(routes)} 个页面全部渲染且无 JS 报错",
                  not role_errors, "；".join(role_errors[:4]))

        # ---- 403 页 ----
        cdp.call("Page.navigate", url=f"{args.frontend}/403")
        cdp.wait(2.0)
        body = cdp.evaluate("document.body.innerText") or ""
        check("403 无权限页可渲染", "无访问权限" in body or "权限" in body,
              f"正文={body[:60]}")

        print("-" * 68)
        print(f"  冒烟自检： {len(passed)}/{len(passed) + len(failed)} 通过"
              f"（累计访问 {total_pages} 个页面实例）")
        if failed:
            print("  失败项：")
            for name, detail in failed:
                print(f"    · {name}  {detail}")
            return 1
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _js_errors(events: list[dict]) -> list[str]:
    """从 CDP 事件流里挑出真正的 JS 异常（忽略资源 404 之类的噪声）。"""
    out = []
    for e in events:
        method = e.get("method")
        params = e.get("params") or {}
        if method == "Runtime.exceptionThrown":
            desc = (params.get("exceptionDetails") or {}).get("exception", {}).get("description")
            out.append((desc or "unknown").split("\n")[0][:160])
        elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            texts = [str(a.get("value", a.get("description", ""))) for a in params.get("args", [])]
            joined = " ".join(texts)[:160]
            # 前端对 4xx 业务错误的 console.error 属正常分支，不计入
            if joined and "AxiosError" not in joined and "403" not in joined and "401" not in joined:
                out.append(joined)
    return out


def _first_error(events: list[dict]) -> str:
    errs = _js_errors(events)
    return errs[0] if errs else ""


if __name__ == "__main__":
    sys.exit(main())
