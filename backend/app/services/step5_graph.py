"""模块 5：画像与图谱 —— 风险标签、实体关联网络、1~2 跳图谱。

对应文档：
- SPEC §5.3 risk_entity / risk_entity_relation
- SPEC §5.4 风险标签生成规则
- SPEC §5.5 API-5.1 / API-5.2
- SPEC §5.6 业务规则 R-PG-01 ~ R-PG-07
"""

from datetime import datetime

from sqlalchemy.orm import Session

from ..config import settings
from ..models import RiskEntity, RiskEntityRelation
from ..utils import to_float

# 维度 → 实体前缀 / 关系类型（R-PG-06）
ENTITY_PREFIX = {"user": "USR", "device": "DEV", "ip": "IP",
                 "address": "ADDR", "phone": "PHONE"}
RELATION_TYPE = {"device": "same_device", "ip": "same_ip",
                 "address": "same_address", "phone": "same_phone"}

DIM_FIELDS = [("device", "device_id"), ("ip", "ip"),
              ("address", "receive_id"), ("phone", "phone")]


def entity_key(entity_type: str, value: str) -> str:
    """节点 ID 生成：`{TYPE_PREFIX}-{value}`（R-PG-06）。"""
    return f"{ENTITY_PREFIX.get(entity_type, 'ENT')}-{value}"


# ============================================================================
# 增量更新（R-PG-05）
# ============================================================================
def _upsert_entity(db: Session, entity_type: str, value: str,
                   created_at: datetime) -> RiskEntity:
    eid = entity_key(entity_type, value)
    row = db.query(RiskEntity).filter(RiskEntity.entity_id == eid).first()
    if row is None:
        row = RiskEntity(
            entity_id=eid, entity_type=entity_type, entity_value=str(value),
            risk_tag_count=0, risk_tags=[], first_seen_at=created_at,
        )
        db.add(row)
        db.flush()
    return row


def _bump_relation(db: Session, src: str, dst: str, relation: str,
                   created_at: datetime) -> None:
    rel_id = f"REL-{src}->{dst}"
    row = db.query(RiskEntityRelation).filter(
        RiskEntityRelation.relation_id == rel_id).first()
    if row is None:
        db.add(RiskEntityRelation(
            relation_id=rel_id, source_entity_id=src, target_entity_id=dst,
            relation_type=relation, evidence_count=1, create_time=created_at,
        ))
    else:
        row.evidence_count = int(row.evidence_count or 1) + 1


def touch_event(db: Session, event: dict, features: dict[str, float],
                created_at: datetime) -> None:
    """事件落库时增量更新实体与关系（R-PG-05，与事件落库同批次）。"""
    user_id = event.get("user_id")
    if not user_id:
        return

    user_entity = _upsert_entity(db, "user", user_id, created_at)
    tags = derive_risk_tags(features)
    user_entity.risk_tags = tags
    user_entity.risk_tag_count = len(tags)

    for dim, field in DIM_FIELDS:
        value = event.get(field)
        if not value:
            continue
        other = _upsert_entity(db, dim, str(value), created_at)
        _bump_relation(db, user_entity.entity_id, other.entity_id,
                       RELATION_TYPE[dim], created_at)
        _bump_relation(db, other.entity_id, user_entity.entity_id,
                       RELATION_TYPE[dim], created_at)


def derive_risk_tags(features: dict[str, float]) -> list[str]:
    """风险标签生成规则（SPEC §5.4）。"""
    tags: list[str] = []
    if to_float(features.get("user_refund_rate")) > 0.5:
        tags.append("高频退款")
    if to_float(features.get("user_postsale_rate")) > 0.5:
        tags.append("售后滥用")
    if to_float(features.get("device_account_cnt_24h")) > 5:
        tags.append("设备聚集")
    if to_float(features.get("ip_account_cnt_24h")) > 8:
        tags.append("IP 聚集")
    if to_float(features.get("addr_account_cnt_7d")) > 3:
        tags.append("地址聚集")
    if to_float(features.get("user_complaint_count")) >= 5:
        tags.append("投诉频繁")
    if to_float(features.get("user_total_orders")) <= 2:
        tags.append("新账号")
    return tags


# ============================================================================
# API-5.2 关联实体图谱
# ============================================================================
def _entities_by_id(db: Session, ids: set[str]) -> dict[str, RiskEntity]:
    """一次性按 ID 批量取实体（避免逐节点各查一次）。"""
    if not ids:
        return {}
    rows = db.query(RiskEntity).filter(RiskEntity.entity_id.in_(list(ids))).all()
    return {r.entity_id: r for r in rows}


def _relations_from(db: Session, source_ids: set[str]) -> list[RiskEntityRelation]:
    """一次性取出一批源实体的全部出边。"""
    if not source_ids:
        return []
    return (db.query(RiskEntityRelation)
            .filter(RiskEntityRelation.source_entity_id.in_(list(source_ids))).all())


