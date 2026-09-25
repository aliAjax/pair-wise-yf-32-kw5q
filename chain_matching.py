"""配对判断模块：校验配对链每一组的血型、器官、医院与有效期。

只包含纯函数，不接触数据库。链记录见 chain_store.py，协调台见 app.py 与 static/index.html。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

BLOOD_COMPATIBILITY = {
    "O": {"O", "A", "B", "AB"},
    "A": {"A", "AB"},
    "B": {"B", "AB"},
    "AB": {"AB"},
}

MIN_LEGS = 2
MAX_LEGS = 4


def blood_compatible(donor: str, recipient: str) -> bool:
    """供体血型能否输给受者。"""
    return recipient.upper() in BLOOD_COMPATIBILITY.get(donor.upper(), set())


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def validate_leg(donor: dict[str, Any], candidate: dict[str, Any], deadline: datetime, now: datetime) -> list[str]:
    """校验单组器官与患者，返回问题列表（空列表表示通过）。"""
    problems: list[str] = []
    if not str(donor.get("hospital", "")).strip() or not str(candidate.get("hospital", "")).strip():
        problems.append("医院信息不完整")
    if donor.get("organ") != candidate.get("organ"):
        problems.append(f"器官类型不一致: {donor.get('organ')} != {candidate.get('organ')}")
    if not blood_compatible(str(donor.get("blood_type", "")), str(candidate.get("blood_type", ""))):
        problems.append(f"血型不兼容: 供体 {donor.get('blood_type')} -> 患者 {candidate.get('blood_type')}")
    if donor.get("status") != "available":
        problems.append("器官当前不可选")
    elif _parse(donor["expires_at"]) <= now:
        problems.append("器官已过有效期")
    elif deadline > _parse(donor["expires_at"]):
        problems.append("确认截止时间晚于器官有效期")
    if candidate.get("status") != "active" or not candidate.get("willing"):
        problems.append("患者当前不可接受配对")
    return problems


def validate_chain(legs: list[dict[str, Any]], deadline: datetime, now: datetime) -> list[dict[str, Any]]:
    """校验整条配对链，legs 为 [{"donor": ..., "candidate": ...}]，返回按环节分组的问题（空列表表示全部对得上）。"""
    issues: list[dict[str, Any]] = []
    if not MIN_LEGS <= len(legs) <= MAX_LEGS:
        issues.append({"leg": None, "problems": [f"配对链需要 {MIN_LEGS}-{MAX_LEGS} 组，当前 {len(legs)} 组"]})
    if deadline <= now:
        issues.append({"leg": None, "problems": ["确认截止时间必须晚于当前时间"]})
    seen_donors: dict[int, int] = {}
    seen_candidates: dict[int, int] = {}
    for seq, leg in enumerate(legs, 1):
        problems = validate_leg(leg["donor"], leg["candidate"], deadline, now)
        donor_id, candidate_id = leg["donor"].get("id"), leg["candidate"].get("id")
        if donor_id in seen_donors:
            problems.append(f"器官与第 {seen_donors[donor_id]} 组重复")
        if candidate_id in seen_candidates:
            problems.append(f"患者与第 {seen_candidates[candidate_id]} 组重复")
        seen_donors[donor_id] = seq
        seen_candidates[candidate_id] = seq
        if problems:
            issues.append({"leg": seq, "problems": problems})
    return issues
