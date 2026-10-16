"""前端冒烟自检 —— 用 Edge + CDP 真跑一遍 13 个页面。

为什么需要它：`pytest` 覆盖后端、`selfcheck.py` 覆盖接口契约，但**没有任何东西
验证「前端页面在浏览器里真的能渲染出来」**。Vue 的失败模式很安静 ——
模板里引用了一个不存在的变量、`v-if` 判错、路由守卫把页面弹回登录页 ——
构建一样通过、接口也正常，只有人肉点开才能发现。

做法：以 `--headless=new` 启动 Edge，用 DevTools Protocol 驱动：
  ① 打开登录页，注入 token（等价于登录）
  ② 逐个访问各角色可见的业务路由
  ③ 收集 `console.error` / `pageerror` / 未捕获的 Promise 异常
  ④ 断言页面主内容区**渲染出了预期的关键文本**（`PAGES`，而不是空壳或 403 页）
  ⑤ 断言**点名的数据列不得整列为空**（`COLUMN_CHECKS`）——
     这一层专门抓「前端读错字段名」：它有兜底不抛错、页面照常挂载，
     前三层判据全部通过，只有整列 `-` 这一个可见症状。

> ⚠ ④⑤ 两层是 v3.1 补的。在此之前 `PAGES` 从未被引用（死代码），
>   于是「页面渲染出正确内容」这件事根本没人验，四类字段名静默失效全部漏过。

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

# 每个页面：路由 → 期望在正文里出现的**可见**关键文本。
#
# ⚠ 这张表曾经是**死代码**：它被定义在此、却从未被 main() 引用，
#   于是「页面渲染出预期内容」这一层断言实际不存在，只跑得动
#   「没有 JS 异常」+「#app 有子节点」。本次修的四类字段名缺陷
#   （时间列 / 名单移除 / 规则状态列 / 失败次数列）全是静默显示错值，
#   既不打 console.error 也不影响挂载 —— 三层判据全绿，一个都没抓到。
#   现在它接进了主循环，且**期望值都逐条核对过是模板里的可见文本**。
#
# ⚠ 选词标准（否则会误报，比没有断言更糟）：
#   ① 必须是渲染出来的**文本节点**，不能是 HTML 注释里的字（注释不进 innerText）；
#   ② 不能是 `placeholder` 等属性值（同理）；
#   ③ 不能落在仅某些角色可见的块里（本表按路由断言，四个角色都会跑）；
#   ④ 优先选卡片标题 / 表头这类**与数据内容无关**的静态文案。
#   历史上 `/dashboard` 写的是「风险趋势」、`/simulate` 写的是「判定链路」，
#   两个词一个已随图例改名、一个只存在于注释里 —— 直接接上会立刻误报。
PAGES: list[tuple[str, str, str]] = [
    ("登录页", "/login", "演示账号"),
    ("态势大盘", "/dashboard", "今日评估量"),
    ("审核工作台", "/workbench", "案件列表"),
    ("策略规则配置", "/strategy/rules", "规则列表"),
    ("名单库管理", "/strategy/watchlist", "名单值"),
    ("特征与阈值", "/strategy/features", "特征编码"),
    ("事件仿真", "/simulate", "事件参数输入"),
    ("AI 智能助手", "/chat", "会话"),
    ("趋势分析", "/analysis", "时间范围"),
    ("审计日志", "/audit", "审计流水"),
    ("系统健康度", "/system/health", "事件吞吐量"),
    ("个人中心", "/profile", "账号信息"),
]

# 路由 → 期望正文关键文本（供主循环按路由取用）
ROUTE_EXPECT: dict[str, str] = {route: text for _name, route, text in PAGES}

# ---------------------------------------------------------------------------
# 「整列全 `-`」检查（本次缺陷的直接回归防线）
#
# 前端读错字段名时，模板里通常有 `fmt(x) || '-'` 之类的兜底 ——
# 于是**整列**都渲染成 `-`，页面不报错、正文够长、挂载正常，冒烟全绿。
# 这一类问题（审计「时间」列、账号管理「失败次数」列）用通用的启发式判断会误伤
# （有些列在合法情况下确实全是 `-`），所以这里只**点名**已知必须非空的列。
#
# 判据：该列存在且有数据行时，不能**每一格**都是 `-` 或空。
# 该角色看不到这张表时（列不存在、无数据行）直接跳过 —— 不算失败。
COLUMN_CHECKS: list[tuple[str, str]] = [
    ("/audit", "时间"),
    ("/profile", "失败次数"),
]


def _column_values_js(header: str) -> str:
    """取某个表头对应的整列数据单元格文本（跨页面所有表格）。"""
    return (
        "(function(col){var out=[];"
        "document.querySelectorAll('table').forEach(function(tb){"
        "var hs=tb.querySelectorAll('thead th'),idx=-1;"
        "hs.forEach(function(h,i){if(h.innerText.trim()===col)idx=i;});"
        "if(idx<0)return;"
        "tb.querySelectorAll('tbody tr').forEach(function(tr){"
        "var tds=tr.querySelectorAll('td');"
        "if(tds.length>idx)out.push(tds[idx].innerText.trim());"
        "});});return out;})(" + json.dumps(header, ensure_ascii=False) + ")"
    )


def _check_columns(cdp) -> list[str]:
    """返回「点名列整列全为空」的问题列表。

    注意：只在**该页确实渲染了这张表**时才判定，因此需要先知道当前在哪一页 ——
    调用方按路由筛选。
    """
    problems = []
    route = cdp.evaluate("location.pathname") or ""
    for want_route, header in COLUMN_CHECKS:
        if not str(route).startswith(want_route):
            continue
        values = cdp.evaluate(_column_values_js(header)) or []
        if not values:
            continue                      # 该角色看不到这张表 → 跳过
        if all(v in ("", "-") for v in values):
            problems.append(
                f"「{header}」列 {len(values)} 行全部为空 —— "
                f"多半是前端读了后端不返回的字段名（静默失效）")
    return problems

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
              "演示账号" in body, _page_health(cdp, body, "/login") or f"正文长度={len(body)}")
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
                # ⚠ 大盘滚屏的「点行展开事件快照」是 v3.1 新增的展示面。
                #   新增面同样必须有断言 —— 否则它就成了下一个「加了但没人验」的东西
                #   （本轮四类静默失效就是这么攒下来的）。这里真点一下再断言面板渲染。
                if role == "admin" and route == "/dashboard":
                    opened = cdp.evaluate(
                        "(function(){var r=document.querySelector('.ticker-row');"
                        "if(!r)return false;r.click();return true;})()")
                    cdp.wait(2.2)
                    snap = cdp.evaluate("document.body.innerText") or ""
                    if not opened:
                        role_errors.append(f"{route} 滚屏没有可点击的事件行（事件数据集为空？）")
                    elif "事件快照" not in snap or "来源单号" not in snap:
                        role_errors.append(f"{route} 点击滚屏行后事件快照面板未渲染")
                errs = _js_errors(cdp.events)
                # ⚠ 必须**正面**确认应用挂载了，不能只检查「没有报错」（v3.1 修）：
                #   前端 dev server 没起时，浏览器显示的是「localhost 拒绝连接」错误页 ——
                #   它既没有 JS 报错、正文也超过 40 字，于是**被判成通过**。
                #   这个漏洞让冒烟自检在最该报警的时候报了绿灯。
                bad = _page_health(cdp, body, route)
                if bad:
                    role_errors.append(f"{route} {bad}")
                # ⚠ 正面断言：页面必须渲染出预期内容（不再是死代码，见 PAGES 的说明）
                want = ROUTE_EXPECT.get(route)
                if want and not bad and want not in body:
                    role_errors.append(f"{route} 未渲染出预期内容「{want}」")
                if errs:
                    role_errors.append(f"{route} JS 报错：{errs[0]}")
                for problem in _check_columns(cdp):
                    role_errors.append(f"{route} {problem}")

            check(f"{role} 的 {len(routes)} 个页面全部渲染且无 JS 报错",
                  not role_errors, "；".join(role_errors[:4]))

        # ---- 403 页 ----
        cdp.call("Page.navigate", url=f"{args.frontend}/403")
        cdp.wait(2.0)
        body = cdp.evaluate("document.body.innerText") or ""
        check("403 无权限页可渲染", "权限" in body,
              _page_health(cdp, body, "/403") or f"正文={body[:60]}")

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


# 浏览器自身的错误页 / 空壳页面的特征词（出现任一即视为「没渲染出应用」）
_DEAD_PAGE_MARKERS = (
    "拒绝连接", "无法访问此页面", "ERR_CONNECTION_REFUSED", "This site can’t be reached",
    "找不到", "404 Not Found", "网页无法正常运作",
)


def _page_health(cdp, body: str, route: str) -> str:
    """页面是否**真的**渲染出了应用？返回问题描述，正常时返回空串。

    三重判断，缺一不可：
      ① 不是浏览器错误页（连接被拒 / DNS 失败…）
      ② SPA 真的挂载了（`#app` 里有子节点）
      ③ 页面不是 403（除非本来就在测 403）
    """
    for marker in _DEAD_PAGE_MARKERS:
        if marker in body:
            return f"页面未加载（出现「{marker}」，前端 dev server 可能没启动）"
    mounted = cdp.evaluate(
        "(function(){var a=document.querySelector('#app');"
        "return a ? a.children.length : -1;})()")
    if mounted is not None and mounted == 0:
        return "SPA 未挂载（#app 为空，可能是路由守卫弹回或运行时异常）"
    if len(body.strip()) < 40:
        return f"正文过短（{len(body)} 字）"
    if "无访问权限" in body and route != "/403":
        return "渲染成了 403 页（角色不该访问该路由，或权限配置有误）"
    return ""


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
