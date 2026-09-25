"""配对链记录存储（与配对判断、协调台 API 分开维护）。

只负责 exchange_chains / exchange_legs 两张表的 DDL 与 SQL 读写，
业务规则（退回、超时、落地 allocation）由 OrganAllocationService 编排。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable

CHAIN_STATUSES = {"pending", "confirmed", "broken"}
LEG_STATUSES = {"pending", "confirmed", "rejected", "timed_out"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS exchange_chains(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_count INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    deadline TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    broken_at TEXT,
    failure_leg INTEGER,
    failure_reason TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exchange_legs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id INTEGER NOT NULL REFERENCES exchange_chains(id),
    position INTEGER NOT NULL,
    donor_id INTEGER NOT NULL REFERENCES donors(id),
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    from_hospital TEXT NOT NULL,
    to_hospital TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    allocation_id INTEGER REFERENCES allocations(id),
    responded_by TEXT,
    responded_at TEXT,
    reason TEXT NOT NULL DEFAULT '',
    UNIQUE(chain_id, position)
);
"""

# 连表查询 leg 时带出的展示字段
LEG_SELECT = """SELECT l.*, d.blood_type donor_blood, d.organ, d.expires_at,
                       d.hospital donor_hospital, d.status donor_status,
                       c.patient_name, c.blood_type candidate_blood, c.hospital candidate_hospital
                FROM exchange_legs l
                JOIN donors d ON d.id=l.donor_id
                JOIN candidates c ON c.id=l.candidate_id"""


class ChainStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        conn.executescript(_SCHEMA)

    # ---- 写 ----
    def insert_chain(self, conn: sqlite3.Connection, *, group_count: int, deadline: str, note: str,
                     created_by: str, created_at: str) -> int:
        cur = conn.execute(
            """INSERT INTO exchange_chains(group_count,status,deadline,note,created_by,created_at)
               VALUES(?, 'pending', ?, ?, ?, ?)""",
            (group_count, deadline, note, created_by, created_at))
        return int(cur.lastrowid)

    def insert_leg(self, conn: sqlite3.Connection, *, chain_id: int, position: int, donor_id: int,
                   candidate_id: int, from_hospital: str, to_hospital: str) -> int:
        cur = conn.execute(
            """INSERT INTO exchange_legs(chain_id,position,donor_id,candidate_id,from_hospital,to_hospital)
               VALUES(?,?,?,?,?,?)""",
            (chain_id, position, donor_id, candidate_id, from_hospital, to_hospital))
        return int(cur.lastrowid)

    def update_leg(self, conn: sqlite3.Connection, leg_id: int, **fields: Any) -> None:
        if not fields:
            return
        clause = ", ".join(f"{key}=?" for key in fields)
        conn.execute(f"UPDATE exchange_legs SET {clause} WHERE id=?", (*fields.values(), leg_id))

    def mark_chain_broken(self, conn: sqlite3.Connection, chain_id: int, *, failure_leg: int,
                          failure_reason: str, broken_at: str) -> None:
        conn.execute(
            """UPDATE exchange_chains SET status='broken', broken_at=?, failure_leg=?, failure_reason=?
               WHERE id=?""",
            (broken_at, failure_leg, failure_reason, chain_id))

    def mark_chain_confirmed(self, conn: sqlite3.Connection, chain_id: int) -> None:
        conn.execute("UPDATE exchange_chains SET status='confirmed' WHERE id=?", (chain_id,))

    # ---- 读 ----
    def get_chain(self, chain_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM exchange_chains WHERE id=?", (chain_id,)).fetchone()

    def get_chain_tx(self, conn: sqlite3.Connection, chain_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM exchange_chains WHERE id=?", (chain_id,)).fetchone()

    def legs(self, chain_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(f"{LEG_SELECT} WHERE l.chain_id=? ORDER BY l.position",
                                      (chain_id,)))

    def legs_tx(self, conn: sqlite3.Connection, chain_id: int) -> list[sqlite3.Row]:
        return list(conn.execute(f"{LEG_SELECT} WHERE l.chain_id=? ORDER BY l.position",
                                 (chain_id,)))

    def pending_leg_by_position(self, conn: sqlite3.Connection, chain_id: int, position: int) -> sqlite3.Row | None:
        return conn.execute(f"{LEG_SELECT} WHERE l.chain_id=? AND l.position=?",
                            (chain_id, position)).fetchone()

    def list_chains(self, status: str | None = None, hospital: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT DISTINCT ec.* FROM exchange_chains ec"
        params: list[Any] = []
        if hospital:
            sql += " JOIN exchange_legs el ON el.chain_id=ec.id"
        sql += " WHERE 1=1"
        if status:
            sql += " AND ec.status=?"; params.append(status)
        if hospital:
            sql += " AND ? IN (el.from_hospital, el.to_hospital)"; params.append(hospital)
        sql += " ORDER BY ec.id DESC"
        return list(self.conn.execute(sql, params))

    def pending_chains_past_deadline(self, conn: sqlite3.Connection, now_iso: str) -> list[sqlite3.Row]:
        return list(conn.execute(
            "SELECT * FROM exchange_chains WHERE status='pending' AND deadline < ? ORDER BY id",
            (now_iso,)))
