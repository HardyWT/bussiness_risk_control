"""数据库引擎与会话（MySQL 8 + SQLAlchemy 2.x）。

对应文档：
- SPEC §0.3   技术栈（MySQL 8.x + PyMySQL）
- SPEC §0.4.3 database.py 职责
- Demo Doc-03 §2.2 连接池参数（pool_pre_ping / pool_size / max_overflow）
- SPEC R-MA-08 事件落库 5 张表同一事务提交
"""

from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

# ---------------------------------------------------------------- 引擎
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,                    # 每次取连接前 ping，防止 MySQL wait_timeout 断连
    pool_size=settings.DB_POOL_SIZE,       # 常驻连接 10
    max_overflow=settings.DB_MAX_OVERFLOW, # 峰值可到 30
    pool_recycle=3600,                     # 1 小时回收，避免被服务端踢掉
    echo=False,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


class Base(DeclarativeBase):
    """所有 ORM 模型的公共基类（SQLAlchemy 2.x 声明式）。"""


def get_db():
    """FastAPI 依赖：请求级会话，请求结束自动释放连接。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope():
    """脚本与后台任务用的会话上下文（自动提交 / 回滚）。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---------------------------------------------------------------- 建表与触发器
def create_all() -> None:
    """建全部表（业务表 + 风控表）并创建审计不可篡改触发器。"""
    from . import models  # noqa: F401  确保模型已注册到 Base.metadata

    Base.metadata.create_all(engine)
    create_audit_triggers()


def create_audit_triggers() -> None:
    """审计日志不可篡改保护（SPEC §6.3 / R-MA-03）。

    MySQL 用 SIGNAL SQLSTATE '45000' 抛错（SQLite 用 RAISE(ABORT, ...)）。
    触发器不存在才创建，避免重复执行报错。
    """
    statements = [
        ("trg_audit_no_update", """
            CREATE TRIGGER trg_audit_no_update BEFORE UPDATE ON risk_audit_log
            FOR EACH ROW
            BEGIN
                SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '审计日志不可修改';
            END
        """),
        ("trg_audit_no_delete", """
            CREATE TRIGGER trg_audit_no_delete BEFORE DELETE ON risk_audit_log
            FOR EACH ROW
            BEGIN
                SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '审计日志不可删除';
            END
        """),
    ]
    with engine.begin() as conn:
        for name, ddl in statements:
            exists = conn.execute(
                text("SELECT COUNT(*) FROM information_schema.TRIGGERS "
                     "WHERE TRIGGER_SCHEMA = DATABASE() AND TRIGGER_NAME = :n"),
                {"n": name},
            ).scalar()
            if not exists:
                conn.execute(text(ddl))


def drop_all() -> None:
    """删除全部表（数据重置用；先删触发器）。"""
    from . import models  # noqa: F401

    with engine.begin() as conn:
        for name in ("trg_audit_no_update", "trg_audit_no_delete"):
            conn.execute(text(f"DROP TRIGGER IF EXISTS {name}"))
    Base.metadata.drop_all(engine)


# ---------------------------------------------------------------- 库级操作
def ensure_database(db_name: str | None = None) -> None:
    """确保数据库存在（CREATE DATABASE IF NOT EXISTS）。"""
    name = db_name or settings.DB_NAME
    server_engine = create_engine(settings.server_url(), future=True, pool_pre_ping=True)
    try:
        with server_engine.begin() as conn:
            conn.execute(text(
                f"CREATE DATABASE IF NOT EXISTS `{name}` "
                f"CHARACTER SET {settings.DB_CHARSET} COLLATE utf8mb4_0900_ai_ci"
            ))
    finally:
        server_engine.dispose()


def ping() -> tuple[bool, str]:
    """连通性探测，返回 (是否连通, 描述)。"""
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT VERSION()")).scalar()
        return True, f"已连接 {settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME} · MySQL {version}"
    except Exception as exc:  # noqa: BLE001
        return False, f"连接失败：{exc}"


# ---------------------------------------------------------------- 测试引擎
def get_test_engine(db_name: str | None = None):
    """测试库引擎（SPEC §13.2：session 级建/毁 ecs_test）。"""
    name = db_name or settings.TEST_DB_NAME
    ensure_database(name)
    return create_engine(
        settings.database_url(name),
        pool_pre_ping=True,
        future=True,
    )
