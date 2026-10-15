"""模块 11 路由：登录 / 当前用户 / 登出 / 用户列表 / 修改密码。

对应 SPEC §11.3 API-11.1 ~ API-11.5、R-AU-09（四类认证事件写审计）。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import ApiError, get_current_user, ok
from ..models import RiskUser
from ..schemas import ChangePassword, LoginRequest
from ..services import step0_auth
from ..services.step6_audit import write_audit

router = APIRouter(prefix="/api/v1/auth", tags=["认证授权"])


@router.post("/login")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """API-11.1 登录，返回 JWT（FR-66）。"""
    user, err = step0_auth.step0_authenticate(db, body.username, body.password)

    if err == "4002":
        write_audit(db, "auth", body.username, "login_locked", body.username,
                    after={"reason": "账号已锁定", "code": "4002"})
        raise ApiError("4002", "账号已锁定，请 10 分钟后重试或联系管理员", 401)

    if err == "4001" or user is None:
        write_audit(db, "auth", body.username, "login_failed", body.username,
                    after={"reason": "账号或密码错误", "code": "4001"})
        # FR-66：不区分账号不存在与密码错误
        raise ApiError("4001", "账号或密码错误", 401)

    step0_auth.step0_on_login_success(db, user)
    token, expires_in = step0_auth.create_token(user)

    write_audit(db, "auth", user.username, "login", user.username, role=user.role,
                after={"reason": "登录成功", "role": user.role})

    return ok({
        "access_token": token,
        "token_type": "bearer",
        "expires_in": expires_in,
        "user": {
            "user_id": user.user_id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
        },
    })


@router.get("/me")
def me(user: RiskUser = Depends(get_current_user)):
    """API-11.2 取当前用户信息与角色（FR-67）。"""
    return ok(step0_auth.user_to_dict(user))


@router.post("/logout")
def logout(user: RiskUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """API-11.3 登出（FR-69）。JWT 无状态，服务端只记审计。"""
    write_audit(db, "auth", user.username, "logout", user.username, role=user.role,
                after={"reason": "退出登录"})
    return ok({"username": user.username, "message": "已登出"})


@router.post("/change-password")
def change_password(body: ChangePassword,
                    user: RiskUser = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    """修改密码（个人中心 FR-69）。旧密码错误累加 fail_count（R-AU-04）。"""
    success, err = step0_auth.change_password(db, user, body.old_password, body.new_password)
    if not success:
        write_audit(db, "auth", user.username, "change_password_failed", user.username,
                    after={"reason": "原密码错误", "code": err})
        raise ApiError(err or "4001", "原密码错误", 400)

    write_audit(db, "auth", user.username, "change_password", user.username,
                after={"reason": "修改密码成功"})
    return ok({"username": user.username, "message": "密码已更新"})


@router.get("/users")
def list_users(current: RiskUser = Depends(get_current_user),
               db: Session = Depends(get_db)):
    """API-11.5 用户列表（仅 admin，R-AU-11）。"""
    if current.role != "admin":
        write_audit(db, "auth", current.username, "403", current.username,
                    role=current.role,
                    after={"reason": "越权访问用户列表", "code": "4003"})
        raise ApiError("4003", "权限不足：仅管理员可查看用户列表", 403)

    rows = db.query(RiskUser).order_by(RiskUser.user_id).all()
    return ok([step0_auth.user_to_dict(u) for u in rows])
