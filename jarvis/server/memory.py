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

-- Triaged mail. Bodies are deliberately not stored in full: a snippet is
-- enough for triage and recall, and it keeps the database small and less
-- sensitive than a second copy of the whole mailbox.
CREATE TABLE IF NOT EXISTS emails (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL    NOT NULL,
    account      TEXT    NOT NULL DEFAULT 'default',
    folder       TEXT    NOT NULL DEFAULT 'INBOX',
    uid          INTEGER NOT NULL,
    message_id   TEXT    NOT NULL DEFAULT '',
    sender       TEXT    NOT NULL DEFAULT '',
    sender_email TEXT    NOT NULL DEFAULT '',
    subject      TEXT    NOT NULL DEFAULT '',
    snippet      TEXT    NOT NULL DEFAULT '',
    received     REAL    NOT NULL DEFAULT 0,
    priority     INTEGER NOT NULL DEFAULT 2,
    category     TEXT    NOT NULL DEFAULT 'other',
    summary      TEXT    NOT NULL DEFAULT '',
    action       TEXT    NOT NULL DEFAULT '',
    needs_reply  INTEGER NOT NULL DEFAULT 0,
    handled      INTEGER NOT NULL DEFAULT 0,
    UNIQUE(account, folder, uid)
);
CREATE INDEX IF NOT EXISTS idx_emails_priority ON emails(handled, priority DESC, ts DESC);

-- Outbound drafts wait here for explicit approval. Nothing is ever sent
-- without the user pressing the button.
CREATE TABLE IF NOT EXISTS drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    email_id    INTEGER,
    to_addr     TEXT    NOT NULL,
    subject     TEXT    NOT NULL,
    body        TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'pending',
    sent_at     REAL    NOT NULL DEFAULT 0
);

-- The background work queue: things JARVIS decided to do for itself.
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    title       TEXT    NOT NULL,
    detail      TEXT    NOT NULL DEFAULT '',
    origin      TEXT    NOT NULL DEFAULT 'cognition',
    status      TEXT    NOT NULL DEFAULT 'pending',
    priority    INTEGER NOT NULL DEFAULT 3,
    attempts    INTEGER NOT NULL DEFAULT 0,
    result      TEXT    NOT NULL DEFAULT '',
    started     REAL    NOT NULL DEFAULT 0,
    finished    REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, priority, id);

-- Money-making opportunities, tracked like goals but with an explicit
-- thesis and a concrete next step so they don't stay abstract.
CREATE TABLE IF NOT EXISTS ventures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL    NOT NULL,
    title       TEXT    NOT NULL,
    thesis      TEXT    NOT NULL DEFAULT '',
    category    TEXT    NOT NULL DEFAULT 'other',
    effort      TEXT    NOT NULL DEFAULT 'medium',
    horizon     TEXT    NOT NULL DEFAULT 'weeks',
    confidence  REAL    NOT NULL DEFAULT 0.5,
    next_step   TEXT    NOT NULL DEFAULT '',
    evidence    TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'proposed',
    updated     REAL    NOT NULL
);

