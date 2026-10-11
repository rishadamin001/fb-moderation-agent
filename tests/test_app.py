"""Webhook contract tests; no network access or real credentials are required."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from config import Settings
from main import create_app, extract_events
from moderator import Decision, ModerationError


class WebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.settings = Settings(
            openai_api_key="test-openai-key",
            fb_page_access_token="test-page-token",
            fb_verify_token="test-verify-token",
            fb_app_secret="test-app-secret",
            inbox_path=root / "inbox.sqlite3",
            log_path=root / "moderation.log",
        )
        self.moderator = Mock()
        self.moderator.analyze = AsyncMock(
            return_value=Decision(
                is_violating=False,
                recommended_action="none",
                reason="An ordinary comment is allowed.",
            )
        )
        self.moderator.close = AsyncMock()
        self.facebook = Mock()
        for target, instance in (
            ("main.Moderator", self.moderator),
            ("main.FacebookAPI", self.facebook),
        ):
            patcher = patch(target, return_value=instance)
            patcher.start()
            self.addCleanup(patcher.stop)

    def client(self, **overrides) -> TestClient:
        from dataclasses import replace

        return TestClient(create_app(replace(self.settings, **overrides)))

    def signed_headers(self, body: bytes) -> dict[str, str]:
        digest = hmac.new(
            self.settings.fb_app_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return {
            "content-type": "application/json",
            "x-hub-signature-256": f"sha256={digest}",
        }

    def test_verification_returns_exact_plain_text_challenge(self) -> None:
        with self.client() as client:
            response = client.get(
                "/webhook",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": self.settings.fb_verify_token,
                    "hub.challenge": "00012345",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "00012345")
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))

    def test_verification_rejects_missing_or_invalid_parameters(self) -> None:
        valid = {
            "hub.mode": "subscribe",
            "hub.verify_token": self.settings.fb_verify_token,
            "hub.challenge": "123",
        }
        bad_parameters = [
            {},
            {**valid, "hub.mode": "unsubscribe"},
            {**valid, "hub.verify_token": "wrong-token"},
            {**valid, "hub.verify_token": "বাংলা-token"},
            {key: value for key, value in valid.items() if key != "hub.challenge"},
        ]
        with self.client() as client:
            for params in bad_parameters:
                with self.subTest(params=params):
                    self.assertEqual(client.get("/webhook", params=params).status_code, 403)

    def test_verification_without_configured_token_fails_closed(self) -> None:
        with self.client(fb_verify_token="") as client:
            response = client.get(
                "/webhook",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "",
                    "hub.challenge": "123",
                },
            )
        self.assertEqual(response.status_code, 503)

    def test_missing_and_forged_signatures_are_rejected(self) -> None:
        body = b'{"object":"page","entry":[]}'
        signatures = [None, "sha1=abc", "sha256=", "sha256=garbage", "sha256=" + "0" * 64]
        with self.client() as client:
            for signature in signatures:
                headers = {"content-type": "application/json"}
                if signature is not None:
                    headers["x-hub-signature-256"] = signature
                with self.subTest(signature=signature):
                    self.assertEqual(
                        client.post("/webhook", content=body, headers=headers).status_code,
                        403,
                    )

    def test_signature_covers_exact_request_bytes(self) -> None:
        signed_body = b'{"object":"page","entry":[]}'
        with self.client() as client:
            response = client.post(
                "/webhook", content=signed_body + b" ", headers=self.signed_headers(signed_body)
            )
        self.assertEqual(response.status_code, 403)

    def test_delivery_without_app_secret_fails_closed(self) -> None:
        body = b'{"object":"page","entry":[]}'
        with self.client(fb_app_secret="") as client:
            response = client.post("/webhook", content=body, headers=self.signed_headers(body))
        self.assertEqual(response.status_code, 503)

    def test_signed_invalid_json_is_bad_request(self) -> None:
        body = b'{"object":'
        with self.client() as client:
            response = client.post("/webhook", content=body, headers=self.signed_headers(body))
        self.assertEqual(response.status_code, 400)

    def test_oversized_body_is_rejected(self) -> None:
        body = b'{"object":"page","entry":[]}' + b" " * 1024
        with self.client(max_body_bytes=1024) as client:
            response = client.post("/webhook", content=body, headers=self.signed_headers(body))
        self.assertEqual(response.status_code, 413)

    def test_unrelated_object_is_acknowledged(self) -> None:
        body = json.dumps({"object": "instagram", "entry": []}).encode()
        with self.client() as client:
            response = client.post("/webhook", content=body, headers=self.signed_headers(body))
        self.assertEqual(response.status_code, 200)

    def test_signed_nonobject_json_is_rejected_without_server_error(self) -> None:
        with self.client() as client:
            for payload in (None, [], 42, "comment"):
                body = json.dumps(payload).encode()
                with self.subTest(payload=payload):
                    response = client.post(
                        "/webhook", content=body, headers=self.signed_headers(body)
                    )
                    self.assertEqual(response.status_code, 400)

    def test_valid_delivery_is_persisted_and_deduplicated_before_ai_finishes(self) -> None:
        async def blocked_analysis(message):
            await asyncio.Future()

        self.moderator.analyze.side_effect = blocked_analysis
        body = json.dumps(EventExtractionTests.payload([EventExtractionTests.change()])).encode()
        with self.client() as client:
            first = client.post("/webhook", content=body, headers=self.signed_headers(body))
            repeated = client.post("/webhook", content=body, headers=self.signed_headers(body))
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json()["queued"], 1)
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(repeated.json()["queued"], 0)
            stats = client.app.state.inbox.statistics()
            self.assertEqual(stats["pending"] + stats["processing"], 1)
            self.assertTrue(self.settings.inbox_path.exists())
            self.facebook.execute.assert_not_called()

    def test_capacity_failure_requests_retry_without_accepting_second_event(self) -> None:
        async def blocked_analysis(message):
            await asyncio.Future()

        self.moderator.analyze.side_effect = blocked_analysis
        first_body = json.dumps(EventExtractionTests.payload([EventExtractionTests.change()])).encode()
        second_body = json.dumps(
            EventExtractionTests.payload([EventExtractionTests.change("100_102")])
        ).encode()
        with self.client(queue_capacity=1) as client:
            first = client.post(
                "/webhook", content=first_body, headers=self.signed_headers(first_body)
            )
            second = client.post(
                "/webhook", content=second_body, headers=self.signed_headers(second_body)
            )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 503)
            stats = client.app.state.inbox.statistics()
            self.assertEqual(stats["pending"] + stats["processing"], 1)

    def test_missing_ai_credentials_only_rejects_eligible_events(self) -> None:
        event_body = json.dumps(EventExtractionTests.payload([EventExtractionTests.change()])).encode()
        ignored_body = b'{"object":"page","entry":[]}'
        with self.client(openai_api_key="") as client:
            ignored = client.post(
                "/webhook", content=ignored_body, headers=self.signed_headers(ignored_body)
            )
            event = client.post(
                "/webhook", content=event_body, headers=self.signed_headers(event_body)
            )
        self.assertEqual(ignored.status_code, 200)
        self.assertEqual(event.status_code, 503)

    def test_live_actions_require_page_access_token(self) -> None:
        body = json.dumps(EventExtractionTests.payload([EventExtractionTests.change()])).encode()
        with self.client(dry_run=False, fb_page_access_token="") as client:
            response = client.post("/webhook", content=body, headers=self.signed_headers(body))
        self.assertEqual(response.status_code, 503)

    def wait_until(self, predicate, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Background moderation did not reach the expected state")

    def test_accepted_comment_is_moderated_and_logged_in_background(self) -> None:
        self.moderator.analyze.return_value = Decision(
            is_violating=True,
            recommended_action="hide",
            reason="Explicit abusive language.",
        )
        self.facebook.execute.return_value = {
            "comment_id": "100_101",
            "action": "hide",
            "status": "dry_run",
            "response": None,
        }
        message = "An abusive comment for this mocked classification"
        body = json.dumps(
            EventExtractionTests.payload([EventExtractionTests.change(message=message)])
        ).encode()
        with self.client() as client:
            response = client.post("/webhook", content=body, headers=self.signed_headers(body))
            self.assertEqual(response.status_code, 200)
            self.wait_until(lambda: client.app.state.inbox.statistics()["done"] == 1)
        self.moderator.analyze.assert_awaited_once_with(message)
        self.facebook.execute.assert_called_once_with("100_101", "hide")
        records = [json.loads(line) for line in self.settings.log_path.read_text().splitlines()]
        moderation = next(record for record in records if record["event"] == "moderation")
        self.assertEqual(moderation["message"], message)
        self.assertEqual(moderation["sender_id"], "201")
        self.assertEqual(moderation["decision"]["recommended_action"], "hide")
        self.assertEqual(moderation["meta_response"]["status"], "dry_run")

    def test_invalid_ai_output_keeps_comment_untouched_and_requeues(self) -> None:
        self.moderator.analyze.side_effect = ModerationError("Invalid structured classification")
        body = json.dumps(EventExtractionTests.payload([EventExtractionTests.change()])).encode()
        with self.client() as client:
            response = client.post("/webhook", content=body, headers=self.signed_headers(body))
            self.assertEqual(response.status_code, 200)
            self.wait_until(
                lambda: self.moderator.analyze.await_count == 1
                and client.app.state.inbox.statistics()["pending"] == 1
            )
            self.assertEqual(client.app.state.inbox.statistics()["done"], 0)
        self.facebook.execute.assert_not_called()
        records = [json.loads(line) for line in self.settings.log_path.read_text().splitlines()]
        error = next(record for record in records if record["event"] == "moderation_error")
        self.assertIsNone(error["decision"])
        self.assertEqual(error["meta_response"]["status"], "failed")

    def test_allowed_comment_completes_without_graph_action(self) -> None:
        body = json.dumps(
            EventExtractionTests.payload([EventExtractionTests.change(message="The product was disappointing")])
        ).encode()
        with self.client() as client:
            response = client.post("/webhook", content=body, headers=self.signed_headers(body))
            self.assertEqual(response.status_code, 200)
            self.wait_until(lambda: client.app.state.inbox.statistics()["done"] == 1)
        self.facebook.execute.assert_not_called()
        records = [json.loads(line) for line in self.settings.log_path.read_text().splitlines()]
        moderation = next(record for record in records if record["event"] == "moderation")
        self.assertEqual(moderation["decision"]["recommended_action"], "none")
        self.assertEqual(moderation["meta_response"]["status"], "not_required")

    def test_invalid_unicode_comment_does_not_reach_database_or_ai(self) -> None:
        payload = EventExtractionTests.payload([EventExtractionTests.change(message="bad\ud800text")])
        body = json.dumps(payload, ensure_ascii=True).encode()
        with self.client() as client:
            response = client.post("/webhook", content=body, headers=self.signed_headers(body))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "ignored")
        self.moderator.analyze.assert_not_awaited()

    def test_temporary_retry_write_failure_does_not_strand_processing_event(self) -> None:
        self.moderator.analyze.side_effect = ModerationError("Invalid structured classification")
        body = json.dumps(EventExtractionTests.payload([EventExtractionTests.change()])).encode()
        with self.client() as client:
            durable_retry = client.app.state.inbox.retry
            attempts = 0

            def transient_failure(*args, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise sqlite3.OperationalError("database is locked")
                return durable_retry(*args, **kwargs)

            with patch.object(client.app.state.inbox, "retry", side_effect=transient_failure) as retry:
                response = client.post("/webhook", content=body, headers=self.signed_headers(body))
                self.assertEqual(response.status_code, 200)
                self.wait_until(
                    lambda: retry.call_count >= 2
                    and client.app.state.inbox.statistics()["pending"] == 1
                )
                self.assertEqual(client.app.state.inbox.statistics()["processing"], 0)
                self.assertEqual(client.app.state.inbox.statistics()["done"], 0)
        self.moderator.analyze.assert_awaited_once()
        self.facebook.execute.assert_not_called()

    def test_live_worker_forwards_original_comment_for_edit_guard(self) -> None:
        message = "Severe abusive comment for this mocked classification"
        self.moderator.analyze.return_value = Decision(
            is_violating=True,
            recommended_action="delete",
            reason="An explicit credible threat.",
        )
        self.facebook.execute.return_value = {
            "comment_id": "100_101",
            "action": "delete",
            "status": "success",
            "response": {"success": True},
        }
        body = json.dumps(
            EventExtractionTests.payload([EventExtractionTests.change(message=message)])
        ).encode()
        with self.client(dry_run=False) as client:
            response = client.post("/webhook", content=body, headers=self.signed_headers(body))
            self.assertEqual(response.status_code, 200)
            self.wait_until(lambda: client.app.state.inbox.statistics()["done"] == 1)
        self.facebook.execute.assert_called_once_with(
            "100_101", "delete", expected_message=message
        )

    def test_health_check_exposes_dry_run(self) -> None:
        with self.client() as client:
            response = client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertTrue(response.json()["dry_run"])


class EventExtractionTests(unittest.TestCase):
    @staticmethod
    def change(
        comment_id: str = "100_101",
        message: str = "A comment",
        sender_id: str = "201",
        verb: str = "add",
        item: str = "comment",
    ) -> dict:
        return {
            "field": "feed",
            "value": {
                "item": item,
                "verb": verb,
                "comment_id": comment_id,
                "message": message,
                "from": {"id": sender_id},
            },
        }

    @staticmethod
    def payload(changes: list, page_id: str = "301") -> dict:
        return {"object": "page", "entry": [{"id": page_id, "time": 123, "changes": changes}]}

    def test_extracts_comments_across_entries_and_changes(self) -> None:
        payload = self.payload([self.change(), self.change("100_102", "Second")])
        payload["entry"].append(
            {"id": "302", "time": 124, "changes": [self.change("200_101", "Third")]}
        )
        events = extract_events(payload, None)
        self.assertEqual([event.comment_id for event in events], ["100_101", "100_102", "200_101"])
        self.assertEqual([event.message for event in events], ["A comment", "Second", "Third"])
        self.assertEqual(events[0].sender_id, "201")

    def test_optional_page_filter(self) -> None:
        payload = self.payload([self.change()])
        self.assertEqual(len(extract_events(payload, "301")), 1)
        self.assertEqual(extract_events(payload, "999"), [])

    def test_ignores_page_comments_removals_and_post_updates(self) -> None:
        changes = [
            self.change(sender_id="301"),
            self.change(verb="remove"),
            self.change(item="post"),
            {"field": "messages", "value": self.change()["value"]},
        ]
        self.assertEqual(extract_events(self.payload(changes), None), [])

    def test_malformed_structures_are_ignored_without_exception(self) -> None:
        malformed = [
            None,
            [],
            "page",
            {},
            {"object": "page", "entry": None},
            {"object": "page", "entry": [None, 5, "bad", {}]},
            {"object": "page", "entry": [{"id": "301", "changes": None}]},
            self.payload([None, 5, "bad", {}, {"field": "feed", "value": None}]),
        ]
        for payload in malformed:
            with self.subTest(payload=payload):
                self.assertEqual(extract_events(payload, None), [])

    def test_nonstring_message_and_comment_id_are_ignored(self) -> None:
        changes = []
        for value in (None, 1, {}, []):
            comment = self.change()
            comment["value"]["message"] = value
            changes.append(comment)
            comment = self.change()
            comment["value"]["comment_id"] = value
            changes.append(comment)
        self.assertEqual(extract_events(self.payload(changes), None), [])

    def test_nonstring_verbs_are_ignored(self) -> None:
        changes = []
        for verb in (None, 1, [], {}):
            change = self.change()
            change["value"]["verb"] = verb
            changes.append(change)
        self.assertEqual(extract_events(self.payload(changes), None), [])

    def test_edits_are_moderated_and_distinguished_from_retries(self) -> None:
        original = self.payload([self.change()])
        retry = self.payload([self.change()])
        edit = self.payload([self.change(message="Updated comment", verb="edited")])
        original_event = extract_events(original, None)[0]
        retry_event = extract_events(retry, None)[0]
        edit_event = extract_events(edit, None)[0]
        self.assertEqual(original_event.event_id, retry_event.event_id)
        self.assertNotEqual(original_event.event_id, edit_event.event_id)
        self.assertEqual(edit_event.message, "Updated comment")


if __name__ == "__main__":
    unittest.main()
