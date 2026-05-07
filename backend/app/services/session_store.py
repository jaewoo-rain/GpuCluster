"""
SQLite-backed session record store (Stage 8).

설계
  - stdlib sqlite3 사용. 새 dep 없음. 작은 prototype 에 충분.
  - 모든 메서드는 sync — SessionManager 가 asyncio.to_thread() 로 감싸서
    이벤트 루프를 안 막음.
  - 매 호출마다 새 connection — sqlite3.Connection 이 thread-safe 보장
    안 하므로, to_thread 가 아무 worker 에서 도는 걸 안전하게 받기 위함.
  - WAL 모드는 켜지 않음. 워크로드가 매우 가볍고 (수 ~ 수십 row) 쿼리도
    짧아서 default rollback journal 로도 충분.

스키마 마이그레이션
  - 새 컬럼은 _MIGRATIONS 에 ADD COLUMN 으로 추가. PRAGMA table_info 로
    이미 있으면 skip 하므로 idempotent.
  - 컬럼 삭제 / 타입 변경은 SQLite 가 까다로워서 production 이 아닌 한
    `data/sessions.db` 수동 삭제 권장.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.schemas.session import Session


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    container_id    TEXT NOT NULL,
    container_name  TEXT NOT NULL,
    ratio           REAL NOT NULL,
    quota_bytes     INTEGER,
    image           TEXT NOT NULL,
    command_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL,
    exit_code       INTEGER,
    gpu_index       INTEGER,
    mode            TEXT NOT NULL DEFAULT 'batch',
    host_port       INTEGER,
    jupyter_token   TEXT,
    jupyter_url     TEXT,
    workspace_dir   TEXT
);
"""

# Idempotent ADD COLUMN migration. SQLite 는 ADD COLUMN IF NOT EXISTS 미지원이라
# PRAGMA table_info 로 검사. 신규 컬럼 추가만, drop / rename 은 안 함.
_MIGRATIONS = [
    ("gpu_index",     "ALTER TABLE sessions ADD COLUMN gpu_index INTEGER"),
    ("mode",          "ALTER TABLE sessions ADD COLUMN mode TEXT NOT NULL DEFAULT 'batch'"),
    ("host_port",     "ALTER TABLE sessions ADD COLUMN host_port INTEGER"),
    ("jupyter_token", "ALTER TABLE sessions ADD COLUMN jupyter_token TEXT"),
    ("jupyter_url",   "ALTER TABLE sessions ADD COLUMN jupyter_url TEXT"),
    ("workspace_dir", "ALTER TABLE sessions ADD COLUMN workspace_dir TEXT"),
]

_SELECT_COLS = (
    "id, container_id, container_name, ratio, quota_bytes, "
    "image, command_json, created_at, status, exit_code, gpu_index, "
    "mode, host_port, jupyter_token, jupyter_url, workspace_dir"
)


class SessionStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as c:
            c.execute(SCHEMA_SQL)
            existing = {row[1] for row in c.execute("PRAGMA table_info(sessions)")}
            for col, ddl in _MIGRATIONS:
                if col not in existing:
                    c.execute(ddl)

    def _conn(self) -> sqlite3.Connection:
        # isolation_level=None = autocommit. 단순 단일 statement 만 실행하므로
        # 명시적 트랜잭션 관리 안 해도 안전.
        return sqlite3.connect(self.db_path, isolation_level=None, timeout=5.0)

    @staticmethod
    def _row_to_session(row: tuple) -> Session:
        return Session(
            id=row[0],
            container_id=row[1],
            container_name=row[2],
            ratio=row[3],
            quota_bytes=row[4],
            image=row[5],
            command=json.loads(row[6]),
            created_at=datetime.fromisoformat(row[7]),
            status=row[8],
            exit_code=row[9],
            gpu_index=row[10],
            mode=row[11] or "batch",
            host_port=row[12],
            jupyter_token=row[13],
            jupyter_url=row[14],
            workspace_dir=row[15],
        )

    # ---- CRUD --------------------------------------------------------- #
    def insert(self, s: Session) -> None:
        with closing(self._conn()) as c:
            c.execute(
                "INSERT INTO sessions ("
                + _SELECT_COLS
                + ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    s.id,
                    s.container_id,
                    s.container_name,
                    s.ratio,
                    s.quota_bytes,
                    s.image,
                    json.dumps(s.command),
                    s.created_at.isoformat(),
                    s.status,
                    s.exit_code,
                    s.gpu_index,
                    s.mode,
                    s.host_port,
                    s.jupyter_token,
                    s.jupyter_url,
                    s.workspace_dir,
                ),
            )

    def get(self, sid: str) -> Optional[Session]:
        with closing(self._conn()) as c:
            row = c.execute(
                f"SELECT {_SELECT_COLS} FROM sessions WHERE id = ?", (sid,)
            ).fetchone()
        return None if row is None else self._row_to_session(row)

    def list_all(self) -> list[Session]:
        with closing(self._conn()) as c:
            rows = c.execute(
                f"SELECT {_SELECT_COLS} FROM sessions ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def update_status(
        self, sid: str, status: str, exit_code: Optional[int]
    ) -> None:
        with closing(self._conn()) as c:
            c.execute(
                "UPDATE sessions SET status = ?, exit_code = ? WHERE id = ?",
                (status, exit_code, sid),
            )

    def delete(self, sid: str) -> bool:
        with closing(self._conn()) as c:
            cur = c.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        return cur.rowcount > 0
