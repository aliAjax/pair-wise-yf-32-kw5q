"""链记录模块：配对链、链环节与链事件的持久化。

与配对判断（chain_matching.py）和协调台（app.py / static/index.html）分开维护。
事务边界由调用方（协调台服务）控制，本模块只负责读写。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS chains(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL DEFAULT 'pending',
    confirm_deadline TEXT NOT NULL,
    created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS chain_legs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id INTEGER NOT NULL REFERENCES chains(id),
    seq INTEGER NOT NULL,
    donor_id INTEGER NOT NULL REFERENCES donors(id),
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    from_hospital TEXT NOT NULL, to_hospital TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    responded_by TEXT, responded_at TEXT, reason TEXT,
    UNIQUE(chain_id, seq), UNIQUE(chain_id, donor_id)
);
CREATE TABLE IF NOT EXISTS chain_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_id INTEGER NOT NULL, leg_id INTEGER,
    actor TEXT NOT NULL, role TEXT NOT NULL,
    action TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""

CHAIN_STATUSES = {"pending", "confirmed", "broken", "timed_out"}
LEG_STATUSES = {"pending", "confirmed", "rejected", "timed_out", "released"}


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ChainStore:
    """配对链记录的读写。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.executescript(SCHEMA)

    def create_chain(self, deadline: str, actor: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO chains(status,confirm_deadline,created_by,created_at,updated_at) VALUES('pending',?,?,?,?)",
            (deadline, actor, _iso(), _iso()))
        return cur.lastrowid

    def add_leg(self, chain_id: int, seq: int, donor: sqlite3.Row, candidate: sqlite3.Row) -> int:
        cur = self.conn.execute(
            """INSERT INTO chain_legs(chain_id,seq,donor_id,candidate_id,from_hospital,to_hospital)
               VALUES(?,?,?,?,?,?)""",
            (chain_id, seq, donor["id"], candidate["id"], donor["hospital"], candidate["hospital"]))
        return cur.lastrowid

    def chain(self, chain_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM chains WHERE id=?", (chain_id,)).fetchone()

    def legs(self, chain_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM chain_legs WHERE chain_id=? ORDER BY seq", (chain_id,)).fetchall()

    def leg(self, chain_id: int, seq: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM chain_legs WHERE chain_id=? AND seq=?", (chain_id, seq)).fetchone()

    def set_leg(self, leg_id: int, status: str, actor: str | None = None, reason: str | None = None) -> None:
        if actor is not None:
            self.conn.execute("UPDATE chain_legs SET status=?,responded_by=?,responded_at=?,reason=? WHERE id=?",
                              (status, actor, _iso(), reason, leg_id))
        else:
            self.conn.execute("UPDATE chain_legs SET status=?,responded_at=? WHERE id=?", (status, _iso(), leg_id))

    def set_chain(self, chain_id: int, status: str) -> None:
        resolved = _iso() if status in {"confirmed", "broken", "timed_out"} else None
        self.conn.execute("UPDATE chains SET status=?,updated_at=?,resolved_at=COALESCE(?,resolved_at) WHERE id=?",
                          (status, _iso(), resolved, chain_id))

    def event(self, chain_id: int, leg_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO chain_events(chain_id,leg_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (chain_id, leg_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), _iso()))

    def events(self, chain_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id,chain_id,leg_id,actor,role,action,detail_json,created_at FROM chain_events WHERE chain_id=? ORDER BY id",
            (chain_id,)).fetchall()

    def chains_for(self, role: str, hospital: str) -> list[sqlite3.Row]:
        if role == "hospital":
            return self.conn.execute(
                """SELECT DISTINCT c.* FROM chains c JOIN chain_legs l ON l.chain_id=c.id
                   WHERE l.from_hospital=? OR l.to_hospital=? ORDER BY c.id DESC""", (hospital, hospital)).fetchall()
        if role in {"coordinator", "allocation_officer", "auditor"}:
            return self.conn.execute("SELECT * FROM chains ORDER BY id DESC").fetchall()
        return []

    def pending_leg_for_candidate(self, candidate_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT l.* FROM chain_legs l JOIN chains c ON c.id=l.chain_id
               WHERE l.candidate_id=? AND c.status='pending' AND l.status='pending'""", (candidate_id,)).fetchone()
