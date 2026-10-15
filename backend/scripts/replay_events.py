"""replay_events.py —— 按 event_time 顺序回放模拟事件（SPEC §10.4）。

让大盘有真实统计数据。支持 `--limit` 控制回放条数。

两种模式：
  （默认）进程内回放 —— 直接走决策流水线写库，**无需先启动服务**，适合一键初始化
  --http            通过 HTTP 调用 POST /api/v1/risk/check（需服务已启动）

用法：
    python scripts/replay_events.py                     # 全量回放
    python scripts/replay_events.py --limit 200         # 只回放 200 条
    python scripts/replay_events.py --http --url http://127.0.0.1:8000
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.database import create_all, session_scope  # noqa: E402
from app.schemas import RiskCheckRequest  # noqa: E402
from app.services import step1_event as event_svc  # noqa: E402

# Windows 控制台默认 GBK，打印 ✓ / ✗ / ⚠ 会抛 UnicodeEncodeError。
# 统一把标准输出强制为 UTF-8（与 run.py / start.py 一致）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


MOCK_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "mock_events.json")


def load_events(limit: int | None) -> list[dict]:
    if not os.path.exists(MOCK_FILE):
        print(f"✗ 未找到数据集：{MOCK_FILE}")
        print("  请先执行：python scripts/gen_mock_events.py")
        sys.exit(1)

    with open(MOCK_FILE, encoding="utf-8") as fh:
        events = json.load(fh)
    events.sort(key=lambda e: e["event_time"])       # 按事件时间升序（SPEC §10.4）
    return events[:limit] if limit else events


def _to_request(raw: dict) -> RiskCheckRequest:
    return RiskCheckRequest(
        event_type=raw["event_type"],
        source_id=raw.get("source_id") or f"RPL-{raw['event_id']}",
        user_id=raw["user_id"],
        device_id=raw.get("device_id"),
        ip=raw.get("ip"),
        phone=raw.get("phone"),
        receive_id=raw.get("receive_id"),
        event_time=raw.get("event_time"),
        event_data=raw.get("event_data"),
        dry_run=False,
    )


def replay_in_process(events: list[dict]) -> Counter:
    """进程内回放：直接调用决策流水线。"""
    create_all()
    decisions: Counter = Counter()
    failed = 0

    with session_scope() as db:
        for i, raw in enumerate(events, start=1):
            try:
                request = _to_request(raw)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                if failed <= 3:
                    print(f"  ⚠ 跳过非法事件 {raw.get('event_id')}：{exc}")
                continue

            try:
                result = event_svc.process_event(db, request, dry_run=False)
                decisions[result["decision"]] += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                if failed <= 3:
                    print(f"  ⚠ 回放失败 {raw.get('event_id')}：{exc}")

            if i % 200 == 0:
                print(f"  ...已回放 {i}/{len(events)}")

        challenge_stats = _resolve_challenges(db)

    if failed:
        print(f"  ⚠ 跳过 {failed} 条非法/失败事件")
    if challenge_stats:
        print(f"  二次验证：通过 {challenge_stats['pass']} 条（→Pass 放行）· "
              f"失败 {challenge_stats['fail']} 条（→Review 人审）· "
              f"未响应 {challenge_stats['ignored']} 条（留给超时兜底 →  Review）")
    return decisions


# 二次验证结果的模拟比例（固定，保证可复现）
CHALLENGE_PASS_RATIO = 0.35          # 通过 → 决策升级 Pass，不建案
CHALLENGE_FAIL_RATIO = 0.45          # 失败 → 决策升级 Review，建案
# 余下 20% 不回调，留给 R-RE-15 的超时兜底扫描处理


def _resolve_challenges(db) -> dict:
    """为回放中产生的二次验证**模拟真实回调**（R-RE-14 的两条分支）。

    ⚠ 为什么不能全都留给超时兜底：
      回放的是历史事件，事件发生时的二次验证早已超窗（没人会响应 30 天前的验证码），
      于是所有 Challenge 都会被超时兜底按「超时按失败处理」升级为 Review。
      结果就是**演示数据集里永远看不到「验证通过 → 放行」这条分支**，
      R-RE-14 的两条路径只被覆盖了一半。

      这里按固定比例显式走一遍真实回调路径：
        通过 → 决策升级 Pass，不建案；失败 → 升级 Review 并建案；
        剩下 20% 故意不回调，用来验证超时兜底链路（`main.py` 的后台扫描）。
    """
    from app.models import RiskChallengeRecord
    from app.services import step3_challenge as challenge_svc

    stats = {"pass": 0, "fail": 0, "ignored": 0}
    rows = (db.query(RiskChallengeRecord)
            .filter(RiskChallengeRecord.status == "pending")
            .order_by(RiskChallengeRecord.issued_at).all())

    for idx, row in enumerate(rows):
        bucket = idx % 100
        if bucket < CHALLENGE_PASS_RATIO * 100:
            result, key = "pass", "pass"
        elif bucket < (CHALLENGE_PASS_RATIO + CHALLENGE_FAIL_RATIO) * 100:
            result, key = "fail", "fail"
        else:
            stats["ignored"] += 1
            continue

        # ⚠ 必须显式传 issued_at：回放时 challenge 的 issued_at 是**事件时间**，
        #   而 `resolve_challenge` 用 `is_expired()` 判断是否超窗。
        #   历史事件必然超窗 → 传 pass 也会被降级成 timeout。
        #   这里把 issued_at 视为「回放当刻下发」，以模拟**当时**的回调。
        from app.utils import now_dt
        row.issued_at = now_dt()
        db.flush()

        data, err = challenge_svc.resolve_challenge(db, row.event_id, result, "replay")
        if err is None:
            stats[key] += 1
            # 回填评估的决策，供调用方统计（resolve_challenge 已改库）
            db.flush()
    return stats


def replay_http(events: list[dict], url: str) -> Counter:
    """HTTP 回放：调用 POST /api/v1/risk/check（服务标识鉴权）。"""
    endpoint = url.rstrip("/") + "/api/v1/risk/check"
    decisions: Counter = Counter()
    failed = 0

    for i, raw in enumerate(events, start=1):
        body = json.dumps({k: v for k, v in raw.items() if k != "event_id"}).encode("utf-8")
        request = urllib.request.Request(
            endpoint, data=body,
            headers={"Content-Type": "application/json",
                     "X-Service-Id": settings.SERVICE_ID},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            decisions[data["data"]["decision"]] += 1
        except (urllib.error.URLError, KeyError, ValueError) as exc:
            failed += 1
            if failed == 1:
                print(f"  ⚠ 首次失败：{exc}")
        if i % 200 == 0:
            print(f"  ...已回放 {i}/{len(events)}")

    if failed:
        print(f"  ⚠ 失败 {failed} 条（请确认服务已启动且可访问 {url}）")
    return decisions


def main() -> None:
    parser = argparse.ArgumentParser(description="ERCS 模拟事件回放")
    parser.add_argument("--limit", type=int, default=None, help="回放条数上限")
    parser.add_argument("--http", action="store_true", help="通过 HTTP 调用 /risk/check")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="服务地址（--http 时生效）")
    args = parser.parse_args()

    events = load_events(args.limit)
    print(f"\n开始回放 {len(events)} 条事件"
          f"（{'HTTP' if args.http else '进程内'}模式）...\n")

    decisions = (replay_http(events, args.url) if args.http
                 else replay_in_process(events))

    total = sum(decisions.values())
    print("\n" + "=" * 62)
    print("  回放完成 —— 决策分布（决策当刻的五档仲裁结果）")
    print("=" * 62)
    for key in ("Pass", "Mark", "Challenge", "Review", "Reject"):
        n = decisions.get(key, 0)
        ratio = f"{n / total * 100:5.1f}%" if total else "  -  "
        print(f"  {key:<12}{n:>6}  {ratio}")
    print("-" * 62)
    print(f"  {'合计':<12}{total:>6}")
    print("=" * 62)
    if total:
        reject_rate = decisions.get("Reject", 0) / total * 100
        high_rate = (decisions.get("Reject", 0) + decisions.get("Review", 0)) / total * 100
        print(f"  拦截率(Reject) = {reject_rate:.2f}%")
        print(f"  高风险占比(Review+Reject) = {high_rate:.2f}%")

    _print_final_state(int(decisions.get("Challenge", 0)))
    print()


def _print_final_state(challenge_at_decision: int) -> None:
    """打印**当前库内**的决策状态分布，并解释它为何与上面不同。

    ⚠ 为什么必须打印两遍（v3.1）：
      `risk_assessment.decision` 是**当前状态**，不是「决策当刻的结论」。
      按 R-RE-14 / R-RE-15，二次验证通过 → 升级 Pass，失败或**超时** → 升级 Review。
      回放的是历史事件，事件发生时的二次验证早已超窗（没人会去响应一份 30 天前的验证码），
      因此 Challenge 会被超时兜底扫描按「超时按失败处理」升级为 Review。

      这不是缺陷，但**只看一个数字会让人以为 Challenge 那一档从来没被触发过**。
      两行并排展示，才能同时回答「仲裁命中了几档」与「现在各是多少」。
    """
    try:
        from sqlalchemy import func, text

        from app.database import session_scope
        from app.models import RiskAssessment
    except Exception as exc:  # noqa: BLE001  仅打印，失败不影响回放
        print(f"\n  （最终状态统计不可用：{exc}）")
        return

    with session_scope() as db:
        rows = (db.query(RiskAssessment.decision, func.count(RiskAssessment.assessment_id))
                .group_by(RiskAssessment.decision).all())
        now_dist = {d: int(c) for d, c in rows}
        pending = db.execute(text(
            "SELECT COUNT(*) FROM risk_challenge_record WHERE status='pending'")).scalar() or 0

    grand = sum(now_dist.values())
    print("\n" + "-" * 62)
    print("  最终状态分布（risk_assessment.decision 的当前值）")
    print("-" * 62)
    for key in ("Pass", "Mark", "Challenge", "Review", "Reject"):
        n = now_dist.get(key, 0)
        ratio = f"{n / grand * 100:5.1f}%" if grand else "  -  "
        print(f"  {key:<12}{n:>6}  {ratio}")
    print("-" * 62)
    print(f"  {'合计':<12}{grand:>6}")
    if challenge_at_decision:
        print(f"  说明：决策当刻有 {challenge_at_decision} 条落入 Challenge。"
              "二次验证是**瞬态**档位 ——")
        print("        通过 → 升级 Pass（不建案）；失败/超时 → 升级 Review（建案）。")
        print("        因此它的「最终状态」只会剩下尚未到期的那些，回放的历史数据基本都已到期。")
        print("        要在大盘上看到 Challenge，请跑仿真页的边界样本或用刚发生的事件发起验证。")
    if pending:
        print(f"  ⚠ 仍有 {pending} 条 pending 挑战（issued_at 在 120s 窗口内）。")
        print("     后端启动后由超时兜底扫描（每 30s）按「超时按失败处理」升级为 Review。")


if __name__ == "__main__":
    main()
