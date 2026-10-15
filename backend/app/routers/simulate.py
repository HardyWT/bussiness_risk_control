"""模块 7 路由：事件仿真测试。

对应 SPEC §7.4 API-7.1 ~ API-7.3、§7.5 内置 4 个用例模板、§7.6 R-SM-01 ~ R-SM-07。
⚠ 仿真执行**不写任何业务表**（R-SM-01），但走**完整决策链路**（R-SM-02）。
"""

from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import SIMULATE_ROLES, ApiError, ok
from ..engine import step2_feature as fe
from ..models import RiskUser
from ..services import step1_event as event_svc
from ..utils import now_dt, to_iso

router = APIRouter(prefix="/api/v1", tags=["仿真测试"])


def _ts(minutes_ago: int) -> str:
    """把「相对当前时刻的分钟偏移」换算成 ISO8601 事件时间。

    ⚠ 必须在**每次回放时**调用，不能提前算好写进模板常量。
    种子事件要落在 1h / 24h / 7d 的滑动窗口内，而窗口是相对「回放当刻」算的：

    | 写法 | 后果 |
    |------|------|
    | ❌ 模块导入时算好常量 | 服务运行越久，种子越旧。运行约 95 分钟后 TPL-01 的 1h 种子全部滑出窗口，`device_coupon_cnt_1h` 从 16 掉到 1，判定由 **Mark 漂移为 Pass**，页面报「与预期不一致」 |
    | ✅ 回放时现算 | 无论服务运行多久，种子与目标事件的相对时间恒定，结果可复现（SPEC R-SM-07 / G-11） |

    因此模板里存的是**分钟偏移量**（`seed_offsets`），不是绝对时间。
    """
    return to_iso(now_dt() - timedelta(minutes=minutes_ago))


# ================================================================ 用例模板（SPEC §7.5）
# seed = 用于构造滑动窗口上下文的合成历史事件（不落库）
TEMPLATES: list[dict] = [
    {
        "template_code": "TPL-01",
        "name": "羊毛党批量领券",
        "desc": "同设备 4 账号，1h 领券 15 次（未触发同设备聚集）",
        "event_type": "coupon_receive",
        "expect": "Mark",
        "request": {
            "event_type": "coupon_receive", "source_id": "CPN-SIM-9007",
            "user_id": "U-SIM-B07", "device_id": "DEV-SIM-FARM1",
            "ip": "172.16.88.31", "phone": "13800009007", "event_data": {"coupon_value": 500},
        },
        # 目标用户 1h 内领券 12 次 + 同设备另 3 个账号各 1 次
        # → device_coupon_cnt_1h = 15 > 10 命中 R-008(+45)
        # → device_account_cnt_24h = 4 ≤ 5 不触发 R-005（否则会被一票否决）
        "seed": (
            [{"event_type": "coupon_receive", "user_id": f"U-SIM-B{i:02d}",
              "device_id": "DEV-SIM-FARM1", "ip": f"172.16.88.{30 + i}",
              "phone": f"138000091{i:02d}", "source_id": f"CPN-SIM-91{i:02d}",
              "event_time": {"offset_min": 50}} for i in range(1, 4)]
            + [{"event_type": "coupon_receive", "user_id": "U-SIM-B07",
                "device_id": "DEV-SIM-FARM1", "ip": "172.16.88.31",
                "phone": "13800009007", "source_id": f"CPN-SIM-82{j}",
                "event_time": {"offset_min": j * 3 + 2}} for j in range(12)]
        ),
    },
    {
        "template_code": "TPL-02",
        "name": "恶意退款欺诈",
        "desc": "同地址 9 账号，同手机号退款率 0.75",
        "event_type": "after_sale_apply",
        "expect": "Review",
        "request": {
            "event_type": "after_sale_apply", "source_id": "PS-SIM-88003",
            "user_id": "U-SIM-R03", "device_id": "DEV-SIM-R03",
            # ⚠ 必须带 receive_id：R-006 依赖 addr_account_cnt_7d，
            #    目标事件缺该字段会算出 0，规则静默不命中
            "ip": "10.30.11.20", "receive_id": "RCV-XS-01",
            "phone": "13900008003", "amount": 3200,
        },
        # 9 个账号共享地址（addr_account_cnt_7d = 9 > 3）
        # 同手机号 8 单下单 + 6 单退款（+1 目标 = 7）→ phone_refund_rate_24h ≈ 0.875 > 0.7
        # → R-006 地址团伙退款(70, 不可挑战) 命中 → 中风险档直接 Review
        "seed": (
            [{"event_type": "order_create", "user_id": f"U-SIM-R{i:02d}",
              "device_id": f"DEV-SIM-R{i:02d}", "ip": "10.30.11.20",
              "receive_id": "RCV-XS-01", "phone": "13900008003",
              "source_id": f"O-SIM-88{i:03d}", "event_time": {"offset_min": 400 + i}}
             for i in range(1, 10)]
            + [{"event_type": "after_sale_apply", "user_id": "U-SIM-R03",
                "device_id": "DEV-SIM-R03", "ip": "10.30.11.20",
                "receive_id": "RCV-XS-01", "phone": "13900008003",
                "source_id": f"PS-SIM-8810{j}", "event_time": {"offset_min": 120 + j}}
               for j in range(6)]
        ),
    },
    {
        "template_code": "TPL-03",
        "name": "黄牛囤货",
        "desc": "同设备 6 账号，1h 下单 7 次，金额 8800",
        "event_type": "order_create",
        "expect": "Reject",
        "request": {
            "event_type": "order_create", "source_id": "O-SIM-77001",
            "user_id": "U-SIM-S01", "device_id": "DEV-SIM-SCAL1",
            "ip": "10.44.20.11", "receive_id": "RCV-HZ-07",
            "phone": "13700007001", "amount": 8800,
        },
        # 6 个账号共用设备：device_account_cnt_24h = 6 > 5 → R-005(90, 极高, 不可挑战) 命中
        # dev_order_cnt_1h = 6+1 = 7 > 5 → R-007(75, 不可挑战)
        # 极高规则触发一票否决 → 最终分 ≥ 90 → Reject
        "seed": (
            [{"event_type": "order_create", "user_id": f"U-SIM-S{i:02d}",
              "device_id": "DEV-SIM-SCAL1", "ip": f"10.44.20.{10 + i}",
              "receive_id": f"RCV-HZ-0{i}", "phone": f"1370000700{i}",
              "source_id": f"O-SIM-77{i:03d}", "event_time": {"offset_min": 50 + i}}
             for i in range(2, 7)]
            + [{"event_type": "order_create", "user_id": "U-SIM-S01",
                "device_id": "DEV-SIM-SCAL1", "ip": "10.44.20.11",
                "receive_id": "RCV-HZ-07", "phone": "13700007001",
                "source_id": "O-SIM-78001", "event_time": {"offset_min": 6}}]
        ),
    },
    {
        "template_code": "TPL-04",
        "name": "正常用户对照",
        "desc": "各项特征均正常",
        "event_type": "order_create",
        "expect": "Pass",
        "request": {
            "event_type": "order_create", "source_id": "O-SIM-66001",
            "user_id": "U-SIM-N01", "device_id": "DEV-SIM-N01",
            "ip": "192.168.20.31", "receive_id": "RCV-NB-99",
            "phone": "13600006001", "amount": 299,
        },
        "seed": [],
    },
]

