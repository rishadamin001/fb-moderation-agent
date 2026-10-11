"""Durable, single-worker inbox for accepted Facebook comment events.

Methods use short-lived SQLite connections and are safe to call in separate
``asyncio.to_thread`` invocations. Delivery and execution are at least once:
after a restart, previously claimed events are available for processing again.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import math
from pathlib import Path
import sqlite3
import time


@dataclass(frozen=True)
class CommentEvent:
    event_id: str
    page_id: str
    comment_id: str
    message: str
    sender_id: str | None


class InboxFull(Exception):
    """Accepting this batch would exceed the pending-event limit."""


class Inbox:
    """Persist comment events and claim them for one application worker.

    ``initialize`` recovers interrupted work and therefore must run once before
    a single process starts its worker. Multiple Uvicorn worker processes must
    not share this inbox. Completed/failed records are retained for seven days
    for delivery deduplication and pruned on startup.
    """

    RETENTION_SECONDS = 7 * 24 * 60 * 60

    def __init__(self, path: Path | str, max_pending: int = 10_000) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self.path = Path(path)
        self.max_pending = max_pending

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        # Explicitly preserve committed events across an abrupt host shutdown.
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbox (
                    event_id TEXT PRIMARY KEY,
                    page_id TEXT NOT NULL,
                    comment_id TEXT NOT NULL,
                    message TEXT NOT NULL,
                    sender_id TEXT,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'processing', 'done', 'failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS inbox_pending
                    ON inbox (status, next_attempt_at, created_at);
                """
            )
            with connection:
                connection.execute(
                    "UPDATE inbox SET status = 'pending', next_attempt_at = ?, "
                    "updated_at = ? WHERE status = 'processing'",
                    (now, now),
                )
                connection.execute(
                    "DELETE FROM inbox WHERE status IN ('done', 'failed') "
                    "AND updated_at < ?",
                    (now - self.RETENTION_SECONDS,),
                )

    def enqueue(self, events: list[CommentEvent]) -> int:
        """Accept an entire batch or roll it back; existing IDs are ignored."""
        if not events:
            return 0
        now = time.time()
        added = 0
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            occupied = connection.execute(
                "SELECT COUNT(*) FROM inbox WHERE status IN ('pending', 'processing')"
            ).fetchone()[0]
            for event in events:
                cursor = connection.execute(
                    "INSERT INTO inbox "
                    "(event_id, page_id, comment_id, message, sender_id, "
                    "next_attempt_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(event_id) DO NOTHING",
                    (
                        event.event_id,
                        event.page_id,
                        event.comment_id,
                        event.message,
                        event.sender_id,
                        now,
                        now,
                        now,
                    ),
                )
                added += cursor.rowcount
                if occupied + added > self.max_pending:
                    raise InboxFull("The moderation inbox is at capacity")
        return added

    def claim(self) -> CommentEvent | None:
        """Atomically claim the oldest event whose retry delay has elapsed."""
        now = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT event_id, page_id, comment_id, message, sender_id "
                "FROM inbox WHERE status = 'pending' AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at, created_at, event_id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE inbox SET status = 'processing', attempts = attempts + 1, "
                "updated_at = ? WHERE event_id = ?",
                (now, row["event_id"]),
            )
            return CommentEvent(**dict(row))

    def complete(self, event_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE inbox SET status = 'done', updated_at = ?, last_error = NULL "
                "WHERE event_id = ? AND status = 'processing'",
                (time.time(), event_id),
            )

    def retry(
        self,
        event_id: str,
        error: str,
        delay_seconds: float,
        max_attempts: int = 5,
    ) -> None:
        """Requeue a claimed event, or mark it failed after its final attempt.

        Each successful ``claim`` increments the stored attempt count. The error
        must already be scrubbed of credentials by the caller; it is truncated
        to bound storage.
        """
        if not math.isfinite(delay_seconds) or delay_seconds < 0:
            raise ValueError("delay_seconds must be finite and nonnegative")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        now = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE inbox SET "
                "status = CASE WHEN attempts >= ? THEN 'failed' ELSE 'pending' END, "
                "next_attempt_at = ?, updated_at = ?, last_error = ? "
                "WHERE event_id = ? AND status = 'processing'",
                (max_attempts, now + delay_seconds, now, error[:2000], event_id),
            )

    def statistics(self) -> dict[str, int]:
        result = {status: 0 for status in ("pending", "processing", "done", "failed")}
        with closing(self._connect()) as connection:
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM inbox GROUP BY status"
            ):
                result[row["status"]] = row["count"]
        return result
