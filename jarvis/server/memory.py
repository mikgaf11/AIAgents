"""Persistent memory: episodes, semantic facts, goals, reminders.

Design notes
------------
Retrieval is a hybrid of lexical relevance (SQLite FTS5, with a LIKE
fallback if the build lacks it), recency decay, and a stored importance
weight. That combination behaves a lot like a small vector store for
conversational recall while keeping the whole system dependency-free and
inspectable with any SQLite browser.

All public methods are async and hand the blocking sqlite3 work to a
worker thread, so the reasoning loop never blocks on disk.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    session     TEXT    NOT NULL DEFAULT 'default',
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    importance  REAL    NOT NULL DEFAULT 0.5,
    consolidated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_episodes_ts ON episodes(ts);
CREATE INDEX IF NOT EXISTS idx_episodes_consolidated ON episodes(consolidated);

CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    subject     TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'fact',
    importance  REAL    NOT NULL DEFAULT 0.5,
    confidence  REAL    NOT NULL DEFAULT 0.8,
    source      TEXT    NOT NULL DEFAULT 'conversation',
    last_used   REAL    NOT NULL DEFAULT 0,
    use_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_facts_subject ON facts(subject);

CREATE TABLE IF NOT EXISTS goals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    title       TEXT    NOT NULL,
    detail      TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'open',
    priority    INTEGER NOT NULL DEFAULT 3,
    progress    REAL    NOT NULL DEFAULT 0,
    updated     REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    due         REAL    NOT NULL,
    text        TEXT    NOT NULL,
    fired       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(due, fired);

CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    text        TEXT    NOT NULL,
    surfaced    INTEGER NOT NULL DEFAULT 0
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts
    USING fts5(content, content='episodes', content_rowid='id');
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
    USING fts5(subject, content, content='facts', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, subject, content)
        VALUES (new.id, new.subject, new.content);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, subject, content)
        VALUES ('delete', old.id, old.subject, old.content);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, subject, content)
        VALUES ('delete', old.id, old.subject, old.content);
    INSERT INTO facts_fts(rowid, subject, content)
        VALUES (new.id, new.subject, new.content);
END;
"""