_TEMPLATE_INDEX = {t["template_code"]: t for t in TEMPLATES}

_DIMENSION_FIELDS = (("device", "device_id"), ("ip", "ip"),
                     ("address", "receive_id"), ("phone", "phone"), ("user", "user_id"))


# ================================================================ 模板沙箱（R-SM-07）
def _touched_keys(template: dict) -> dict[str, list[str]]:
    keys: dict[str, set[str]] = {dim: set() for dim, _ in _DIMENSION_FIELDS}
    for raw in [template["request"], *template.get("seed", [])]:
        for dim, field in _DIMENSION_FIELDS:
            value = raw.get(field)
            if value:
                keys[dim].add(str(value))
    return {dim: sorted(values) for dim, values in keys.items()}


def _resolve_seed(template: dict) -> list[dict]:
    """把种子事件里的 `{"offset_min": N}` 现算成 ISO8601 事件时间。

    R-SM-07 / G-11：种子必须以**回放当刻**为基准，才能在服务运行任意时长后
    仍落在 1h / 24h / 7d 窗口内，保证同一模板反复回放结果一致。
    """
    resolved: list[dict] = []
    for item in template.get("seed", []):
        row = dict(item)
        raw_time = row.get("event_time")
        if isinstance(raw_time, dict) and "offset_min" in raw_time:
            row["event_time"] = _ts(raw_time["offset_min"])
        resolved.append(row)
    return resolved


def _seed_context(template: dict) -> None:
    """清理模板涉及的实体键后灌入种子数据，保证**反复回放结果一致**（R-SM-07）。"""
    fe.purge(_touched_keys(template))
    for item in _resolve_seed(template):
        fe.record_event(item)