def get_graph(db: Session, user_id: str) -> dict:
    """中心用户 → 直接关联实体（1 跳）→ 该实体下的其他账号（2 跳）。

    节点上限 50，超出按 evidence_count 降序截断（R-PG-03）。

    ⚠ **必须批量取数**（R-PG-08）。1 跳 + 2 跳天然是嵌套遍历
    （中心 → 关联实体 → 该实体下的其他账号），朴素写法会对每个节点各发一条
    `SELECT`：节点上限 50 时约 150 条 SQL，而案件详情页**每次打开**都要渲染图谱。
    本版改为 3 次批量查询：出边 → 批量实体 → 批量反向边 → 批量实体。
    """
    center_id = entity_key("user", user_id)
    nodes: dict[str, dict] = {
        center_id: {"id": center_id, "type": "user", "label": user_id, "is_center": True}
    }
    edges: list[dict] = []
    seen_edge: set[tuple] = set()

    def add_edge(src: str, dst: str, relation: str, weight: int) -> None:
        key = (src, dst, relation)
        if key in seen_edge:
            return
        seen_edge.add(key)
        edges.append({"source": src, "target": dst,
                      "relation": relation, "weight": weight})

    # ---- 1 跳：中心用户直接关联的实体 ----
    first_hop = _relations_from(db, {center_id})
    hop_entities = _entities_by_id(db, {r.target_entity_id for r in first_hop})
    candidates: list[tuple[int, RiskEntityRelation, RiskEntity]] = []
    for rel in first_hop:
        target = hop_entities.get(rel.target_entity_id)
        if target is None or target.entity_type == "user":
            continue
        candidates.append((int(rel.evidence_count or 1), rel, target))
    candidates.sort(key=lambda x: x[0], reverse=True)

    # ---- 2 跳：这些实体下的其他账号（一次批量取边 + 一次批量取实体）----
    for weight, rel, target in candidates:
        nodes[target.entity_id] = {
            "id": target.entity_id, "type": target.entity_type,
            "label": target.entity_value, "evidence_count": weight,
        }
        add_edge(center_id, target.entity_id, rel.relation_type, weight)

    hop_ids = {t.entity_id for _, _, t in candidates}
    back_rels = _relations_from(db, hop_ids)
    back_entities = _entities_by_id(db, {b.target_entity_id for b in back_rels})

    second_hop: list[tuple[int, str, str, str, str]] = []
    for b in back_rels:
        other = back_entities.get(b.target_entity_id)
        if other is None or other.entity_type != "user" or other.entity_id == center_id:
            continue
        second_hop.append((int(b.evidence_count or 1), other.entity_id,
                           other.entity_value, b.source_entity_id, b.relation_type))

    second_hop.sort(key=lambda x: x[0], reverse=True)
    for weight, eid, label, parent_id, relation in second_hop:
        if len(nodes) >= settings.MAX_GRAPH_NODES:
            break                                   # R-PG-03 截断
        if eid in nodes:
            continue
        nodes[eid] = {"id": eid, "type": "user", "label": label,
                      "evidence_count": weight}
        add_edge(parent_id, eid, relation, weight)

    return {"center": center_id, "nodes": list(nodes.values()), "edges": edges}


# ============================================================================
# 用户画像（API-5.1）
# ============================================================================
def get_profile(db: Session, user_id: str) -> dict:
    """用户画像聚合（供 AI 工具与画像接口复用）。"""
    from . import step4_case as case_svc

    profile = case_svc.get_user_profile(db, user_id)
    if profile is None:
        return {"user_id": user_id, "has_profile": False, "risk_tags": []}

    # 关联账号数 = 与该用户共享设备的其他账号数（取各设备下的最大值）。
    # ⚠ 同样批量取数（R-PG-08）：本函数在案件详情页与 `/profile/{uid}` 上都会跑，
    #   朴素的三层嵌套查询会随设备数线性放大 SQL 条数。
    center_id = entity_key("user", user_id)
    out_rels = _relations_from(db, {center_id})
    out_entities = _entities_by_id(db, {r.target_entity_id for r in out_rels})
    device_ids = {r.target_entity_id for r in out_rels
                  if (e := out_entities.get(r.target_entity_id)) is not None
                  and e.entity_type == "device"}

    back_rels = _relations_from(db, device_ids)
    per_device: dict[str, set[str]] = {}
    for b in back_rels:
        if b.relation_type != "same_device":
            continue
        per_device.setdefault(b.source_entity_id, set()).add(b.target_entity_id)

    user_ids = {uid for uids in per_device.values() for uid in uids}
    user_entities = _entities_by_id(db, user_ids)
    related = 0
    for uids in per_device.values():
        users = {user_entities[u].entity_value
                 for u in uids if u in user_entities
                 and user_entities[u].entity_type == "user"}
        related = max(related, len(users))

    profile["related_account_cnt"] = related
    return profile
