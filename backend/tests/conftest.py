"""pytest 测试基础设施（SPEC §13.2）。

- session 级：建测试库 `ecs_test` → 建表 → 灌最小数据集 → teardown 删库
- function 级：每用例独立 Session，teardown 时 rollback + close
- client：覆盖 `get_db` 依赖的 FastAPI TestClient

⚠ 测试库与演示库（`ecs`）完全隔离，不会污染演示数据。
"""

import os
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import RiskRule, RiskRuleScene, RiskUser  # noqa: E402
from app.services.step0_auth import hash_password  # noqa: E402
from app.utils import now_dt  # noqa: E402

TEST_DB = settings.TEST_DB_NAME


# ---------------------------------------------------------------- 测试库
@pytest.fixture(scope="session")
def test_engine():
    """session 级：建库 → 建表 → 灌最小数据集；结束时删库。"""
    server_engine = create_engine(settings.server_url(), future=True, pool_pre_ping=True)
    with server_engine.begin() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS `{TEST_DB}`"))
        conn.execute(text(f"CREATE DATABASE `{TEST_DB}` "
                          f"CHARACTER SET {settings.DB_CHARSET} COLLATE utf8mb4_0900_ai_ci"))
    server_engine.dispose()

    engine = create_engine(settings.database_url(TEST_DB), future=True, pool_pre_ping=True)
    Base.metadata.create_all(engine)

    # 审计触发器（与生产一致）
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TRIGGER trg_audit_no_update BEFORE UPDATE ON risk_audit_log
            FOR EACH ROW BEGIN
                SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '审计日志不可修改';
            END
        """))
        conn.execute(text("""
            CREATE TRIGGER trg_audit_no_delete BEFORE DELETE ON risk_audit_log
            FOR EACH ROW BEGIN
                SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '审计日志不可删除';
            END
        """))

    _seed_minimal(engine)

    yield engine

    engine.dispose()
    drop_engine = create_engine(settings.server_url(), future=True, pool_pre_ping=True)
    with drop_engine.begin() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS `{TEST_DB}`"))
    drop_engine.dispose()


def _seed_minimal(engine) -> None:
    """灌入测试数据集：**与生产完全一致的场景与 30 条规则** + 5 个演示账号。

    仿真模板（TPL-01~04）的预期决策依赖真实规则集，因此测试库必须镜像生产规则，
    不能只用一条占位规则。
    """
    from app.config import DEMO_USERS
    from scripts.init_db import RULES, SCENES, _action_of

    Session = sessionmaker(bind=engine, future=True)
    now = now_dt()
    with Session() as db:
        # 场景（7 条：六类事件 + all）
        for code, name, biz in SCENES:
            db.add(RiskRuleScene(
                scene_code=code, scene_name=name, biz_event=biz,
                pass_threshold=30, mark_threshold=60, reject_threshold=80,
                multi_rule_bonus=3, veto_min_score=90, enabled=1,
            ))

        # 30 条生产规则（含 6 条 challengeable=0）
        for code, name, cat, evt, cond, level, score, prio, ch, desc in RULES:
            db.add(RiskRule(
                rule_id=code, rule_name=name, rule_category=cat, event_type=evt,
                rule_condition=cond, risk_level=level, risk_score=score,
                action=_action_of(level), challengeable=ch, priority=prio,
                is_enabled=1, version=1, description=desc,
                updated_by="test", create_time=now, update_time=now,
            ))

        # 5 个演示账号
        for u in DEMO_USERS:
            db.add(RiskUser(
                user_id=u["user_id"], username=u["username"],
                password_hash=hash_password(u["password"]),
                display_name=u["display_name"], role=u["role"],
                status="active", fail_count=0, create_time=now,
            ))
        db.commit()


@pytest.fixture(scope="function")
def db_session(test_engine):
    """function 级：每用例独立 Session，结束时回滚。"""
    Session = sessionmaker(bind=test_engine, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture(scope="function")
def client(test_engine, monkeypatch):
    """覆盖 get_db 依赖的 TestClient（依赖不需要真实 HTTP 服务）。"""
    from fastapi.testclient import TestClient

    Session = sessionmaker(bind=test_engine, future=True)

    def _override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    import app.database as database
    import app.main as main
    from app.deps import get_db

    # 让服务层里直接 SessionLocal() 的地方（如 AI 工具）也走测试库
    monkeypatch.setattr(database, "SessionLocal", Session)
    main.app.dependency_overrides[get_db] = _override_get_db
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()


# ---------------------------------------------------------------- 公共数据
@pytest.fixture
def features_basic() -> dict:
    """单元测试用的特征字典（Demo Doc-04 附录 A）。"""
    return {"x": 10.0, "y": 3.0, "a": 60.0, "b": 1.0, "c": 200.0,
            "user_total_orders": 5.0, "order_total_amount": 6500.0}


@pytest.fixture
def auth_headers(client):
    """返回各角色的 Authorization 头。"""
    headers = {}
    for role in ("reviewer", "strategist", "analyst", "admin", "ops"):
        resp = client.post("/api/v1/auth/login",
                           json={"username": role, "password": f"{role}@123"})
        if resp.status_code == 200:
            headers[role] = {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}
    return headers
