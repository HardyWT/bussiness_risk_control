"""进程内接口调用计数器（SPEC §6.5 R-MA-12）。

用途：给 `/api/v1/system/health` 的「接口健康度」提供**真实**数据。

⚠ 为什么需要这个模块：v3.0 的该接口返回的是写死的示例常量
（`{"calls": 8240, "errors": 12, ...}`），页面看起来正常但**从不变化**。
运维按它排障会被误导 —— 假指标比缺失功能更危险（SPEC R-MA-12）。

设计取舍：
- **进程内累加**，不落库、不上报外部系统（PRD §1.4 范围外已排除 Prometheus 等）。
- 单进程演示环境，多 worker 部署时各进程各算一份 —— 演示场景可接受，
  若要多 worker 汇总应改为共享存储（记入 SPEC §13.5 已知限制）。
- 按「HTTP 方法 + 路由模板」聚合，避免把 `/cases/cas01abc` 与 `/cases/cas02def`
  当成两个接口统计。
- 只保留**有上界的**键（路由模板有限），不会随请求量无界增长。
"""

import threading
from dataclasses import dataclass

# 只统计这些前缀的业务接口（排在 /docs、/openapi.json、静态资源之外）
COUNTED_PREFIXES = ("/api/",)
# 计数不参与健康度展示的噪声路径
IGNORED_PATHS = ("/api/v1/admin/health",)


@dataclass
class _Bucket:
    """单个「方法 + 路由模板」的累计量。"""

    calls: int = 0
    errors: int = 0
    total_ms: float = 0.0

    def as_dict(self) -> dict:
        avg = round(self.total_ms / self.calls, 2) if self.calls else 0.0
        return {
            "calls": self.calls,
            "errors": self.errors,
            "error_rate": round(self.errors / self.calls, 6) if self.calls else 0.0,
            "avg_latency_ms": avg,
        }


_lock = threading.Lock()
_buckets: dict[str, _Bucket] = {}


def should_count(path: str) -> bool:
    """是否纳入统计（排除文档页、探针与静态资源）。"""
    if not path.startswith(COUNTED_PREFIXES):
        return False
    return not any(path.startswith(p) for p in IGNORED_PATHS)


def record(key: str, status_code: int, elapsed_ms: float) -> None:
    """累加一次调用。`key` 形如 `POST /api/v1/risk/check`。

    `status_code >= 500` 记为错误 —— 与「业务上返回 4xx 是正常分支」的约定一致：
    4xx（401 / 403 / 404 / 422）由权限与校验逻辑主动抛出，不算系统故障。
    """
    with _lock:
        bucket = _buckets.get(key)
        if bucket is None:
            bucket = _Bucket()
            _buckets[key] = bucket
        bucket.calls += 1
        bucket.total_ms += elapsed_ms
        if status_code >= 500:
            bucket.errors += 1


def snapshot() -> dict:
    """全部接口的健康度快照（按调用数降序）。

    返回项的 `path` 形如 `POST /api/v1/risk/check`（含方法前缀），
    与 SPEC §6.4 API-6.10 的 `api_health[].path` 契约一致。
    """
    with _lock:
        items = [{"path": key, **bucket.as_dict()}
                 for key, bucket in _buckets.items()]
    items.sort(key=lambda x: x["calls"], reverse=True)
    return {
        "api_health": items,
        "total_calls": sum(i["calls"] for i in items),
        "total_errors": sum(i["errors"] for i in items),
    }


def reset() -> None:
    """清空计数（测试用；也让 `init_db --reset` 后页面回到干净状态）。"""
    with _lock:
        _buckets.clear()


def make_key(method: str, route_template: str | None, path: str) -> str:
    """生成聚合键。

    优先用 FastAPI 解析出的**路由模板**（`/api/v1/cases/{case_id}`），
    未匹配到路由（404）时退回原始路径，并按「去掉最后一段」粗粒度归并，
    避免用扫描器打出的随机路径把计数器撑爆。
    """
    target = route_template or path
    if route_template is None:
        parts = [p for p in path.split("/") if p]
        if len(parts) > 2:
            target = "/" + "/".join(parts[:2]) + "/…"
    return f"{method.upper()} {target}"
