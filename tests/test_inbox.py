"""Tests for persistence and the webhook acknowledgment boundary."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from inbox import CommentEvent, Inbox, InboxFull


class InboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "data" / "inbox.sqlite3"
        self.inbox = Inbox(self.path)
        self.inbox.initialize()

    @staticmethod
    def event(event_id: str) -> CommentEvent:
        return CommentEvent(event_id, "page", "comment-" + event_id, "A comment", None)

    def test_deduplication_survives_completion_and_reopening(self) -> None:
        event = self.event("one")
        self.assertEqual(self.inbox.enqueue([event, event]), 1)
        self.assertEqual(self.inbox.claim(), event)
        self.inbox.complete(event.event_id)
        reopened = Inbox(self.path)
        reopened.initialize()
        self.assertEqual(reopened.enqueue([event]), 0)
        self.assertIsNone(reopened.claim())
        self.assertEqual(reopened.statistics()["done"], 1)

    def test_capacity_failure_rolls_back_whole_batch(self) -> None:
        self.inbox = Inbox(self.path, max_pending=2)
        existing = self.event("existing")
        self.inbox.enqueue([existing])
        self.inbox.claim()
        with self.assertRaises(InboxFull):
            self.inbox.enqueue([self.event("two"), self.event("three")])
        self.assertEqual(
            self.inbox.statistics(),
            {"pending": 0, "processing": 1, "done": 0, "failed": 0},
        )
        self.assertEqual(self.inbox.enqueue([existing, self.event("two")]), 1)
        self.assertEqual(self.inbox.enqueue([existing, self.event("two")]), 0)

    def test_restart_recovers_claimed_event(self) -> None:
        event = self.event("recover")
        self.inbox.enqueue([event])
        self.assertEqual(self.inbox.claim(), event)
        recovered = Inbox(self.path)
        recovered.initialize()
        self.assertEqual(recovered.claim(), event)

    def test_retry_delay_and_attempt_limit(self) -> None:
        event = self.event("retry")
        with patch("inbox.time.time", return_value=1000):
            self.inbox.enqueue([event])
            self.assertEqual(self.inbox.claim(), event)
            self.inbox.retry(event.event_id, "temporary failure", 30, max_attempts=2)
            self.assertIsNone(self.inbox.claim())
        with patch("inbox.time.time", return_value=1029):
            self.assertIsNone(self.inbox.claim())
        with patch("inbox.time.time", return_value=1030):
            self.assertEqual(self.inbox.claim(), event)
            self.inbox.retry(event.event_id, "final failure", 30, max_attempts=2)
        self.assertEqual(self.inbox.statistics()["failed"], 1)
        self.assertIsNone(self.inbox.claim())

    def test_concurrent_claims_do_not_claim_same_event(self) -> None:
        events = [self.event(str(number)) for number in range(20)]
        self.inbox.enqueue(events)
        with ThreadPoolExecutor(max_workers=8) as executor:
            claimed = list(executor.map(lambda _: self.inbox.claim(), range(24)))
        event_ids = [event.event_id for event in claimed if event is not None]
        self.assertEqual(len(event_ids), 20)
        self.assertEqual(len(set(event_ids)), 20)
        self.assertEqual(self.inbox.statistics()["processing"], 20)

    def test_startup_prunes_only_old_terminal_events(self) -> None:
        old = self.event("old")
        pending = self.event("pending")
        current = self.event("current")
        self.inbox.enqueue([old, pending, current])
        # Use direct fixture state so this check does not depend on claim order.
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE inbox SET status = 'done', updated_at = 1 WHERE event_id = ?",
                (old.event_id,),
            )
            connection.execute(
                "UPDATE inbox SET status = 'done' WHERE event_id = ?",
                (current.event_id,),
            )
        self.inbox.initialize()
        self.assertEqual(self.inbox.statistics()["pending"], 1)
        self.assertEqual(self.inbox.statistics()["done"], 1)
        self.assertEqual(self.inbox.enqueue([old]), 1)
        self.assertEqual(self.inbox.enqueue([current]), 0)


if __name__ == "__main__":
    unittest.main()
