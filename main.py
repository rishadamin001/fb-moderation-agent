"""Signed Meta webhooks with durable background comment moderation."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import re
import sqlite3
from typing import Any
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from config import Settings
from facebook_api import FacebookAPI, GraphAPIError
from inbox import CommentEvent, Inbox, InboxFull
from moderator import ModerationError, Moderator

COMMENT_ID = re.compile(r"[0-9]+(?:_[0-9]+)?")


def verify_signature(body: bytes, signature: str | None, secret: str) -> bool:
    """Validate exactly the bytes Meta signed, using a constant-time comparison."""
    if not secret or not signature or not re.fullmatch(r"sha256=[0-9a-fA-F]{64}", signature):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature[7:].lower())


def extract_events(payload: Any, page_id: str | None = None) -> list[CommentEvent]:
    """Process comments across all entries; ignore posts, removals and Page replies."""
    if not isinstance(payload, dict) or payload.get("object") != "page":
        return []
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return []
    result: list[CommentEvent] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_page = entry.get("id")
        if not isinstance(entry_page, str) or not re.fullmatch(r"[0-9]+", entry_page):
            continue
        if page_id is not None and entry_page != page_id:
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict) or change.get("field") != "feed":
                continue
            value = change.get("value")
            if not isinstance(value, dict) or value.get("item") != "comment":
                continue
            verb = value.get("verb")
            if not isinstance(verb, str) or verb not in {"add", "edited", "edit"}:
                continue
            comment_id, message = value.get("comment_id"), value.get("message")
            if (
                not isinstance(comment_id, str)
                or len(comment_id) > 150
                or not COMMENT_ID.fullmatch(comment_id)
                or not isinstance(message, str)
                or not message.strip()
            ):
                continue
            try:
                message.encode("utf-8")
            except UnicodeEncodeError:
                # Escaped lone surrogates are valid Python strings but cannot
                # be persisted as UTF-8 or sent to the classification API.
                continue
            author = value.get("from")
            sender_id = author.get("id") if isinstance(author, dict) else value.get("sender_id")
            if not isinstance(sender_id, str) or not re.fullmatch(r"[0-9]+", sender_id):
                sender_id = None
            if sender_id == entry_page:
                continue
            # Delivery retries retain this content key; edits have distinct keys.
            timestamp = value.get("created_time", entry.get("time"))
            if not isinstance(timestamp, (str, int, float)):
                timestamp = None
            identity = json.dumps(
                [entry_page, comment_id, verb, message, sender_id, timestamp],
                ensure_ascii=True,
                separators=(",", ":"),
            )
            result.append(CommentEvent(
                event_id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                page_id=entry_page,
                comment_id=comment_id,
                message=message,
                sender_id=sender_id,
            ))
    return result


def _record(logger: logging.Logger, kind: str, **fields: Any) -> None:
    logger.info(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": kind,
        **fields,
    }, ensure_ascii=True, separators=(",", ":")))


async def _worker(app: FastAPI, stop: asyncio.Event) -> None:
    inbox: Inbox = app.state.inbox
    while not stop.is_set():
        try:
            event = await asyncio.to_thread(inbox.claim)
        except sqlite3.Error:
            _record(app.state.logger, "inbox_error", operation="claim")
            await asyncio.sleep(1)
            continue
        if event is None:
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass
            continue

        decision = None
        try:
            decision = await app.state.moderator.analyze(event.message)
            response: dict[str, Any] = {"status": "not_required"}
            if decision.is_violating and decision.recommended_action != "none":
                options = {} if app.state.settings.dry_run else {"expected_message": event.message}
                response = await asyncio.to_thread(
                    app.state.facebook.execute, event.comment_id, decision.recommended_action, **options
                )
            _record(
                app.state.logger, "moderation", **asdict(event),
                decision=decision.model_dump(), meta_response=response,
                dry_run=app.state.settings.dry_run,
            )
            await asyncio.to_thread(inbox.complete, event.event_id)
        except asyncio.CancelledError:
            # The processing row remains durable and is recovered on next start.
            raise
        except Exception as error:
            # Known clients expose sanitized errors; unknown exception messages
            # can contain credentials and are deliberately excluded from logs.
            detail = str(error) if isinstance(error, (ModerationError, GraphAPIError)) else type(error).__name__
            _record(
                app.state.logger, "moderation_error", **asdict(event),
                decision=decision.model_dump() if decision is not None else None,
                error=detail, meta_response={"status": "failed"},
            )
            # Keep ownership of this row until its retry status is persisted.
            # A transient write failure must not strand it in processing.
            while not stop.is_set():
                try:
                    await asyncio.to_thread(inbox.retry, event.event_id, detail, 5.0, 5)
                    break
                except sqlite3.Error:
                    _record(app.state.logger, "inbox_error", operation="retry", event_id=event.event_id)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=0.5)
                    except asyncio.TimeoutError:
                        pass


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        settings.log_path.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger(f"fb_moderation.{uuid.uuid4().hex}")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = RotatingFileHandler(settings.log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        application.state.logger = logger
        application.state.settings = settings
        application.state.inbox = Inbox(settings.inbox_path, settings.queue_capacity)
        application.state.facebook = FacebookAPI(settings)
        application.state.moderator = None
        application.state.workers = []
        stop = asyncio.Event()
        try:
            await asyncio.to_thread(application.state.inbox.initialize)
            if settings.moderation_ready:
                application.state.moderator = Moderator(settings)
                application.state.workers = [
                    asyncio.create_task(_worker(application, stop), name=f"moderation-{index}")
                    for index in range(settings.worker_count)
                ]
            _record(logger, "startup", dry_run=settings.dry_run, moderation_ready=settings.moderation_ready)
            yield
        finally:
            stop.set()
            if application.state.workers:
                _, pending = await asyncio.wait(application.state.workers, timeout=30)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*application.state.workers, return_exceptions=True)
            if application.state.moderator is not None:
                await application.state.moderator.close()
            handler.close()
            logger.removeHandler(handler)

    application = FastAPI(title="Facebook Page Moderation Agent", lifespan=lifespan)

    @application.get("/healthz")
    async def health() -> dict[str, Any]:
        try:
            queue = await asyncio.to_thread(application.state.inbox.statistics)
        except sqlite3.Error:
            raise HTTPException(status_code=503, detail="Moderation inbox unavailable") from None
        workers = sum(not task.done() for task in application.state.workers)
        return {
            "status": "ok", "dry_run": settings.dry_run,
            "moderation_ready": settings.moderation_ready and workers > 0,
            "workers": workers, "queue": queue,
        }

    @application.get("/webhook", response_class=PlainTextResponse)
    async def verify(request: Request) -> PlainTextResponse:
        if not settings.fb_verify_token:
            raise HTTPException(status_code=503, detail="Webhook verification is not configured")
        mode = request.query_params.get("hub.mode")
        token = request.query_params.get("hub.verify_token", "")
        challenge = request.query_params.get("hub.challenge")
        if mode != "subscribe" or challenge is None or not hmac.compare_digest(
            token.encode("utf-8"), settings.fb_verify_token.encode("utf-8")
        ):
            raise HTTPException(status_code=403, detail="Webhook verification failed")
        return PlainTextResponse(challenge, status_code=200)

    @application.post("/webhook")
    async def receive(request: Request) -> dict[str, Any]:
        if not settings.fb_app_secret:
            raise HTTPException(status_code=503, detail="Webhook signature verification is not configured")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > settings.max_body_bytes:
                raise HTTPException(status_code=413, detail="Webhook payload too large")
        if not verify_signature(bytes(body), request.headers.get("X-Hub-Signature-256"), settings.fb_app_secret):
            raise HTTPException(status_code=403, detail="Invalid webhook signature")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise HTTPException(status_code=400, detail="Invalid JSON payload") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Webhook payload must be an object")
        events = extract_events(payload, settings.fb_page_id)
        if not events:
            return {"status": "ignored"}
        if not settings.moderation_ready or not any(not task.done() for task in application.state.workers):
            raise HTTPException(status_code=503, detail="Moderation credentials or workers are unavailable")
        try:
            added = await asyncio.to_thread(application.state.inbox.enqueue, events)
        except InboxFull:
            raise HTTPException(status_code=503, detail="Moderation inbox is full; retry delivery") from None
        except sqlite3.Error:
            raise HTTPException(status_code=503, detail="Cannot persist webhook; retry delivery") from None
        _record(application.state.logger, "delivery", received=len(events), queued=added)
        # OpenAI and Meta requests run separately, after durable acceptance.
        return {"status": "accepted", "queued": added}

    return application


app = create_app()


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the Facebook moderation webhook server")
    parser.add_argument("--dry-run", action="store_true", help="Override DRY_RUN and prohibit Meta action calls")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    cli_settings = Settings.from_env()
    if args.dry_run:
        cli_settings = replace(cli_settings, dry_run=True)
    uvicorn.run(create_app(cli_settings), host=args.host, port=args.port, access_log=False)
