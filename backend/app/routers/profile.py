"""模块 5 路由：用户画像与关联图谱。

对应 SPEC §5.5 API-5.1 / API-5.2。权限：reviewer / strategist / analyst / admin（R-AU-11）。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import PROFILE_ROLES, ok
from ..models import RiskUser
from ..services import step5_graph as graph_svc

router = APIRouter(prefix="/api/v1", tags=["画像与图谱"])


@router.get("/profile/{user_id}")
def get_profile(user_id: str, _: RiskUser = Depends(PROFILE_ROLES),
                db: Session = Depends(get_db)):
    """API-5.1 用户画像（累计画像 + 风险标签 + 历史统计；无画像时降级实时计算，FR-25）。"""
    return ok(graph_svc.get_profile(db, user_id))


@router.get("/graph/{user_id}")
def get_graph(user_id: str, depth: int = 2, _: RiskUser = Depends(PROFILE_ROLES),
              db: Session = Depends(get_db)):
    """API-5.2 关联实体图谱 {center, nodes, edges}（FR-26）。

    深度固定 1 跳（中心用户 + 直接关联实体），并附带该实体下的其他账号作为 2 跳补充；
    节点上限 50，超出按 evidence_count 降序截断（R-PG-03）。
    """
    return ok(graph_svc.get_graph(db, user_id))