-- Per-connector bookkeeping, e.g. the highest mail UID already seen.
CREATE TABLE IF NOT EXISTS connector_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated     REAL NOT NULL
);
"""

PRIORITY_LABELS = {0: "noise", 1: "low", 2: "normal", 3: "high", 4: "critical"}

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

    # -- connector bookkeeping -------------------------------------------

    async def get_state(self, key: str, default: str = "") -> str:
        def _read() -> str:
            row = self._db.execute(
                "SELECT value FROM connector_state WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

        return await self._run(_read)

    async def set_state(self, key: str, value: str) -> None:
        def _write() -> None:
            self._db.execute(
                "INSERT INTO connector_state (key, value, updated) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                " updated=excluded.updated",
                (key, value, time.time()),
            )
            self._db.commit()

        await self._run(_write)

    # -- email -----------------------------------------------------------

    async def record_email(self, message: dict[str, Any]) -> int | None:
        """Store a triaged email. Returns None if this UID was already seen."""

        def _write() -> int | None:
            try:
                cur = self._db.execute(
                    "INSERT INTO emails (ts, account, folder, uid, message_id, sender,"
                    " sender_email, subject, snippet, received, priority, category,"
                    " summary, action, needs_reply)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        time.time(),
                        message.get("account", "default"),
                        message.get("folder", "INBOX"),
                        int(message.get("uid", 0)),
                        message.get("message_id", ""),
                        message.get("sender", ""),
                        message.get("sender_email", ""),
                        message.get("subject", ""),
                        message.get("snippet", "")[:2000],
                        float(message.get("received", 0) or 0),
                        int(message.get("priority", 2)),
                        message.get("category", "other"),
                        message.get("summary", ""),
                        message.get("action", ""),
                        1 if message.get("needs_reply") else 0,
                    ),
                )
                self._db.commit()
                return int(cur.lastrowid)
            except sqlite3.IntegrityError:
                return None  # already triaged

        return await self._run(_write)

    async def list_emails(
        self, *, limit: int = 40, unhandled_only: bool = False
    ) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            sql = "SELECT * FROM emails"
            if unhandled_only:
                sql += " WHERE handled = 0"
            sql += " ORDER BY priority DESC, received DESC LIMIT ?"
            rows = self._db.execute(sql, (limit,)).fetchall()
            return [self._email_row(r) for r in rows]

        return await self._run(_read)

    async def urgent_emails(self, limit: int = 5) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            rows = self._db.execute(
                "SELECT * FROM emails WHERE handled = 0 AND priority >= 3"
                " ORDER BY priority DESC, received DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._email_row(r) for r in rows]

        return await self._run(_read)

    async def mark_email_handled(self, email_id: int) -> bool:
        def _write() -> bool:
            cur = self._db.execute(
                "UPDATE emails SET handled = 1 WHERE id = ?", (email_id,)
            )
            self._db.commit()
            return cur.rowcount > 0

        return await self._run(_write)

    async def search_emails(self, query: str, limit: int = 15) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            like = f"%{query.lower()}%"
            rows = self._db.execute(
                "SELECT * FROM emails WHERE lower(subject) LIKE ?"
                " OR lower(sender) LIKE ? OR lower(snippet) LIKE ?"
                " ORDER BY received DESC LIMIT ?",
                (like, like, like, limit),
            ).fetchall()
            return [self._email_row(r) for r in rows]

        return await self._run(_read)

    @staticmethod
    def _email_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["priority_label"] = PRIORITY_LABELS.get(data["priority"], "normal")
        return data

    # -- drafts ----------------------------------------------------------

    async def add_draft(
        self, to_addr: str, subject: str, body: str, email_id: int | None = None
    ) -> int:
        def _write() -> int:
            cur = self._db.execute(
                "INSERT INTO drafts (ts, email_id, to_addr, subject, body)"
                " VALUES (?,?,?,?,?)",
                (time.time(), email_id, to_addr, subject, body),
            )
            self._db.commit()
            return int(cur.lastrowid)

        return await self._run(_write)

    async def list_drafts(self, status: str = "pending") -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            rows = self._db.execute(
                "SELECT * FROM drafts WHERE status = ? ORDER BY id DESC LIMIT 25",
                (status,),
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._run(_read)

    async def get_draft(self, draft_id: int) -> dict[str, Any] | None:
        def _read() -> dict[str, Any] | None:
            row = self._db.execute(
                "SELECT * FROM drafts WHERE id = ?", (draft_id,)
            ).fetchone()
            return dict(row) if row else None

        return await self._run(_read)

    async def set_draft_status(self, draft_id: int, status: str) -> bool:
        def _write() -> bool:
            cur = self._db.execute(
                "UPDATE drafts SET status = ?, sent_at = ? WHERE id = ?",
                (status, time.time() if status == "sent" else 0, draft_id),
            )
            self._db.commit()
            return cur.rowcount > 0

        return await self._run(_write)

    # -- background tasks ------------------------------------------------

    async def queue_task(
        self, title: str, detail: str = "", *, origin: str = "cognition",
        priority: int = 3,
    ) -> int:
        def _write() -> int:
            # Don't queue the same work twice while it's still outstanding.
            existing = self._db.execute(
                "SELECT id FROM tasks WHERE title = ? AND status IN ('pending','running')",
                (title,),
            ).fetchone()
            if existing:
                return int(existing["id"])
            cur = self._db.execute(
                "INSERT INTO tasks (ts, title, detail, origin, priority)"
                " VALUES (?,?,?,?,?)",
                (time.time(), title, detail, origin, priority),
            )
            self._db.commit()
            return int(cur.lastrowid)

        return await self._run(_write)

    async def next_task(self) -> dict[str, Any] | None:
        """Claim the highest-priority pending task, marking it running."""

        def _write() -> dict[str, Any] | None:
            row = self._db.execute(
                "SELECT * FROM tasks WHERE status = 'pending'"
                " ORDER BY priority ASC, id ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            self._db.execute(
                "UPDATE tasks SET status='running', attempts = attempts + 1,"
                " started = ? WHERE id = ?",
                (time.time(), row["id"]),
            )
            self._db.commit()
            return dict(row)

        return await self._run(_write)

    async def finish_task(self, task_id: int, result: str, status: str = "done") -> None:
        def _write() -> None:
            self._db.execute(
                "UPDATE tasks SET status = ?, result = ?, finished = ? WHERE id = ?",
                (status, result[:8000], time.time(), task_id),
            )
            self._db.commit()

        await self._run(_write)

    async def list_tasks(self, limit: int = 25) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            rows = self._db.execute(
                "SELECT * FROM tasks ORDER BY"
                " CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,"
                " id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

        return await self._run(_read)

    async def requeue_stuck_tasks(self, older_than: float = 1800.0) -> int:
        """Recover tasks orphaned by a restart mid-run."""

        def _write() -> int:
            cutoff = time.time() - older_than
            cur = self._db.execute(
                "UPDATE tasks SET status = 'pending' WHERE status = 'running'"
                " AND started < ? AND attempts < 3",
                (cutoff,),
            )
            self._db.execute(
                "UPDATE tasks SET status = 'failed' WHERE status = 'running'"
                " AND started < ? AND attempts >= 3",
                (cutoff,),
            )
            self._db.commit()
            return cur.rowcount

        return await self._run(_write)

    # -- ventures --------------------------------------------------------

    async def add_venture(self, venture: dict[str, Any]) -> int:
        def _write() -> int:
            now = time.time()
            title = str(venture.get("title", "")).strip()
            existing = self._db.execute(
                "SELECT id FROM ventures WHERE lower(title) = ?", (title.lower(),)
            ).fetchone()
            if existing:
                self._db.execute(
                    "UPDATE ventures SET thesis=?, next_step=?, evidence=?,"
                    " confidence=?, updated=? WHERE id=?",
                    (
                        venture.get("thesis", ""),
                        venture.get("next_step", ""),
                        venture.get("evidence", ""),
                        float(venture.get("confidence", 0.5) or 0.5),
                        now,
                        existing["id"],
                    ),
                )
                self._db.commit()
                return int(existing["id"])
            cur = self._db.execute(
                "INSERT INTO ventures (ts, title, thesis, category, effort, horizon,"
                " confidence, next_step, evidence, status, updated)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    now,
                    title,
                    venture.get("thesis", ""),
                    venture.get("category", "other"),
                    venture.get("effort", "medium"),
                    venture.get("horizon", "weeks"),
                    float(venture.get("confidence", 0.5) or 0.5),
                    venture.get("next_step", ""),
                    venture.get("evidence", ""),
                    venture.get("status", "proposed"),
                    now,
                ),
            )
            self._db.commit()
            return int(cur.lastrowid)

        return await self._run(_write)

    async def list_ventures(self, status: str | None = None) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            if status:
                rows = self._db.execute(
                    "SELECT * FROM ventures WHERE status = ?"
                    " ORDER BY confidence DESC, updated DESC",
                    (status,),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM ventures ORDER BY"
                    " CASE status WHEN 'active' THEN 0 WHEN 'proposed' THEN 1"
                    " ELSE 2 END, confidence DESC, updated DESC"
                ).fetchall()
            return [dict(r) for r in rows]

        return await self._run(_read)

    async def update_venture(
        self, venture_id: int, *, status: str | None = None,
        next_step: str | None = None, evidence: str | None = None,
    ) -> bool:
        def _write() -> bool:
            sets, params = ["updated = ?"], [time.time()]
            for column, value in (
                ("status", status), ("next_step", next_step), ("evidence", evidence),
            ):
                if value is not None:
                    sets.append(f"{column} = ?")
                    params.append(value)
            params.append(venture_id)
            cur = self._db.execute(
                f"UPDATE ventures SET {', '.join(sets)} WHERE id = ?", params
            )
            self._db.commit()
            return cur.rowcount > 0

        return await self._run(_write)

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
                "emails": count("emails"),
                "emails_unhandled": count("emails", "WHERE handled=0"),
                "emails_urgent": count("emails", "WHERE handled=0 AND priority>=3"),
                "drafts_pending": count("drafts", "WHERE status='pending'"),
                "tasks_pending": count("tasks", "WHERE status IN ('pending','running')"),
                "ventures": count("ventures", "WHERE status IN ('proposed','active')"),
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
