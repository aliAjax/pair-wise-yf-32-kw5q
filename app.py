#!/usr/bin/env python3
"""Organ allocation and cold-chain coordination service (standard library only)."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from chain_matching import ChainValidationError, MAX_GROUPS, MIN_GROUPS, validate_legs
from chain_store import ChainStore

PORT = 8203
ROLES = {"viewer", "hospital", "coordinator", "allocation_officer", "auditor"}
STATUSES = {"proposed", "accepted", "in_transit", "handed_off", "implanted", "withdrawn", "expired"}
# available 可选；allocated 单笔分配中；locked 配对链待确认；held 断链环节留存；expired 失效；used 已植入
DONOR_STATUSES = {"available", "allocated", "locked", "held", "expired", "used"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, errors: dict[str, Any] | None = None):
        super().__init__(message); self.status, self.code, self.message, self.errors = status, code, message, errors


def utcnow() -> datetime: return datetime.now(timezone.utc)
def iso(value: datetime | None = None) -> str: return (value or utcnow()).replace(microsecond=0).isoformat().replace("+00:00", "Z")
def parse_time(value: str | None) -> datetime:
    if not value: raise ApiError(400, "time_required", "必须提供 ISO 8601 时间")
    try: parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc: raise ApiError(400, "invalid_time", f"时间格式错误: {value}") from exc
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def blood_compatible(donor: str, recipient: str) -> bool:
    return {
        "O": {"O", "A", "B", "AB"}, "A": {"A", "AB"}, "B": {"B", "AB"}, "AB": {"AB"},
    }.get(donor.upper(), set()) and recipient.upper() in {"O", "A", "B", "AB"} and recipient.upper() in {
        "O": {"O", "A", "B", "AB"}, "A": {"A", "AB"}, "B": {"B", "AB"}, "AB": {"AB"},
    }.get(donor.upper(), set())


class Repository:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON"); self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS donors(
            id INTEGER PRIMARY KEY AUTOINCREMENT, blood_type TEXT NOT NULL, organ TEXT NOT NULL, hospital TEXT NOT NULL,
            region TEXT NOT NULL, available_at TEXT NOT NULL, expires_at TEXT NOT NULL, clinical_match INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'available', revision INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candidates(
            id INTEGER PRIMARY KEY AUTOINCREMENT, patient_name TEXT NOT NULL, blood_type TEXT NOT NULL, organ TEXT NOT NULL,
            hospital TEXT NOT NULL, region TEXT NOT NULL, urgency INTEGER NOT NULL, wait_days INTEGER NOT NULL,
            willing INTEGER NOT NULL DEFAULT 1, clinical_match INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
            created_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS allocations(
            id INTEGER PRIMARY KEY AUTOINCREMENT, donor_id INTEGER NOT NULL UNIQUE REFERENCES donors(id), candidate_id INTEGER NOT NULL REFERENCES candidates(id),
            score REAL NOT NULL, status TEXT NOT NULL DEFAULT 'proposed', revision INTEGER NOT NULL DEFAULT 1,
            cold_chain_temp REAL, delayed_minutes INTEGER NOT NULL DEFAULT 0, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, accepted_at TEXT, implanted_at TEXT
        );
        CREATE TABLE IF NOT EXISTS handoffs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, allocation_id INTEGER NOT NULL REFERENCES allocations(id), from_hospital TEXT NOT NULL,
            to_hospital TEXT NOT NULL, cold_chain_temp REAL NOT NULL, status TEXT NOT NULL DEFAULT 'initiated',
            initiated_by TEXT NOT NULL, accepted_by TEXT, initiated_at TEXT NOT NULL, accepted_at TEXT,
            UNIQUE(allocation_id)
        );
        CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, allocation_id INTEGER, donor_id INTEGER, chain_id INTEGER, actor TEXT NOT NULL, role TEXT NOT NULL,
            action TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
        # 既有数据库补齐配对链审计列
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(audit_log)")}
        if "chain_id" not in cols:
            self.conn.execute("ALTER TABLE audit_log ADD COLUMN chain_id INTEGER")

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn; self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK"); raise

    @staticmethod
    def audit(conn: sqlite3.Connection, allocation_id: int | None, donor_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any], chain_id: int | None = None) -> None:
        conn.execute("INSERT INTO audit_log(allocation_id,donor_id,chain_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                     (allocation_id, donor_id, chain_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()))


class OrganAllocationService:
    def __init__(self, path: str | Path):
        self.repo = Repository(path)
        self.chains = ChainStore(self.repo.conn)

    @staticmethod
    def identity(headers: Any) -> tuple[str, str, str]:
        actor, role, hospital = headers.get("X-User-Id", "").strip(), headers.get("X-Role", "").strip(), headers.get("X-Hospital", "").strip()
        if not actor or role not in ROLES: raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效 X-Role")
        if role == "hospital" and not hospital: raise ApiError(401, "hospital_required", "医院角色必须提供 X-Hospital")
        return actor, role, hospital

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None: return dict(row) if row else None

    def register_donor(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"coordinator", "allocation_officer"}: raise ApiError(403, "donor_forbidden", "当前角色不能登记器官")
        required = ("blood_type", "organ", "hospital", "region", "available_at", "expires_at")
        if any(not body.get(k) for k in required): raise ApiError(400, "missing_fields", f"缺少字段: {', '.join(k for k in required if not body.get(k))}")
        blood, organ = str(body["blood_type"]).upper(), str(body["organ"]).lower()
        if blood not in {"O", "A", "B", "AB"}: raise ApiError(400, "invalid_blood_type", "血型必须为 O/A/B/AB")
        available, expires = parse_time(body["available_at"]), parse_time(body["expires_at"])
        if expires <= available: raise ApiError(400, "invalid_window", "可用窗口结束时间必须晚于开始时间")
        with self.repo.tx() as conn:
            cur = conn.execute("""INSERT INTO donors(blood_type,organ,hospital,region,available_at,expires_at,clinical_match,created_by,created_at)
                                  VALUES(?,?,?,?,?,?,?,?,?)""",
                               (blood, organ, body["hospital"], body["region"], iso(available), iso(expires), int(body.get("clinical_match", 0)), actor, iso()))
            donor_id = cur.lastrowid; Repository.audit(conn, None, donor_id, actor, role, "donor_registered", {"organ": organ, "expires_at": iso(expires)})
            return dict(conn.execute("SELECT * FROM donors WHERE id=?", (donor_id,)).fetchone())

    def register_candidate(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"coordinator", "allocation_officer"}: raise ApiError(403, "candidate_forbidden", "当前角色不能登记候选患者")
        required = ("patient_name", "blood_type", "organ", "hospital", "region")
        if any(not body.get(k) for k in required): raise ApiError(400, "missing_fields", "候选患者基础信息不完整")
        blood = str(body["blood_type"]).upper(); urgency = body.get("urgency"); wait_days = body.get("wait_days", 0)
        if blood not in {"O", "A", "B", "AB"} or not isinstance(urgency, int) or not 1 <= urgency <= 5 or not isinstance(wait_days, int) or wait_days < 0:
            raise ApiError(400, "invalid_candidate", "血型、1-5 紧急度和非负等待天数无效")
        with self.repo.tx() as conn:
            cur = conn.execute("""INSERT INTO candidates(patient_name,blood_type,organ,hospital,region,urgency,wait_days,willing,clinical_match,created_by,created_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                               (body["patient_name"], blood, str(body["organ"]).lower(), body["hospital"], body["region"], urgency, wait_days,
                                int(body.get("willing", True)), int(body.get("clinical_match", 0)), actor, iso()))
            return dict(conn.execute("SELECT * FROM candidates WHERE id=?", (cur.lastrowid,)).fetchone())

    @staticmethod
    def _score(donor: sqlite3.Row, candidate: sqlite3.Row) -> dict[str, float]:
        region = 250 if donor["region"] == candidate["region"] else 0
        clinical = min(donor["clinical_match"], candidate["clinical_match"]) * 30
        return {"urgency": candidate["urgency"] * 1000, "waiting": candidate["wait_days"] * 2, "region": region, "clinical": clinical,
                "total": candidate["urgency"] * 1000 + candidate["wait_days"] * 2 + region + clinical}

    def ranking(self, donor_id: int, role: str, hospital: str) -> dict[str, Any]:
        if role not in {"allocation_officer", "auditor"}: raise ApiError(403, "ranking_forbidden", "只有分配员或审计员可以查看完整候选排序")
        with self.repo.tx() as conn:
            donor = conn.execute("SELECT * FROM donors WHERE id=?", (donor_id,)).fetchone()
            if not donor: raise ApiError(404, "donor_not_found", "器官不存在")
            rows = []
            for candidate in conn.execute("SELECT * FROM candidates WHERE organ=? AND status='active' AND willing=1", (donor["organ"],)):
                if blood_compatible(donor["blood_type"], candidate["blood_type"]):
                    item = dict(candidate); item["match"] = self._score(donor, candidate); rows.append(item)
            rows.sort(key=lambda item: (-item["match"]["total"], item["id"]))
            for index, item in enumerate(rows, 1): item["rank"] = index
            return {"donor": dict(donor), "candidates": rows}

    def propose(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "allocation_officer": raise ApiError(403, "allocate_forbidden", "只有分配员可以提出分配")
        donor_id, candidate_id = body.get("donor_id"), body.get("candidate_id")
        if not isinstance(donor_id, int) or not isinstance(candidate_id, int): raise ApiError(400, "ids_required", "donor_id 和 candidate_id 必填")
        with self.repo.tx() as conn:
            donor = conn.execute("SELECT * FROM donors WHERE id=?", (donor_id,)).fetchone(); candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
            if not donor or not candidate: raise ApiError(404, "not_found", "器官或候选患者不存在")
            if donor["status"] != "available": raise ApiError(409, "donor_unavailable", "器官当前不可分配")
            if parse_time(donor["expires_at"]) <= utcnow():
                conn.execute("UPDATE donors SET status='expired',revision=revision+1 WHERE id=?", (donor_id,))
                Repository.audit(conn, None, donor_id, actor, role, "organ_expired", {"candidate_id": candidate_id})
                raise ApiError(409, "organ_expired", "器官可用窗口已结束")
            if candidate["status"] != "active" or not candidate["willing"]: raise ApiError(409, "candidate_unavailable", "候选患者当前不可接受分配")
            if donor["organ"] != candidate["organ"] or not blood_compatible(donor["blood_type"], candidate["blood_type"]):
                raise ApiError(409, "medical_mismatch", "器官类型或血型不匹配")
            if conn.execute("SELECT 1 FROM allocations WHERE donor_id=? AND status NOT IN ('withdrawn','expired')", (donor_id,)).fetchone():
                raise ApiError(409, "already_allocated", "该器官已有有效分配")
            score = self._score(donor, candidate)
            cur = conn.execute("""INSERT INTO allocations(donor_id,candidate_id,score,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?)""",
                               (donor_id, candidate_id, score["total"], actor, iso(), iso()))
            allocation_id = cur.lastrowid
            conn.execute("UPDATE donors SET status='allocated',revision=revision+1 WHERE id=?", (donor_id,))
            Repository.audit(conn, allocation_id, donor_id, actor, role, "allocation_proposed", {"candidate_id": candidate_id, "score": score})
            return self._allocation(conn, allocation_id, role, "")

    def _allocation(self, conn: sqlite3.Connection, allocation_id: int, role: str, hospital: str) -> dict[str, Any]:
        row = conn.execute("""SELECT a.*,d.blood_type donor_blood,d.organ,d.hospital donor_hospital,d.region donor_region,d.available_at,d.expires_at,d.status donor_status,
                                     c.patient_name,c.blood_type candidate_blood,c.hospital candidate_hospital,c.region candidate_region,c.urgency,c.wait_days
                              FROM allocations a JOIN donors d ON d.id=a.donor_id JOIN candidates c ON c.id=a.candidate_id WHERE a.id=?""", (allocation_id,)).fetchone()
        if not row: raise ApiError(404, "allocation_not_found", "分配不存在")
        result = dict(row)
        if role == "hospital" and hospital not in {row["donor_hospital"], row["candidate_hospital"]}:
            raise ApiError(403, "allocation_forbidden", "医院不能查看与本机构无关的分配")
        if role == "hospital" and hospital != row["candidate_hospital"]:
            result["patient_name"] = "***"
        result["handoff"] = self._row(conn.execute("SELECT * FROM handoffs WHERE allocation_id=?", (allocation_id,)).fetchone())
        return result

    def _ensure_active(self, conn: sqlite3.Connection, allocation_id: int, actor: str, role: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM allocations WHERE id=?", (allocation_id,)).fetchone()
        if not row: raise ApiError(404, "allocation_not_found", "分配不存在")
        if row["status"] in {"withdrawn", "expired", "implanted"}: raise ApiError(409, "allocation_closed", "分配已结束")
        donor = conn.execute("SELECT * FROM donors WHERE id=?", (row["donor_id"],)).fetchone()
        if parse_time(donor["expires_at"]) <= utcnow():
            conn.execute("UPDATE allocations SET status='expired',revision=revision+1,updated_at=? WHERE id=?", (iso(), allocation_id))
            conn.execute("UPDATE donors SET status='expired',revision=revision+1 WHERE id=?", (donor["id"],))
            Repository.audit(conn, allocation_id, donor["id"], actor, role, "allocation_expired", {"reason": "organ_window_elapsed"})
            raise ApiError(409, "organ_expired", "器官已经超过可用时间，禁止继续流转")
        return row

    def accept(self, allocation_id: int, actor: str, role: str, hospital: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "hospital": raise ApiError(403, "hospital_required", "只有接收医院可以接受器官")
        expected = body.get("expected_revision")
        if not isinstance(expected, int): raise ApiError(400, "revision_required", "expected_revision 必填")
        with self.repo.tx() as conn:
            row = self._ensure_active(conn, allocation_id, actor, role)
            candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (row["candidate_id"],)).fetchone()
            if candidate["hospital"] != hospital: raise ApiError(403, "wrong_hospital", "只能由候选患者所在医院接受")
            if row["status"] == "accepted": return self._allocation(conn, allocation_id, role, hospital)
            if row["status"] != "proposed": raise ApiError(409, "invalid_transition", "当前状态不能接受")
            if row["revision"] != expected: raise ApiError(409, "revision_conflict", "分配信息已发生变化")
            conn.execute("UPDATE allocations SET status='accepted',accepted_at=?,revision=revision+1,updated_at=? WHERE id=?", (iso(), iso(), allocation_id))
            Repository.audit(conn, allocation_id, row["donor_id"], actor, role, "allocation_accepted", {"hospital": hospital})
            return self._allocation(conn, allocation_id, role, hospital)

    def mark_transit(self, allocation_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "allocation_officer": raise ApiError(403, "transit_forbidden", "只有分配员可以登记转运")
        temp = body.get("cold_chain_temp")
        if not isinstance(temp, (int, float)) or not -2 <= float(temp) <= 8:
            raise ApiError(409, "cold_chain_violation", "冷链温度必须保持在 -2°C 到 8°C")
        with self.repo.tx() as conn:
            row = self._ensure_active(conn, allocation_id, actor, role)
            if row["status"] != "accepted": raise ApiError(409, "invalid_transition", "只有已接受分配可以进入转运")
            conn.execute("UPDATE allocations SET status='in_transit',cold_chain_temp=?,revision=revision+1,updated_at=? WHERE id=?", (float(temp), iso(), allocation_id))
            Repository.audit(conn, allocation_id, row["donor_id"], actor, role, "transfer_started", {"cold_chain_temp": temp})
            return self._allocation(conn, allocation_id, role, "")

    def report_delay(self, allocation_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"allocation_officer", "hospital"}: raise ApiError(403, "delay_forbidden", "当前角色不能上报延误")
        minutes, reason = body.get("delayed_minutes"), str(body.get("reason", "")).strip()
        if not isinstance(minutes, int) or minutes <= 0 or not reason: raise ApiError(400, "invalid_delay", "delayed_minutes 必须为正整数且 reason 必填")
        with self.repo.tx() as conn:
            row = self._ensure_active(conn, allocation_id, actor, role)
            conn.execute("UPDATE allocations SET delayed_minutes=delayed_minutes+?,revision=revision+1,updated_at=? WHERE id=?", (minutes, iso(), allocation_id))
            Repository.audit(conn, allocation_id, row["donor_id"], actor, role, "logistics_delay", {"minutes": minutes, "reason": reason, "at_risk": minutes >= 120})
            return self._allocation(conn, allocation_id, role, "")

    def initiate_handoff(self, allocation_id: int, actor: str, role: str, hospital: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "hospital": raise ApiError(403, "handoff_forbidden", "只有医院可以发起交接")
        expected = body.get("expected_revision"); target = str(body.get("to_hospital", "")).strip(); temp = body.get("cold_chain_temp")
        if not isinstance(expected, int) or not target or not isinstance(temp, (int, float)) or not -2 <= float(temp) <= 8:
            raise ApiError(400, "invalid_handoff", "expected_revision、to_hospital 和合规冷链温度必填")
        with self.repo.tx() as conn:
            row = self._ensure_active(conn, allocation_id, actor, role)
            donor = conn.execute("SELECT * FROM donors WHERE id=?", (row["donor_id"],)).fetchone()
            candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (row["candidate_id"],)).fetchone()
            if hospital != donor["hospital"]: raise ApiError(403, "wrong_hospital", "只能由器官来源医院发起交接")
            if target != candidate["hospital"]: raise ApiError(409, "wrong_destination", "交接目标必须与候选患者医院一致")
            if row["status"] != "in_transit": raise ApiError(409, "invalid_transition", "只有转运中分配可以交接")
            if row["revision"] != expected: raise ApiError(409, "revision_conflict", "分配版本已变化")
            try:
                cur = conn.execute("""INSERT INTO handoffs(allocation_id,from_hospital,to_hospital,cold_chain_temp,initiated_by,initiated_at)
                                      VALUES(?,?,?,?,?,?)""", (allocation_id, hospital, target, float(temp), actor, iso()))
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "handoff_exists", "交接已经登记") from exc
            Repository.audit(conn, allocation_id, row["donor_id"], actor, role, "handoff_initiated", {"target": target, "cold_chain_temp": temp})
            return {"handoff": dict(conn.execute("SELECT * FROM handoffs WHERE id=?", (cur.lastrowid,)).fetchone()), "allocation": self._allocation(conn, allocation_id, role, hospital)}

    def accept_handoff(self, allocation_id: int, actor: str, role: str, hospital: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "hospital": raise ApiError(403, "handoff_forbidden", "只有医院可以确认交接")
        with self.repo.tx() as conn:
            row = self._ensure_active(conn, allocation_id, actor, role)
            handoff = conn.execute("SELECT * FROM handoffs WHERE allocation_id=?", (allocation_id,)).fetchone()
            if not handoff: raise ApiError(409, "handoff_missing", "尚未发起交接")
            if handoff["to_hospital"] != hospital: raise ApiError(403, "wrong_hospital", "只能由接收医院确认交接")
            if handoff["status"] == "accepted": return self._allocation(conn, allocation_id, role, hospital)
            conn.execute("UPDATE handoffs SET status='accepted',accepted_by=?,accepted_at=? WHERE id=?", (actor, iso(), handoff["id"]))
            conn.execute("UPDATE allocations SET status='handed_off',revision=revision+1,updated_at=? WHERE id=?", (iso(), allocation_id))
            Repository.audit(conn, allocation_id, row["donor_id"], actor, role, "handoff_accepted", {"handoff_id": handoff["id"]})
            return self._allocation(conn, allocation_id, role, hospital)

    def implant(self, allocation_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "allocation_officer": raise ApiError(403, "implant_forbidden", "只有分配员可以确认植入")
        with self.repo.tx() as conn:
            row = self._ensure_active(conn, allocation_id, actor, role)
            if row["status"] != "handed_off": raise ApiError(409, "invalid_transition", "交接完成后才能确认植入")
            conn.execute("UPDATE allocations SET status='implanted',implanted_at=?,revision=revision+1,updated_at=? WHERE id=?", (iso(), iso(), allocation_id))
            conn.execute("UPDATE donors SET status='used',revision=revision+1 WHERE id=?", (row["donor_id"],))
            Repository.audit(conn, allocation_id, row["donor_id"], actor, role, "organ_implanted", {"candidate_id": row["candidate_id"]})
            return self._allocation(conn, allocation_id, role, "")

    def withdraw(self, allocation_id: int, actor: str, role: str, hospital: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "hospital": raise ApiError(403, "withdraw_forbidden", "只有医院可以撤回")
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "撤回原因必填")
        with self.repo.tx() as conn:
            row = self._ensure_active(conn, allocation_id, actor, role)
            candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (row["candidate_id"],)).fetchone()
            if hospital != candidate["hospital"]: raise ApiError(403, "wrong_hospital", "只能由候选患者医院撤回")
            if row["status"] not in {"proposed", "accepted"}: raise ApiError(409, "invalid_transition", "转运开始后不能直接撤回")
            conn.execute("UPDATE allocations SET status='withdrawn',revision=revision+1,updated_at=? WHERE id=?", (iso(), allocation_id))
            donor = conn.execute("SELECT * FROM donors WHERE id=?", (row["donor_id"],)).fetchone()
            donor_status = "available" if parse_time(donor["expires_at"]) > utcnow() else "expired"
            conn.execute("UPDATE donors SET status=?,revision=revision+1 WHERE id=?", (donor_status, row["donor_id"]))
            Repository.audit(conn, allocation_id, row["donor_id"], actor, role, "allocation_withdrawn", {"reason": reason})
            return self._allocation(conn, allocation_id, role, hospital)

    # ------------------------------------------------------------------
    # 跨医院成组交换配对链（配对判断在 chain_matching，链记录在 chain_store）
    # ------------------------------------------------------------------

    LEG_REQUIRED = ("donor_id", "candidate_id")

    def _load_chain(self, conn: sqlite3.Connection, chain_id: int) -> sqlite3.Row:
        chain = self.chains.get_chain_tx(conn, chain_id)
        if not chain: raise ApiError(404, "chain_not_found", "配对链不存在")
        return chain

    @staticmethod
    def _chain_mask(leg: dict[str, Any], role: str, hospital: str) -> None:
        # 与单笔分配一致：非患者所在医院只看到掩码
        if role == "hospital" and hospital != leg["candidate_hospital"]:
            leg["patient_name"] = "***"

    def _chain_view(self, conn: sqlite3.Connection, chain: sqlite3.Row, role: str, hospital: str) -> dict[str, Any]:
        result = dict(chain)
        legs = [dict(row) for row in self.chains.legs_tx(conn, chain["id"])]
        for leg in legs:
            self._chain_mask(leg, role, hospital)
            leg["released_to_available"] = chain["status"] == "broken" and leg["status"] in {"pending", "confirmed"} and leg["donor_status"] == "available"
        result["legs"] = legs
        return result

    def create_chain(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "coordinator": raise ApiError(403, "chain_forbidden", "只有协调员可以提交配对链")
        raw_groups = body.get("groups")
        if not isinstance(raw_groups, list) or not MIN_GROUPS <= len(raw_groups) <= MAX_GROUPS:
            raise ApiError(400, "invalid_groups", f"一次必须提交 {MIN_GROUPS}-{MAX_GROUPS} 组器官和患者")
        groups: list[tuple[int, int]] = []
        for index, group in enumerate(raw_groups):
            if not isinstance(group, dict) or not all(isinstance(group.get(k), int) for k in self.LEG_REQUIRED):
                raise ApiError(400, "invalid_groups", f"第 {index + 1} 组必须包含整数 donor_id 和 candidate_id")
            groups.append((group["donor_id"], group["candidate_id"]))
        deadline = parse_time(body.get("deadline"))
        if deadline <= utcnow(): raise ApiError(400, "invalid_deadline", "确认截止时间必须晚于当前时间")
        note = str(body.get("note", "")).strip()

        with self.repo.tx() as conn:
            donor_ids = {g[0] for g in groups}; candidate_ids = {g[1] for g in groups}
            donors = {row["id"]: row for row in conn.execute(
                f"SELECT * FROM donors WHERE id IN ({','.join('?' * len(donor_ids))})", tuple(donor_ids))}
            candidates = {row["id"]: row for row in conn.execute(
                f"SELECT * FROM candidates WHERE id IN ({','.join('?' * len(candidate_ids))})", tuple(candidate_ids))}

            try:
                legs = validate_legs(groups, donors, candidates, now=utcnow(), deadline=deadline)
            except ChainValidationError as exc:
                raise ApiError(409, "chain_validation_failed", "配对链校验未通过，未建立待确认链", exc.errors) from exc

            # 全部对得上：建立待确认链，先锁器官，防止等待确认期间被单笔分配拿走
            now = iso()
            chain_id = self.chains.insert_chain(conn, group_count=len(groups), deadline=iso(deadline),
                                                note=note, created_by=actor, created_at=now)
            for leg in legs:
                self.chains.insert_leg(conn, chain_id=chain_id, position=leg["position"], donor_id=leg["donor_id"],
                                       candidate_id=leg["candidate_id"], from_hospital=leg["from_hospital"],
                                       to_hospital=leg["to_hospital"])
                conn.execute("UPDATE donors SET status='locked',revision=revision+1 WHERE id=? AND status='available'",
                             (leg["donor_id"],))
            Repository.audit(conn, None, None, actor, role, "chain_created",
                             {"group_count": len(groups), "deadline": iso(deadline),
                              "legs": [{"position": l["position"], "donor_id": l["donor_id"],
                                        "candidate_id": l["candidate_id"],
                                        "from_hospital": l["from_hospital"], "to_hospital": l["to_hospital"]}
                                       for l in legs]}, chain_id)
            return self._chain_view(conn, self._load_chain(conn, chain_id), role, "")

    def _sweep_timeouts(self, conn: sqlite3.Connection, chain: sqlite3.Row) -> sqlite3.Row:
        """惰性超时清扫：待确认链超过截止时间，未确认环节按超时断开。"""
        if chain["status"] != "pending" or parse_time(chain["deadline"]) > utcnow():
            return chain
        legs = self.chains.legs_tx(conn, chain["id"])
        pending = next((leg for leg in legs if leg["position"] == min(
            l["position"] for l in legs if l["status"] == "pending")), None)
        return self._break_chain(conn, chain, pending, cause="timed_out", actor="system", role="system", reason="超过约定确认截止时间")

    def _break_chain(self, conn: sqlite3.Connection, chain: sqlite3.Row, failure_leg: sqlite3.Row | None,
                     *, cause: str, actor: str, role: str, reason: str) -> sqlite3.Row:
        """断链：该环节留存（拒绝/超时，器官 held），其余未过期器官恢复可选。"""
        legs = self.chains.legs_tx(conn, chain["id"])
        now = iso()
        if failure_leg is not None:
            status = "rejected" if cause == "rejected" else "timed_out"
            self.chains.update_leg(conn, failure_leg["id"], status=status, responded_by=actor,
                                   responded_at=now, reason=reason)
            # 该环节留下：器官不回池，标记 held 等协调员后续处理
            conn.execute("UPDATE donors SET status='held',revision=revision+1 WHERE id=?",
                         (failure_leg["donor_id"],))
            Repository.audit(conn, None, failure_leg["donor_id"], actor, role,
                             "chain_leg_rejected" if cause == "rejected" else "chain_leg_timed_out",
                             {"position": failure_leg["position"], "reason": reason, "organ_held": True},
                             chain["id"])
        released, held_expired = [], []
        failure_id = failure_leg["id"] if failure_leg is not None else -1
        for leg in legs:
            if leg["id"] == failure_id:
                continue
            # 其余环节（无论此前是否已逐组确认）都未真正落地 allocation：未过期恢复可选
            donor = conn.execute("SELECT * FROM donors WHERE id=?", (leg["donor_id"],)).fetchone()
            if parse_time(donor["expires_at"]) > utcnow():
                conn.execute("UPDATE donors SET status='available',revision=revision+1 WHERE id=?",
                             (leg["donor_id"],))
                released.append(leg["donor_id"])
            else:
                conn.execute("UPDATE donors SET status='expired',revision=revision+1 WHERE id=?",
                             (leg["donor_id"],))
                held_expired.append(leg["donor_id"])
                Repository.audit(conn, None, leg["donor_id"], "system", "system", "organ_expired",
                                 {"chain_position": leg["position"], "reason": "chain_broken_after_expiry"},
                                 chain["id"])
        failure_reason = f"{cause}: {reason}"
        self.chains.mark_chain_broken(conn, chain["id"],
                                      failure_leg=failure_leg["position"] if failure_leg is not None else None,
                                      failure_reason=failure_reason, broken_at=now)
        Repository.audit(conn, None, None, actor, role, "chain_broken",
                         {"cause": cause, "reason": reason,
                          "failure_leg": failure_leg["position"] if failure_leg is not None else None,
                          "donors_released": released, "donors_expired": held_expired, "donors_held":
                              [failure_leg["donor_id"]] if failure_leg is not None else []},
                         chain["id"])
        return self.chains.get_chain_tx(conn, chain["id"])

    def respond_leg(self, chain_id: int, position: int, actor: str, role: str, hospital: str,
                    body: dict[str, Any], *, approve: bool) -> dict[str, Any]:
        if role != "hospital": raise ApiError(403, "hospital_required", "只有接收医院可以确认配对环节")
        reason = str(body.get("reason", "")).strip()
        if not approve and not reason: raise ApiError(400, "reason_required", "拒绝必须填写原因")
        with self.repo.tx() as conn:
            chain = self._sweep_timeouts(conn, self._load_chain(conn, chain_id))
            if chain["status"] != "pending":
                raise ApiError(409, "chain_closed", f"配对链已结束（{chain['status']}），不能再确认")
            leg = self.chains.pending_leg_by_position(conn, chain_id, position)
            if not leg: raise ApiError(404, "leg_not_found", "该环节不存在或已经处理")
            if leg["to_hospital"] != hospital: raise ApiError(403, "wrong_hospital", "只能由该环节的接收医院回应")

            if not approve:
                chain = self._break_chain(conn, chain, leg, cause="rejected", actor=actor, role=role, reason=reason)
                return self._chain_view(conn, chain, role, hospital)

            self.chains.update_leg(conn, leg["id"], status="confirmed", responded_by=actor, responded_at=iso())
            Repository.audit(conn, None, leg["donor_id"], actor, role, "chain_leg_confirmed",
                             {"position": position, "hospital": hospital}, chain_id)

            remaining = [row for row in self.chains.legs_tx(conn, chain_id) if row["status"] == "pending"]
            if remaining:
                return self._chain_view(conn, chain, role, hospital)

            # 全部环节在截止时间前确认：整链落地为既有单笔 allocation（accepted），进入转运流程
            return self._finalize_chain(conn, chain, actor, role, hospital)

    def _finalize_chain(self, conn: sqlite3.Connection, chain: sqlite3.Row, actor: str, role: str,
                        hospital: str) -> dict[str, Any]:
        now = iso(); allocation_ids = []
        legs = self.chains.legs_tx(conn, chain["id"])
        for leg in legs:
            donor = conn.execute("SELECT * FROM donors WHERE id=?", (leg["donor_id"],)).fetchone()
            candidate = conn.execute("SELECT * FROM candidates WHERE id=?", (leg["candidate_id"],)).fetchone()
            score = self._score(donor, candidate)
            cur = conn.execute(
                """INSERT INTO allocations(donor_id,candidate_id,score,status,accepted_at,created_by,created_at,updated_at)
                   VALUES(?,?,?, 'accepted', ?,?,?,?)""",
                (donor["id"], candidate["id"], score["total"], now, f"chain:{chain['id']}", now, now))
            allocation_id = cur.lastrowid; allocation_ids.append(allocation_id)
            conn.execute("UPDATE donors SET status='allocated',revision=revision+1 WHERE id=?", (donor["id"],))
            self.chains.update_leg(conn, leg["id"], allocation_id=allocation_id)
            # 每组 chain_leg_confirmed 已在 respond_leg 逐组记录；这里只补每笔 allocation 的落地痕迹，不重复
            Repository.audit(conn, allocation_id, donor["id"], actor, role, "allocation_proposed",
                             {"candidate_id": candidate["id"], "score": score, "source": "exchange_chain"}, chain["id"])
        self.chains.mark_chain_confirmed(conn, chain["id"])
        Repository.audit(conn, None, None, actor, role, "chain_confirmed",
                         {"allocation_ids": allocation_ids}, chain["id"])
        return self._chain_view(conn, self.chains.get_chain_tx(conn, chain["id"]), role, hospital)

    def release_held_donor(self, donor_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        """协调员处置断链留存环节：把 held 器官重新放回可选池（过期则转 expired）。"""
        if role != "coordinator": raise ApiError(403, "chain_forbidden", "只有协调员可以释放留存环节的器官")
        reason = str(body.get("reason", "")).strip()
        if not reason: raise ApiError(400, "reason_required", "释放原因必填（用于审计追溯）")
        with self.repo.tx() as conn:
            donor = conn.execute("SELECT * FROM donors WHERE id=?", (donor_id,)).fetchone()
            if not donor: raise ApiError(404, "donor_not_found", "器官不存在")
            if donor["status"] != "held": raise ApiError(409, "donor_not_held", "该器官不处于断链留存状态")
            leg = conn.execute("SELECT * FROM exchange_legs WHERE donor_id=? AND status IN ('rejected','timed_out') ORDER BY id DESC LIMIT 1",
                               (donor_id,)).fetchone()
            chain_id = leg["chain_id"] if leg else None
            if parse_time(donor["expires_at"]) <= utcnow():
                new_status = "expired"
            else:
                new_status = "available"
            conn.execute("UPDATE donors SET status=?,revision=revision+1 WHERE id=?", (new_status, donor_id))
            Repository.audit(conn, None, donor_id, actor, role, "held_donor_released",
                             {"new_status": new_status, "reason": reason, "chain_position": leg["position"] if leg else None},
                             chain_id)
            return dict(conn.execute("SELECT * FROM donors WHERE id=?", (donor_id,)).fetchone())

    def get_chain(self, chain_id: int, role: str, hospital: str) -> dict[str, Any]:
        with self.repo.tx() as conn:
            chain = self._sweep_timeouts(conn, self._load_chain(conn, chain_id))
            return self._chain_view(conn, chain, role, hospital)

    def list_chains(self, role: str, hospital: str, status: str | None = None) -> dict[str, Any]:
        with self.repo.tx() as conn:
            # 读侧统一过一遍超时，保证协调台看到的状态已结算
            for row in self.chains.pending_chains_past_deadline(conn, iso()):
                self._sweep_timeouts(conn, row)
            chains = []
            for row in self.chains.list_chains(status=status, hospital=hospital if role == "hospital" else None):
                chains.append(self._chain_view(conn, row, role, hospital))
            return {"chains": chains, "server_time": iso()}

    def chain_audit(self, chain_id: int, role: str, hospital: str) -> dict[str, Any]:
        if role == "viewer": raise ApiError(403, "audit_forbidden", "当前角色不能查看审计记录")
        with self.repo.tx() as conn:
            chain = self._load_chain(conn, chain_id)
            if role == "hospital":
                related = {r["from_hospital"] for r in self.chains.legs_tx(conn, chain_id)} | \
                          {r["to_hospital"] for r in self.chains.legs_tx(conn, chain_id)}
                if hospital not in related: raise ApiError(403, "audit_forbidden", "医院只能查看本机构参与的配对链审计")
            rows = conn.execute(
                "SELECT actor,role,action,detail_json,created_at,donor_id,allocation_id FROM audit_log WHERE chain_id=? ORDER BY id",
                (chain_id,))
            return {"chain_id": chain_id, "status": chain["status"],
                    "audit": [dict(r) for r in rows]}

    def get_allocation(self, allocation_id: int, role: str, hospital: str) -> dict[str, Any]:
        return self._allocation(self.repo.conn, allocation_id, role, hospital)

    def audit(self, allocation_id: int, role: str) -> list[dict[str, Any]]:
        if role not in {"auditor", "allocation_officer"}: raise ApiError(403, "audit_forbidden", "当前角色不能查看审计记录")
        return [dict(r) for r in self.repo.conn.execute("SELECT actor,role,action,detail_json,created_at FROM audit_log WHERE allocation_id=? ORDER BY id", (allocation_id,))]

    def state(self, role: str, hospital: str) -> dict[str, Any]:
        conn = self.repo.conn
        if role == "hospital":
            donors = [dict(r) for r in conn.execute("SELECT * FROM donors WHERE hospital=?", (hospital,))]
            candidates = [dict(r) for r in conn.execute("SELECT * FROM candidates WHERE hospital=?", (hospital,))]
            allocated = [dict(r) for r in conn.execute("SELECT a.* FROM allocations a JOIN candidates c ON c.id=a.candidate_id WHERE c.hospital=?", (hospital,))]
        elif role == "viewer":
            donors = []
            candidates = []
            allocated = [dict(r) for r in conn.execute("SELECT id,status,updated_at FROM allocations WHERE status='implanted' ORDER BY id DESC")]
        else:
            donors = [dict(r) for r in conn.execute("SELECT * FROM donors ORDER BY id DESC")]
            candidates = [dict(r) for r in conn.execute("SELECT * FROM candidates ORDER BY id DESC")]
            allocated = [dict(r) for r in conn.execute("SELECT * FROM allocations ORDER BY id DESC")]
        return {"donors": donors, "candidates": candidates, "allocations": allocated, "server_time": iso()}


def json_reply(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode(); handler.send_response(status); handler.send_header("Content-Type", "application/json; charset=utf-8"); handler.send_header("Content-Length", str(len(raw))); handler.end_headers(); handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    service: OrganAllocationService; web_root: Path
    def log_message(self, fmt: str, *args: Any) -> None: print(f"{self.address_string()} - {fmt % args}")
    def read_body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        if not size: return {}
        try: body = json.loads(self.rfile.read(size))
        except json.JSONDecodeError as exc: raise ApiError(400, "invalid_json", "请求体不是有效 JSON") from exc
        if not isinstance(body, dict): raise ApiError(400, "invalid_json", "请求体必须是对象")
        return body
    def dispatch_get(self, path: str, query: dict[str, list[str]]) -> tuple[int, Any]:
        if path == "/health": return 200, {"status": "ok", "service": "organ-allocation"}
        actor, role, hospital = self.service.identity(self.headers)
        if path == "/api/state": return 200, self.service.state(role, hospital)
        parts = [p for p in path.split("/") if p]
        if len(parts) == 4 and parts[:2] == ["api", "donors"] and parts[2].isdigit() and parts[3] == "ranking": return 200, self.service.ranking(int(parts[2]), role, hospital)
        if len(parts) == 3 and parts[:2] == ["api", "allocations"] and parts[2].isdigit(): return 200, self.service.get_allocation(int(parts[2]), role, hospital)
        if len(parts) == 4 and parts[:2] == ["api", "allocations"] and parts[2].isdigit() and parts[3] == "audit": return 200, {"audit": self.service.audit(int(parts[2]), role)}
        if path == "/api/chains":
            status = query.get("status", [None])[0]
            return 200, self.service.list_chains(role, hospital, status)
        if len(parts) == 3 and parts[:2] == ["api", "chains"] and parts[2].isdigit():
            return 200, self.service.get_chain(int(parts[2]), role, hospital)
        if len(parts) == 4 and parts[:2] == ["api", "chains"] and parts[2].isdigit() and parts[3] == "audit":
            return 200, self.service.chain_audit(int(parts[2]), role, hospital)
        raise ApiError(404, "not_found", "接口不存在")
    def dispatch_post(self, path: str) -> tuple[int, Any]:
        actor, role, hospital = self.service.identity(self.headers); body = self.read_body(); parts = [p for p in path.split("/") if p]
        actions = {
            "/api/donors": lambda: (201, self.service.register_donor(actor, role, body)),
            "/api/candidates": lambda: (201, self.service.register_candidate(actor, role, body)),
            "/api/allocations": lambda: (201, self.service.propose(actor, role, body)),
            "/api/chains": lambda: (201, self.service.create_chain(actor, role, body)),
        }
        if path in actions: return actions[path]()
        # 配对链：接收医院逐组确认 / 拒绝
        if len(parts) == 5 and parts[:2] == ["api", "chains"] and parts[2].isdigit() and parts[3] == "legs" and parts[4].isdigit():
            cid, position = int(parts[2]), int(parts[4])
            return 200, self.service.respond_leg(cid, position, actor, role, hospital, body, approve=True)
        if len(parts) == 6 and parts[:2] == ["api", "chains"] and parts[2].isdigit() and parts[3] == "legs" and parts[4].isdigit():
            cid, position, action = int(parts[2]), int(parts[4]), parts[5]
            if action == "reject":
                return 200, self.service.respond_leg(cid, position, actor, role, hospital, body, approve=False)
        # 断链留存器官：协调员释放回可选池
        if len(parts) == 4 and parts[:2] == ["api", "donors"] and parts[2].isdigit() and parts[3] == "release-held":
            return 200, self.service.release_held_donor(int(parts[2]), actor, role, body)
        if len(parts) == 4 and parts[:2] == ["api", "allocations"] and parts[2].isdigit():
            aid, action = int(parts[2]), parts[3]
            routes = {
                "accept": lambda: self.service.accept(aid, actor, role, hospital, body),
                "withdraw": lambda: self.service.withdraw(aid, actor, role, hospital, body),
                "transit": lambda: self.service.mark_transit(aid, actor, role, body),
                "delay": lambda: self.service.report_delay(aid, actor, role, body),
                "handoff": lambda: self.service.initiate_handoff(aid, actor, role, hospital, body),
                "handoff-accept": lambda: self.service.accept_handoff(aid, actor, role, hospital, body),
                "implant": lambda: self.service.implant(aid, actor, role, body),
            }
            if action in routes: return 200, routes[action]()
        raise ApiError(404, "not_found", "接口不存在")
    def handle_any(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path == "/":
                raw = (self.web_root / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
            status, payload = self.dispatch_get(parsed.path, parse_qs(parsed.query)) if method == "GET" else self.dispatch_post(parsed.path)
            json_reply(self, status, payload)
        except ApiError as exc: json_reply(self, exc.status, {"error": exc.code, "message": exc.message, **({"errors": exc.errors} if exc.errors else {})})
        except Exception as exc: print(f"unhandled error: {exc!r}"); json_reply(self, 500, {"error": "internal_error", "message": str(exc)})
    def do_GET(self) -> None: self.handle_any("GET")
    def do_POST(self) -> None: self.handle_any("POST")


def create_server(db_path: str | Path, host: str = "127.0.0.1", port: int = PORT) -> ThreadingHTTPServer:
    service = OrganAllocationService(db_path); handler = type("OrganHandler", (Handler,), {"service": service, "web_root": Path(__file__).resolve().parent / "static"}); return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=PORT); parser.add_argument("--db", default=os.environ.get("ORGAN_DB", "organ_allocation.db")); args = parser.parse_args()
    server = create_server(args.db, args.host, args.port); print(f"organ-allocation listening on http://{args.host}:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == "__main__": main()