# Half-life in seconds for the recency term of the retrieval score (~5 days).
RECENCY_HALFLIFE = 5 * 24 * 3600.0

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 OR-query.

    User text can contain FTS operators, quotes and punctuation that would
    otherwise be a syntax error, so every token is quoted individually.
    """
    tokens = [t for t in _TOKEN_RE.findall(text) if len(t) > 1]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens[:24])


@dataclass
class Recalled:
    kind: str
    id: int
    text: str
    score: float
    ts: float
    meta: dict[str, Any]

    def render(self) -> str:
        age = time.time() - self.ts
        days = age / 86400
        when = f"{days:.1f}d ago" if days >= 1 else f"{age / 3600:.1f}h ago"
        return f"[{self.kind} #{self.id} · {when}] {self.text}"


class Memory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self.fts = True
        try:
            self._db.executescript(FTS_SCHEMA)
        except sqlite3.OperationalError:
            # Build without FTS5 — fall back to LIKE scanning.
            self.fts = False
        self._db.commit()

    # -- plumbing --------------------------------------------------------

    async def _run(self, fn, *args):
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    def close(self) -> None:
        self._db.close()

    # -- episodes --------------------------------------------------------

    async def add_episode(
        self,
        role: str,
        content: str,
        *,
        session: str = "default",
        importance: float = 0.5,
    ) -> int:
        def _write() -> int:
            cur = self._db.execute(
                "INSERT INTO episodes (ts, session, role, content, importance)"
                " VALUES (?,?,?,?,?)",
                (time.time(), session, role, content, importance),
            )
            self._db.commit()
            return int(cur.lastrowid)

        return await self._run(_write)

    async def recent_episodes(self, limit: int = 20) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            rows = self._db.execute(
                "SELECT * FROM episodes ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

        return await self._run(_read)

    # -- facts -----------------------------------------------------------

    async def add_fact(
        self,
        subject: str,
        content: str,
        *,
        kind: str = "fact",
        importance: float = 0.6,
        confidence: float = 0.85,
        source: str = "conversation",
    ) -> int:
        def _write() -> int:
            # Overwrite an existing fact with the same subject+kind rather
            # than accumulating near-duplicates the model has to reconcile.
            existing = self._db.execute(
                "SELECT id FROM facts WHERE subject = ? AND kind = ?"
                " ORDER BY id DESC LIMIT 1",
                (subject, kind),
            ).fetchone()
            now = time.time()
            if existing:
                self._db.execute(
                    "UPDATE facts SET content=?, ts=?, importance=?, confidence=?,"
                    " source=? WHERE id=?",
                    (content, now, importance, confidence, source, existing["id"]),
                )
                self._db.commit()
                return int(existing["id"])
            cur = self._db.execute(
                "INSERT INTO facts (ts, subject, content, kind, importance,"
                " confidence, source) VALUES (?,?,?,?,?,?,?)",
                (now, subject, content, kind, importance, confidence, source),
            )
            self._db.commit()
            return int(cur.lastrowid)

        return await self._run(_write)

    async def forget_fact(self, fact_id: int) -> bool:
        def _write() -> bool:
            cur = self._db.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
            self._db.commit()
            return cur.rowcount > 0

        return await self._run(_write)

    async def all_facts(self, limit: int = 200) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            rows = self._db.execute(
                "SELECT * FROM facts ORDER BY importance DESC, ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._run(_read)

    # -- retrieval -------------------------------------------------------

    async def recall(self, query: str, limit: int = 8) -> list[Recalled]:
        def _search() -> list[Recalled]:
            now = time.time()
            results: list[Recalled] = []
            match = _fts_query(query)

            def score(base: float, ts: float, importance: float) -> float:
                recency = math.pow(0.5, (now - ts) / RECENCY_HALFLIFE)
                return base * (0.55 + 0.45 * importance) * (0.4 + 0.6 * recency)

            if self.fts and match:
                for row in self._db.execute(
                    "SELECT f.*, bm25(facts_fts) AS rank FROM facts_fts"
                    " JOIN facts f ON f.id = facts_fts.rowid"
                    " WHERE facts_fts MATCH ? ORDER BY rank LIMIT ?",
                    (match, limit * 3),
                ):
                    base = 1.0 / (1.0 + max(0.0, float(row["rank"]) + 8.0))
                    results.append(
                        Recalled(
                            "fact",
                            int(row["id"]),
                            f'{row["subject"]}: {row["content"]}',
                            score(base * 1.35, row["ts"], row["importance"]),
                            row["ts"],
                            {"kind": row["kind"], "confidence": row["confidence"]},
                        )
                    )
                for row in self._db.execute(
                    "SELECT e.*, bm25(episodes_fts) AS rank FROM episodes_fts"
                    " JOIN episodes e ON e.id = episodes_fts.rowid"
                    " WHERE episodes_fts MATCH ? ORDER BY rank LIMIT ?",
                    (match, limit * 3),
                ):
                    base = 1.0 / (1.0 + max(0.0, float(row["rank"]) + 8.0))
                    results.append(
                        Recalled(
                            "episode",
                            int(row["id"]),
                            f'{row["role"]}: {row["content"]}',
                            score(base, row["ts"], row["importance"]),
                            row["ts"],
                            {"role": row["role"]},
                        )
                    )
            else:
                tokens = [t.lower() for t in _TOKEN_RE.findall(query) if len(t) > 2]
                if not tokens:
                    return []
                like = f"%{tokens[0]}%"
                for row in self._db.execute(
                    "SELECT * FROM facts WHERE lower(content) LIKE ?"
                    " OR lower(subject) LIKE ? ORDER BY ts DESC LIMIT ?",
                    (like, like, limit * 2),
                ):
                    results.append(
                        Recalled(
                            "fact",
                            int(row["id"]),
                            f'{row["subject"]}: {row["content"]}',
                            score(0.6, row["ts"], row["importance"]),
                            row["ts"],
                            {"kind": row["kind"]},
                        )
                    )
                for row in self._db.execute(
                    "SELECT * FROM episodes WHERE lower(content) LIKE ?"
                    " ORDER BY ts DESC LIMIT ?",
                    (like, limit * 2),
                ):
                    results.append(
                        Recalled(
                            "episode",
                            int(row["id"]),
                            f'{row["role"]}: {row["content"]}',
                            score(0.4, row["ts"], row["importance"]),
                            row["ts"],
                            {"role": row["role"]},
                        )
                    )

            results.sort(key=lambda r: r.score, reverse=True)
            top = results[:limit]
            fact_ids = [r.id for r in top if r.kind == "fact"]
            if fact_ids:
                marks = ",".join("?" * len(fact_ids))
                self._db.execute(
                    f"UPDATE facts SET use_count = use_count + 1, last_used = ?"
                    f" WHERE id IN ({marks})",
                    [now, *fact_ids],
                )
                self._db.commit()
            return top

        return await self._run(_search)

    # -- goals -----------------------------------------------------------

    async def add_goal(self, title: str, detail: str = "", priority: int = 3) -> int:
        def _write() -> int:
            now = time.time()
            cur = self._db.execute(
                "INSERT INTO goals (ts, title, detail, priority, updated)"
                " VALUES (?,?,?,?,?)",
                (now, title, detail, priority, now),
            )
            self._db.commit()
            return int(cur.lastrowid)

        return await self._run(_write)

    async def update_goal(
        self,
        goal_id: int,
        *,
        status: str | None = None,
        progress: float | None = None,
        detail: str | None = None,
    ) -> bool:
        def _write() -> bool:
            sets, params = ["updated = ?"], [time.time()]
            if status is not None:
                sets.append("status = ?")
                params.append(status)
            if progress is not None:
                sets.append("progress = ?")
                params.append(max(0.0, min(1.0, progress)))
            if detail is not None:
                sets.append("detail = ?")
                params.append(detail)
            params.append(goal_id)
            cur = self._db.execute(
                f"UPDATE goals SET {', '.join(sets)} WHERE id = ?", params
            )
            self._db.commit()
            return cur.rowcount > 0

        return await self._run(_write)

    async def list_goals(self, status: str | None = "open") -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            if status:
                rows = self._db.execute(
                    "SELECT * FROM goals WHERE status = ?"
                    " ORDER BY priority ASC, updated DESC",
                    (status,),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM goals ORDER BY priority ASC, updated DESC"
                ).fetchall()
            return [dict(r) for r in rows]

        return await self._run(_read)

    # -- reminders -------------------------------------------------------

    async def add_reminder(self, text: str, due: float) -> int:
        def _write() -> int:
            cur = self._db.execute(
                "INSERT INTO reminders (ts, due, text) VALUES (?,?,?)",
                (time.time(), due, text),
            )
            self._db.commit()
            return int(cur.lastrowid)

        return await self._run(_write)

    async def due_reminders(self) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            now = time.time()
            rows = self._db.execute(
                "SELECT * FROM reminders WHERE fired = 0 AND due <= ? ORDER BY due",
                (now,),
            ).fetchall()
            if rows:
                self._db.execute(
                    "UPDATE reminders SET fired = 1 WHERE id IN (%s)"
                    % ",".join(str(int(r["id"])) for r in rows)
                )
                self._db.commit()
            return [dict(r) for r in rows]

        return await self._run(_read)

    async def list_reminders(self) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            rows = self._db.execute(
                "SELECT * FROM reminders WHERE fired = 0 ORDER BY due LIMIT 50"
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._run(_read)

    # -- observations (the background mind's scratchpad) -----------------

    async def add_observation(self, text: str) -> int:
        def _write() -> int:
            cur = self._db.execute(
                "INSERT INTO observations (ts, text) VALUES (?,?)",
                (time.time(), text),
            )
            self._db.commit()
            return int(cur.lastrowid)

        return await self._run(_write)

    async def recent_observations(self, limit: int = 10) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            rows = self._db.execute(
                "SELECT * FROM observations ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._run(_read)

    # -- consolidation ---------------------------------------------------

    async def unconsolidated(self, limit: int = 40) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            rows = self._db.execute(
                "SELECT * FROM episodes WHERE consolidated = 0 ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._run(_read)

    async def mark_consolidated(self, ids: Iterable[int]) -> None:
        ids = list(ids)
        if not ids:
            return

        def _write() -> None:
            marks = ",".join("?" * len(ids))
            self._db.execute(
                f"UPDATE episodes SET consolidated = 1 WHERE id IN ({marks})", ids
            )
            self._db.commit()

        await self._run(_write)

    # -- stats -----------------------------------------------------------

    async def stats(self) -> dict[str, Any]:
        def _read() -> dict[str, Any]:
            def count(table: str, where: str = "") -> int:
                sql = f"SELECT COUNT(*) AS c FROM {table} {where}"
                return int(self._db.execute(sql).fetchone()["c"])

            return {
                "episodes": count("episodes"),
                "facts": count("facts"),
                "goals_open": count("goals", "WHERE status='open'"),
                "reminders_pending": count("reminders", "WHERE fired=0"),
                "observations": count("observations"),
                "db_kb": round(self.path.stat().st_size / 1024, 1)
                if self.path.exists()
                else 0.0,
                "fts": self.fts,
            }

        return await self._run(_read)

    async def export(self) -> str:
        facts = await self.all_facts(1000)
        goals = await self.list_goals(None)
        return json.dumps({"facts": facts, "goals": goals}, indent=2)