# ================================================================ API-7.2 模板列表
@router.get("/simulate/templates")
def list_templates(_: RiskUser = Depends(SIMULATE_ROLES)):
    """API-7.2 用例模板列表（FR-51）。"""
    return ok([{
        "template_code": t["template_code"], "name": t["name"], "desc": t["desc"],
        "event_type": t["event_type"], "expect": t["expect"], "request": t["request"],
    } for t in TEMPLATES])


# ================================================================ 执行仿真
def _run(body, db: Session) -> dict:
    """执行一次仿真并组装四步判定链路（SPEC §7.4）。"""
    from ..schemas import RiskCheckRequest

    try:
        request = body if isinstance(body, RiskCheckRequest) else RiskCheckRequest(**body)
    except Exception as exc:  # noqa: BLE001
        return _failed_steps(str(exc))

    missing = request.validate_conditional()
    if missing:
        return _failed_steps(f"缺少必填字段：{missing}")

    try:
        payload = event_svc.process_event(db, request, dry_run=True)
    except Exception as exc:  # noqa: BLE001
        return _failed_steps(f"仿真执行失败：{exc}")

    features = payload.get("features") or {}

    # ② 特征按 4 维度分组
    dimensions: dict[str, dict] = {}
    for key, value in features.items():
        dim = fe.DIMENSION_OF.get(key, "其他")
        dimensions.setdefault(dim, {})[key] = value

    steps = [
        {"step": 1, "name": "事件接收", "status": "ok",
         "detail": f"格式校验通过，事件类型 {payload['event_type']}，"
                   f"事件编号 {payload['event_id']}，参数补全完成"},
        {"step": 2, "name": "特征提取", "status": "ok",
         "detail": {"dimensions": dimensions, "total": len(features)}},
        {"step": 3, "name": "规则命中", "status": "ok",
         "detail": payload.get("triggered_rules", [])},
        {"step": 4, "name": "综合决策", "status": "ok", "detail": {
            "final_score": payload["final_score"],
            "risk_level": payload["risk_level"],
            "decision": payload["decision"],
            "is_veto": payload["is_veto"],
            "rule_count": payload["rule_count"],
            "hit_source": payload.get("hit_source"),
            "thresholds": payload.get("thresholds", {}),
            "threshold_text": "0-29 Pass / 30-59 Mark / 60-79 Challenge|Review / 80-100 Reject",
        }},
    ]

    if payload.get("challenge"):
        steps[3]["challenge"] = payload["challenge"]
        steps[3]["detail"]["challenge_required"] = True

    return {"steps": steps, "raw_response": payload}


def _failed_steps(message: str) -> dict:
    """校验失败时后续步骤标 skipped（R-SM-06）。"""
    return {
        "steps": [
            {"step": 1, "name": "事件接收", "status": "failed", "detail": message},
            {"step": 2, "name": "特征提取", "status": "skipped", "detail": None},
            {"step": 3, "name": "规则命中", "status": "skipped", "detail": None},
            {"step": 4, "name": "综合决策", "status": "skipped", "detail": None},
        ],
        "raw_response": {"code": 1002, "message": message},
    }


@router.post("/simulate/run")
def run_simulate(body: dict, _: RiskUser = Depends(SIMULATE_ROLES),
                 db: Session = Depends(get_db)):
    """API-7.1 执行仿真检测（不入库、不影响真实统计，R-SM-01）。

    ⚠ 这里刻意用 `body: dict` 而不是 `SimulateRun` / `RiskCheckRequest`：
    若交给 FastAPI 做 Pydantic 校验，参数非法时会直接返回 422，
    仿真页就拿不到「① 事件接收失败 → ②③④ skipped」的四步结构，
    只能笼统提示「请求失败」。所以先收原始 dict，在 `_run()` 里用
    `RiskCheckRequest` 校验并把错误渲染成链路第一步的红框（R-SM-06）。
    """
    return ok(_run(body, db))


@router.post("/simulate/run_template/{template_code}")
def run_template(template_code: str, _: RiskUser = Depends(SIMULATE_ROLES),
                 db: Session = Depends(get_db)):
    """API-7.3 载入模板并回放（FR-51）。"""
    tpl = _TEMPLATE_INDEX.get(template_code)
    if tpl is None:
        raise ApiError("3001", f"模板不存在：{template_code}", 404)

    _seed_context(tpl)
    result = _run(tpl["request"], db)
    observed = (result.get("raw_response") or {}).get("decision")
    result["template"] = {
        "template_code": tpl["template_code"],
        "name": tpl["name"],
        "desc": tpl["desc"],
        "expect": tpl["expect"],
        "observed": observed,
        "matched": observed == tpl["expect"],
    }
    return ok(result)
