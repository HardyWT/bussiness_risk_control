"""依赖注入：统一响应包装、JWT 解析、五角色校验。

对应文档：
- SPEC §11.4 R-AU-05（除 login 与 risk/check 外所有接口必须挂 get_current_user）
- SPEC §11.4 R-AU-09（登录 / 登出 / 越权拒绝都要写审计）
- SPEC §11.4 R-AU-11（接口允许角色映射表，与 PRD §2.1.1 三级权限矩阵一致）
"""

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .database import get_db
from .models import RiskUser
from .services import step0_auth
from .services.step6_audit import write_audit


class ApiError(HTTPException):
    """统一错误：{code, message, data=null}。"""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(status_code=status_code, detail={"code": code, "message": message})


def ok(data=None, message: str = "success") -> dict:
    return {"code": 0, "message": message, "data": data}


# ================================================================ 当前用户
def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> RiskUser:
    """解析 Bearer Token；失败一律 401 + code 4001（R-AU-05 / R-AU-07）。"""
    if not authorization:
        raise ApiError("4001", "未认证：缺少 Authorization 头", 401)

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ApiError("4001", "未认证：Authorization 格式应为 Bearer <token>", 401)

    payload = step0_auth.decode_token(token)
    if payload is None:
        raise ApiError("4001", "Token 无效或已过期", 401)

    user = db.query(RiskUser).filter(RiskUser.username == payload.get("username")).first()
    if user is None:
        raise ApiError("4001", "用户不存在", 401)
    if user.status == "locked":
        raise ApiError("4002", "账号已锁定", 401)
    return user


def require_role(*roles: str):
    """角色校验依赖工厂（R-AU-11）。越权返回 403 + code 4003 并写审计（R-AU-09）。"""

    def _checker(user: RiskUser = Depends(get_current_user),
                 db: Session = Depends(get_db)) -> RiskUser:
        if user.role not in roles:
            write_audit(db, biz_type="auth", biz_id=user.username, action="403",
                        operator=user.username, role=user.role,
                        after={"reason": "越权访问", "required_roles": list(roles),
                               "actual_role": user.role, "code": "4003"})
            raise ApiError("4003",
                           f"权限不足：需要角色 {list(roles)}，当前为 {user.role}", 403)
        return user

    return _checker


# ================================================================ 角色依赖
# 允许角色映射与 PRD §2.1.1 / SPEC R-AU-11 严格一致。
#
# 角色：admin 管理员 / reviewer 审核员 / strategist 策略师 / analyst 分析师 / ops 运维

ALL_ROLES = require_role("reviewer", "strategist", "analyst", "admin", "ops")

# 大盘：全部角色可见
DASHBOARD_ROLES = ALL_ROLES

# 案件：GET 允许 reviewer / admin（admin 只读）；写操作仅 reviewer
CASE_READ_ROLES = require_role("reviewer", "admin")
CASE_WRITE_ROLES = require_role("reviewer")

# 规则 / 名单 / 仿真：读放开到 4 角色（analyst 只读、ops 不可见）
RULE_READ_ROLES = require_role("reviewer", "strategist", "analyst", "admin")
# ⚠ SPEC R-AU-11：/rules/** 与 /blacklist/** 的**写操作仅 strategist**，admin 为只读
#    （对应 PRD §2.1.1 权限矩阵中 admin 对这两项标 👁 只读）
STRATEGY_WRITE_ROLES = require_role("strategist")
# 兼容旧命名：本项目中「策略类写操作」统一用上面这个
STRATEGY_ROLES = STRATEGY_WRITE_ROLES
# 仿真：strategist + admin 均可（SPEC R-AU-11）
SIMULATE_ROLES = require_role("strategist", "admin")

# 阈值配置：仅 admin（PRD §2.1.1）
ADMIN_ONLY = require_role("admin")

# 健康度：admin / ops
SYSTEM_ROLES = require_role("admin", "ops")

# 分析师专属能力（导出分析报告 / 保存视图）：analyst 为主，策略师与管理员也需要
# （策略师要交规则效果报告、管理员要全量数据），审核员与运维不涉及数据交付。
ANALYST_ROLES = require_role("analyst", "strategist", "admin")

# AI Agent：strategist / analyst / admin
AGENT_ROLES = require_role("strategist", "analyst", "admin")

# 审计：全部可读（数据范围由服务层按角色过滤；analyst 无权 → 单独排除）
AUDIT_ROLES = require_role("reviewer", "strategist", "admin", "ops")

# 画像与图谱：reviewer / strategist / analyst / admin
PROFILE_ROLES = require_role("reviewer", "strategist", "analyst", "admin")
