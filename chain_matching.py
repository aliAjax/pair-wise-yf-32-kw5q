"""配对链匹配判断（纯逻辑，不访问数据库，与链记录、协调台分开维护）。

协调员一次提交 2-4 组「器官 → 患者」，本模块逐组核对：
血型兼容、器官一致、跨医院交换、器官在确认截止前仍然有效；
并做整链校验：每家参与医院恰好转出一例、接收一例（成组交换闭环）。
任何一组不通过都抛出 ChainValidationError，调用方不得建立链记录。
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

MIN_GROUPS = 2
MAX_GROUPS = 4
VALID_BLOOD = {"O", "A", "B", "AB"}


class ChainValidationError(Exception):
    """携带逐组校验错误：errors = {"groups": {组序号: {"code", "message"}}, "chain": [...]}"""

    def __init__(self, errors: dict[str, Any]):
        super().__init__("配对链校验未通过")
        self.errors = errors


def _blood_compatible(donor_blood: str, candidate_blood: str) -> bool:
    # 延迟导入避免与 app 模块形成加载期循环依赖
    from app import blood_compatible

    return blood_compatible(donor_blood, candidate_blood)


def _expires_at(donor: Any) -> datetime:
    from app import parse_time

    value = donor["expires_at"]
    return value if isinstance(value, datetime) else parse_time(value)


def validate_legs(
    groups: list[tuple[int, int]],
    donors: dict[int, Any],
    candidates: dict[int, Any],
    *,
    now: datetime,
    deadline: datetime,
) -> list[dict[str, Any]]:
    """校验全部组，全部通过时返回按链路顺序排列的 leg dict 列表。"""
    errors: dict[int, dict[str, str]] = {}
    legs: list[dict[str, Any]] = []
    seen_donors: set[int] = set()
    seen_candidates: set[int] = set()

    def fail(index: int, code: str, message: str) -> None:
        errors.setdefault(index, {"code": code, "message": message})

    for index, (donor_id, candidate_id) in enumerate(groups):
        donor, candidate = donors.get(donor_id), candidates.get(candidate_id)
        if donor is None:
            fail(index, "donor_not_found", f"第 {index + 1} 组器官不存在")
            continue
        if candidate is None:
            fail(index, "candidate_not_found", f"第 {index + 1} 组患者不存在")
            continue

        leg = {"position": index + 1, "donor_id": donor_id, "candidate_id": candidate_id,
               "from_hospital": donor["hospital"], "to_hospital": candidate["hospital"]}
        legs.append(leg)

        if donor["status"] != "available":
            fail(index, "donor_unavailable", f"第 {index + 1} 组器官当前状态为 {donor['status']}，不在可选池")
        if candidate["status"] != "active" or not candidate["willing"]:
            fail(index, "candidate_unavailable", f"第 {index + 1} 组患者当前不可接受分配")
        if donor["organ"] != candidate["organ"]:
            fail(index, "organ_mismatch",
                 f"第 {index + 1} 组器官不一致：{donor['organ']} ≠ {candidate['organ']}")
        if donor["blood_type"] not in VALID_BLOOD or candidate["blood_type"] not in VALID_BLOOD \
                or not _blood_compatible(donor["blood_type"], candidate["blood_type"]):
            fail(index, "blood_mismatch",
                 f"第 {index + 1} 组血型不兼容：{donor['blood_type']} → {candidate['blood_type']}")
        if donor["hospital"] == candidate["hospital"]:
            fail(index, "cross_hospital_required", f"第 {index + 1} 组必须跨医院交换，不能留在本院 {donor['hospital']}")

        # 有效期：建链时未过期，且约定确认截止时间必须早于该器官有效期
        donor_expires = _expires_at(donor)
        if donor_expires <= now:
            fail(index, "organ_expired", f"第 {index + 1} 组器官已超过有效期")
        elif donor_expires <= deadline:
            fail(index, "deadline_after_expiry",
                 f"第 {index + 1} 组确认截止时间必须早于器官有效期 {donor['expires_at']}")

        if donor_id in seen_donors:
            fail(index, "donor_repeated", f"第 {index + 1} 组器官在链中重复出现")
        if candidate_id in seen_candidates:
            fail(index, "candidate_repeated", f"第 {index + 1} 组患者在链中重复出现")
        seen_donors.add(donor_id)
        seen_candidates.add(candidate_id)

    chain_errors: list[dict[str, str]] = []
    if legs and not errors:
        # 成组交换闭环：转出医院与接收医院的多重集合必须一致（每家医院一出一进）
        outgoing = Counter(leg["from_hospital"] for leg in legs)
        incoming = Counter(leg["to_hospital"] for leg in legs)
        if outgoing != incoming:
            chain_errors.append({"code": "hospital_loop_unbalanced",
                                 "message": f"转出医院 {dict(outgoing)} 与接收医院 {dict(incoming)} 不能一一对应，交换不成环"})

    if errors or chain_errors:
        payload: dict[str, Any] = {}
        if errors:
            payload["groups"] = {str(index + 1): item for index, item in sorted(errors.items())}
        if chain_errors:
            payload["chain"] = chain_errors
        raise ChainValidationError(payload)

    return legs
