"""模块 11：认证与授权 —— 密码哈希、JWT 签发校验、登录防爆破。

命名统一 step0_ 前缀：认证发生在所有业务步骤（step1~step6）之前（SPEC §11）。
对应规则：R-AU-01 ~ R-AU-04、R-AU-10。
"""

import logging
from datetime import timedelta

from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from ..config import settings
from ..models import RiskUser
from ..utils import now_dt

log = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ---------------------------------------------------------------- 密码
def hash_password(raw: str) -> str:
    """bcrypt 加盐哈希（R-AU-01：绝不存明文）。"""
    return pwd_context.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(raw, hashed)
    except ValueError:
        return False


# ---------------------------------------------------------------- JWT
def create_token(user: RiskUser) -> tuple[str, int]:
    """签发 JWT，返回 (token, expires_in 秒)。载荷见 SPEC §11.3。"""
    expires_in = settings.JWT_EXPIRE_SECONDS
    now = now_dt()
    payload = {
        "sub": user.user_id,
        "username": user.username,
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, expires_in


def decode_token(token: str) -> dict | None:
    """解析并校验 JWT；无效/过期返回 None（R-AU-07 → 4001）。"""
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None


# ---------------------------------------------------------------- 登录
def lock_expired(user: RiskUser, now=None) -> bool:
    """锁定是否已过 `LOGIN_LOCK_SECONDS`（R-AU-04）。

    `locked_at IS NULL` 视为已过期 —— 兼容字段上线前就被锁定的历史数据，
    否则那些账号永远解不开。
    """
    if user.locked_at is None:
        return True
    now = now or now_dt()
    elapsed = (now - user.locked_at).total_seconds()
    return elapsed >= settings.LOGIN_LOCK_SECONDS


def _apply_lock(user: RiskUser, now=None) -> None:
    """把账号置为锁定并记录锁定时刻。"""
    user.status = "locked"
    user.locked_at = now or now_dt()


def _clear_lock(user: RiskUser) -> None:
    """解除锁定（登录成功 / 锁定过期）。"""
    user.status = "active"
    user.fail_count = 0
    user.locked_at = None


def step0_authenticate(db: Session, username: str, password: str) -> tuple[RiskUser | None, str | None]:
    """账号密码校验，返回 (user, error_code)。

    - (user, None)    成功
    - (None, "4001")  账号或密码错误（不区分账号不存在与密码错误，FR-66）
    - (None, "4002")  账号已锁定（R-AU-03：不进行密码比对，避免时序侧信道）

    ⚠ **校验顺序：先判锁定是否过期，再判是否锁定**（R-AU-04）。
    v3.0 只判 `status == 'locked'` 就返回 4002，`LOCKED` 是永久状态 ——
    提示写着「请 10 分钟后重试」，但 10 分钟后重试仍然是 4002，
    `LOGIN_LOCK_SECONDS` 成了死配置。对演示环境的杀伤尤其大：
    误输 5 次密码后，这个演示账号在整场演示里都废了。
    """
    user = db.query(RiskUser).filter(RiskUser.username == username).first()
    if user is None:
        return None, "4001"

    if user.status == "locked":
        if not lock_expired(user):
            return None, "4002"          # 锁定生效中：不比对密码（防时序侧信道）
        # 锁定已过期 → 自动解锁，继续走正常比对
        _clear_lock(user)
        db.commit()
        log.info("账号 %s 的登录锁定已过期（%d 秒），自动解锁",
                 username, settings.LOGIN_LOCK_SECONDS)

    if not verify_password(password, user.password_hash):
        # R-AU-04：连续失败达上限即锁定，并记录锁定时刻
        user.fail_count = int(user.fail_count or 0) + 1
        if user.fail_count >= settings.LOGIN_MAX_FAIL:
            _apply_lock(user)
        db.commit()
        return None, "4001"

    return user, None


def step0_on_login_success(db: Session, user: RiskUser) -> None:
    """登录成功：更新 last_login_at、归零失败计数并清除锁定（R-AU-02）。"""
    user.last_login_at = now_dt()
    _clear_lock(user)
    db.commit()


def unlock_user(db: Session, user: RiskUser) -> None:
    """手动解锁（管理员重置脚本使用）。"""
    _clear_lock(user)
    db.commit()


def change_password(db: Session, user: RiskUser, old_password: str, new_password: str) -> tuple[bool, str | None]:
    """修改密码（个人中心，PRD §2.2）。

    旧密码错误同样累加 fail_count，走 R-AU-04 防爆破（同样记录 `locked_at`）。
    """
    if not verify_password(old_password, user.password_hash):
        user.fail_count = int(user.fail_count or 0) + 1
        if user.fail_count >= settings.LOGIN_MAX_FAIL:
            _apply_lock(user)
        db.commit()
        return False, "4001"
    user.password_hash = hash_password(new_password)
    _clear_lock(user)
    db.commit()
    return True, None


def user_to_dict(user: RiskUser) -> dict:
    """用户信息出参（永不含 password_hash，R-AU-01）。

    ⚠ `last_login_at` 为空时必须返回 `None`，**不能**用当前时间顶替 ——
    个人中心与登录页会把它显示成「上次登录」，从未登录过的账号会显示成
    「刚刚登录过」，属于凭空造数据。

    ⚠ `fail_count` 必须带上：账号管理页有「失败次数」列（配合「连续失败 5 次锁定」的
      说明文案，用来判断离锁定还差几次）。早期这里没返回该键，前端读恒得 `undefined`
      而显示成 `-`，页面看起来正常但那一列永远是空的。
      该键本来就不是敏感信息 —— `GET /auth/users/{id}/lock-state` 早已对外返回它。
    """
    return {
        "user_id": user.user_id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "status": user.status,
        "fail_count": user.fail_count,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }
