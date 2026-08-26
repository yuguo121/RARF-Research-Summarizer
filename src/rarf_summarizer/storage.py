from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS papers (
    id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    relative_path TEXT,
    folder TEXT,
    file_hash TEXT NOT NULL,
    title TEXT,
    authors TEXT,
    year TEXT,
    doi TEXT,
    page_count INTEGER,
    warnings TEXT,
    extracted_at TEXT,
    status TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    paper_id TEXT NOT NULL,
    page_number INTEGER NOT NULL,
    clean_text TEXT,
    char_count INTEGER,
    is_low_text INTEGER,
    PRIMARY KEY (paper_id, page_number)
);

CREATE TABLE IF NOT EXISTS sections (
    paper_id TEXT NOT NULL,
    section_key TEXT,
    title TEXT,
    page_start INTEGER,
    page_end INTEGER,
    text TEXT
);

CREATE TABLE IF NOT EXISTS field_values (
    paper_id TEXT NOT NULL,
    field_id TEXT NOT NULL,
    status TEXT,
    confidence REAL,
    generated_text TEXT,
    generated_json TEXT,
    human_text TEXT,
    last_exported_text TEXT,
    source TEXT,
    updated_at TEXT,
    PRIMARY KEY (paper_id, field_id)
);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id TEXT NOT NULL,
    field_id TEXT NOT NULL,
    quote TEXT,
    page INTEGER,
    matched INTEGER,
    score REAL,
    location TEXT,
    extra_json TEXT
);

CREATE TABLE IF NOT EXISTS constructs (
    paper_id TEXT NOT NULL,
    construct_id TEXT NOT NULL,
    class TEXT,
    name TEXT,
    nominal_definition TEXT,
    PRIMARY KEY (paper_id, construct_id)
);

CREATE TABLE IF NOT EXISTS measures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id TEXT NOT NULL,
    construct_id TEXT,
    class TEXT,
    name TEXT,
    operationalization TEXT,
    range TEXT,
    type TEXT,
    linked_construct TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id TEXT,
    session_type TEXT,
    cache_key TEXT,
    agent_id TEXT,
    run_id TEXT,
    model TEXT,
    schema_version TEXT,
    prompt_version TEXT,
    status TEXT,
    error TEXT,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS qa_warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id TEXT,
    field_id TEXT,
    warning TEXT
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class _LockedCursor:
    """Snapshot lastrowid and serialize fetches on a shared connection."""

    def __init__(self, cursor: sqlite3.Cursor, lock: threading.RLock, lastrowid: int | None):
        self._cursor = cursor
        self._lock = lock
        self.lastrowid = lastrowid

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)

    def fetchall(self):
        with self._lock:
            return self._cursor.fetchall()

    def fetchone(self):
        with self._lock:
            return self._cursor.fetchone()

    def fetchmany(self, size: int | None = None):
        with self._lock:
            if size is None:
                return self._cursor.fetchmany()
            return self._cursor.fetchmany(size)


class _LockedConnection:
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def execute(self, *args, **kwargs):
        with self._lock:
            cursor = self._conn.execute(*args, **kwargs)
            return _LockedCursor(cursor, self._lock, cursor.lastrowid)

    def executemany(self, *args, **kwargs):
        with self._lock:
            cursor = self._conn.executemany(*args, **kwargs)
            return _LockedCursor(cursor, self._lock, cursor.lastrowid)

    def executescript(self, *args, **kwargs):
        with self._lock:
            return self._conn.executescript(*args, **kwargs)

    def commit(self):
        with self._lock:
            return self._conn.commit()

    def close(self):
        return self._conn.close()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        raw = sqlite3.connect(path, check_same_thread=False, timeout=8.0)
        raw.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.conn = _LockedConnection(raw, self._lock)
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def upsert_paper(self, record: dict[str, Any]) -> None:
        columns = [
            "id",
            "source_path",
            "relative_path",
            "folder",
            "file_hash",
            "title",
            "authors",
            "year",
            "doi",
            "page_count",
            "warnings",
            "extracted_at",
            "status",
        ]
        values = [record.get(col) for col in columns]
        placeholders = ",".join("?" for _ in columns)
        assignments = ",".join(f"{col}=excluded.{col}" for col in columns if col != "id")
        self.conn.execute(
            f"INSERT INTO papers ({','.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {assignments}",
            values,
        )
        self.conn.commit()

    def replace_pages(self, paper_id: str, pages: Iterable[dict[str, Any]]) -> None:
        self.conn.execute("DELETE FROM pages WHERE paper_id=?", (paper_id,))
        self.conn.executemany(
            "INSERT INTO pages (paper_id, page_number, clean_text, char_count, is_low_text) "
            "VALUES (:paper_id, :page_number, :clean_text, :char_count, :is_low_text)",
            list(pages),
        )
        self.conn.commit()

    def replace_sections(self, paper_id: str, sections: Iterable[dict[str, Any]]) -> None:
        self.conn.execute("DELETE FROM sections WHERE paper_id=?", (paper_id,))
        self.conn.executemany(
            "INSERT INTO sections (paper_id, section_key, title, page_start, page_end, text) "
            "VALUES (:paper_id, :section_key, :title, :page_start, :page_end, :text)",
            list(sections),
        )
        self.conn.commit()

    def get_paper(self, paper_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        return dict(row) if row else None

    def list_papers(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM papers ORDER BY relative_path, title").fetchall()
        return [dict(row) for row in rows]

    def page_map(self, paper_id: str) -> dict[int, str]:
        rows = self.conn.execute(
            "SELECT page_number, clean_text FROM pages WHERE paper_id=? ORDER BY page_number",
            (paper_id,),
        ).fetchall()
        return {int(row["page_number"]): row["clean_text"] or "" for row in rows}

    def list_pages(self, paper_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM pages WHERE paper_id=? ORDER BY page_number",
            (paper_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_sections(self, paper_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM sections WHERE paper_id=? ORDER BY page_start, rowid",
            (paper_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def cached_run(self, paper_id: str, session_type: str, cache_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE paper_id=? AND session_type=? AND cache_key=? "
            "AND status='finished' ORDER BY id DESC LIMIT 1",
            (paper_id, session_type, cache_key),
        ).fetchone()
        return dict(row) if row else None

    def add_run(self, record: dict[str, Any]) -> int:
        columns = [
            "paper_id",
            "session_type",
            "cache_key",
            "agent_id",
            "run_id",
            "model",
            "schema_version",
            "prompt_version",
            "status",
            "error",
            "started_at",
            "finished_at",
        ]
        cursor = self.conn.execute(
            f"INSERT INTO runs ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            [record.get(col) for col in columns],
        )
        rowid = cursor.lastrowid
        self.conn.commit()
        if rowid is None:
            raise RuntimeError("INSERT into runs did not return lastrowid")
        return int(rowid)

    def update_run(self, run_pk: int, **fields: Any) -> None:
        assignments = ", ".join(f"{key}=?" for key in fields)
        self.conn.execute(f"UPDATE runs SET {assignments} WHERE id=?", [*fields.values(), run_pk])
        self.conn.commit()

    def list_runs(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM runs ORDER BY id").fetchall()]

    def upsert_field(
        self,
        paper_id: str,
        field_id: str,
        payload: dict[str, Any],
        preserve_human: bool = True,
        clear_human: bool = False,
    ) -> None:
        existing = self.get_field(paper_id, field_id)
        if clear_human:
            human_text = None
            source = "generated"
            human_clause = "human_text=NULL"
        elif preserve_human:
            human_text = existing.get("human_text") if existing else payload.get("human_text")
            source = "human" if human_text else "generated"
            human_clause = "human_text=COALESCE(excluded.human_text, field_values.human_text)"
        else:
            human_text = payload.get("human_text")
            source = "human" if human_text else "generated"
            human_clause = "human_text=excluded.human_text"
        self.conn.execute(
            f"""
            INSERT INTO field_values (
                paper_id, field_id, status, confidence, generated_text, generated_json,
                human_text, last_exported_text, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_id, field_id) DO UPDATE SET
                status=excluded.status,
                confidence=excluded.confidence,
                generated_text=excluded.generated_text,
                generated_json=excluded.generated_json,
                {human_clause},
                last_exported_text=COALESCE(excluded.last_exported_text, field_values.last_exported_text),
                source=excluded.source,
                updated_at=excluded.updated_at
            """,
            (
                paper_id,
                field_id,
                payload.get("status"),
                payload.get("confidence"),
                payload.get("generated_text"),
                payload.get("generated_json"),
                human_text,
                payload.get("last_exported_text", existing.get("last_exported_text") if existing else None),
                source,
                utc_now(),
            ),
        )
        self.conn.commit()

    def set_human_override(self, paper_id: str, field_id: str, text: str) -> None:
        existing = self.get_field(paper_id, field_id) or {}
        self.conn.execute(
            """
            INSERT INTO field_values (
                paper_id, field_id, status, confidence, generated_text, generated_json,
                human_text, last_exported_text, source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'human', ?)
            ON CONFLICT(paper_id, field_id) DO UPDATE SET
                human_text=excluded.human_text,
                source='human',
                updated_at=excluded.updated_at
            """,
            (
                paper_id,
                field_id,
                existing.get("status") or "present",
                existing.get("confidence"),
                existing.get("generated_text"),
                existing.get("generated_json"),
                text,
                existing.get("last_exported_text"),
                utc_now(),
            ),
        )
        self.conn.commit()

    def mark_exported(self, paper_id: str, field_id: str, text: str) -> None:
        self.conn.execute(
            "UPDATE field_values SET last_exported_text=? WHERE paper_id=? AND field_id=?",
            (text, paper_id, field_id),
        )
        self.conn.commit()

    def get_field(self, paper_id: str, field_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM field_values WHERE paper_id=? AND field_id=?",
            (paper_id, field_id),
        ).fetchone()
        return dict(row) if row else None

    def fields_for(self, paper_id: str) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM field_values WHERE paper_id=?", (paper_id,)).fetchall()
        return {row["field_id"]: dict(row) for row in rows}

    def replace_evidence(self, paper_id: str, rows: list[dict[str, Any]], field_ids: list[str] | None = None) -> None:
        if field_ids:
            placeholders = ",".join("?" for _ in field_ids)
            self.conn.execute(
                f"DELETE FROM evidence WHERE paper_id=? AND field_id IN ({placeholders})",
                [paper_id, *field_ids],
            )
        else:
            self.conn.execute("DELETE FROM evidence WHERE paper_id=?", (paper_id,))
        for row in rows:
            extra = row.get("extra_json")
            if isinstance(extra, (dict, list)):
                extra = json.dumps(extra, ensure_ascii=False)
            self.conn.execute(
                "INSERT INTO evidence (paper_id, field_id, quote, page, matched, score, location, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    paper_id,
                    row.get("field_id"),
                    row.get("quote"),
                    row.get("page"),
                    1 if row.get("matched") else 0,
                    row.get("score"),
                    row.get("location"),
                    extra,
                ),
            )
        self.conn.commit()

    def list_evidence(self, paper_id: str | None = None) -> list[dict[str, Any]]:
        if paper_id:
            rows = self.conn.execute("SELECT * FROM evidence WHERE paper_id=?", (paper_id,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM evidence ORDER BY paper_id, field_id").fetchall()
        return [dict(row) for row in rows]

    def replace_constructs(self, paper_id: str, rows: list[dict[str, Any]]) -> None:
        self.conn.execute("DELETE FROM constructs WHERE paper_id=?", (paper_id,))
        self.conn.executemany(
            "INSERT INTO constructs (paper_id, construct_id, class, name, nominal_definition) "
            "VALUES (:paper_id, :construct_id, :class, :name, :nominal_definition)",
            rows,
        )
        self.conn.commit()

    def replace_measures(self, paper_id: str, rows: list[dict[str, Any]]) -> None:
        self.conn.execute("DELETE FROM measures WHERE paper_id=?", (paper_id,))
        self.conn.executemany(
            "INSERT INTO measures (paper_id, construct_id, class, name, operationalization, range, type, linked_construct) "
            "VALUES (:paper_id, :construct_id, :class, :name, :operationalization, :range, :type, :linked_construct)",
            rows,
        )
        self.conn.commit()

    def list_constructs(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM constructs").fetchall()]

    def list_measures(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM measures").fetchall()]

    def replace_warnings(self, paper_id: str, warnings: list[tuple[str, str]], field_ids: list[str] | None = None) -> None:
        if field_ids:
            placeholders = ",".join("?" for _ in field_ids)
            self.conn.execute(
                f"DELETE FROM qa_warnings WHERE paper_id=? AND field_id IN ({placeholders})",
                [paper_id, *field_ids],
            )
        else:
            self.conn.execute("DELETE FROM qa_warnings WHERE paper_id=?", (paper_id,))
        self.conn.executemany(
            "INSERT INTO qa_warnings (paper_id, field_id, warning) VALUES (?, ?, ?)",
            [(paper_id, field_id, warning) for field_id, warning in warnings],
        )
        self.conn.commit()

    def list_warnings(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute("SELECT * FROM qa_warnings").fetchall()]
