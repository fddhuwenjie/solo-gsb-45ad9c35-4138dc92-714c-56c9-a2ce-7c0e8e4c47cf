"""SQLite 持久层：调色台配置与历史版本。"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

DB_PATH = os.environ.get("DATABASE_PATH", "/data/palette.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    payload TEXT NOT NULL,
    result TEXT,
    created_at TEXT NOT NULL
);
"""


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.executescript(_SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_config() -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT payload FROM config WHERE id = 1").fetchone()
    return json.loads(row["payload"]) if row else None


def save_config(payload: dict) -> dict:
    ts = _now()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO config (id, payload, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, "
            "updated_at = excluded.updated_at",
            (json.dumps(payload, ensure_ascii=False), ts),
        )
    return {"updated_at": ts}


def create_version(label: str, payload: dict, result: dict | None) -> dict:
    vid = uuid.uuid4().hex[:12]
    ts = _now()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO versions (id, label, payload, result, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (vid, label, json.dumps(payload, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False) if result else None, ts),
        )
    return {"id": vid, "label": label, "created_at": ts}


def list_versions() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, label, created_at FROM versions ORDER BY created_at DESC, id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_version(vid: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, label, payload, result, created_at FROM versions WHERE id = ?",
            (vid,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "label": row["label"],
        "created_at": row["created_at"],
        "payload": json.loads(row["payload"]),
        "result": json.loads(row["result"]) if row["result"] else None,
    }


def delete_version(vid: str) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM versions WHERE id = ?", (vid,))
    return cur.rowcount > 0
