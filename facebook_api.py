"""Small, synchronous Meta Graph API client; call it outside the event loop."""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import TYPE_CHECKING, Any, Literal

import requests

if TYPE_CHECKING:
    from config import Settings


class GraphAPIError(RuntimeError):
    """A sanitized action error that contains no credential or signed URL."""


class FacebookAPI:
    def __init__(self, settings: Settings) -> None:
        version = settings.fb_graph_version
        match = re.fullmatch(r"v(\d+)\.(\d+)", version)
        if match is None or int(match.group(1)) < 20:
            raise ValueError("FB_GRAPH_VERSION must be v20.0 or newer")
        self._base_url = f"https://graph.facebook.com/{version}"
        self._token = settings.fb_page_access_token
        self._credentials_ready = bool(settings.fb_page_access_token and settings.fb_app_secret)
        self._proof = hmac.new(
            settings.fb_app_secret.encode("utf-8"),
            self._token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self._timeout = settings.api_timeout_seconds
        self._dry_run = settings.dry_run

    def execute(
        self,
        comment_id: str,
        action: Literal["hide", "delete"],
        *,
        expected_message: str | None = None,
    ) -> dict[str, Any]:
        """Apply one action or record a dry run; requests are never silently retried."""
        if not isinstance(comment_id, str) or not re.fullmatch(r"[0-9]+(?:_[0-9]+)?", comment_id):
            raise ValueError("comment_id must be a numeric Meta comment identifier")
        if len(comment_id) > 150:
            raise ValueError("comment_id is too long")
        if action not in ("hide", "delete"):
            raise ValueError("action must be hide or delete")

        record: dict[str, Any] = {"comment_id": comment_id, "action": action}
        if self._dry_run:
            return {**record, "status": "dry_run", "response": None}
        if not self._credentials_ready:
            raise GraphAPIError("Meta Page token and App Secret are required for live actions")

        if expected_message is not None:
            if not isinstance(expected_message, str):
                raise ValueError("expected_message must be a string")
            current = self._request(
                "GET",
                comment_id,
                params={"fields": "message", "appsecret_proof": self._proof},
            )
            if not isinstance(current.get("message"), str):
                raise GraphAPIError("Meta Graph API did not return the current comment text")
            if current["message"] != expected_message:
                return {
                    **record,
                    "status": "skipped_changed",
                    "response": {"reason": "Comment text changed before action"},
                }

        method = "POST" if action == "hide" else "DELETE"
        request_options: dict[str, Any] = {}
        if action == "hide":
            request_options["data"] = {"is_hidden": "true", "appsecret_proof": self._proof}
        else:
            request_options["params"] = {"appsecret_proof": self._proof}

        payload = self._request(method, comment_id, **request_options)
        if payload.get("success") is not True:
            raise GraphAPIError("Meta Graph API did not confirm action success")

        # Action endpoints have a success boolean. Exclude unexpected response
        # fields so logs cannot accidentally capture reflected credentials.
        return {**record, "status": "success", "response": {"success": True}}

    def _request(self, method: str, comment_id: str, **request_options: Any) -> dict[str, Any]:
        try:
            response = requests.request(
                method,
                f"{self._base_url}/{comment_id}",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=self._timeout,
                allow_redirects=False,
                **request_options,
            )
        except requests.RequestException:
            # Exception messages may contain the URL and its appsecret_proof.
            raise GraphAPIError("Meta Graph API request failed") from None

        try:
            payload = response.json()
        except ValueError:
            raise GraphAPIError(f"Meta Graph API returned invalid JSON (HTTP {response.status_code})") from None

        if not 200 <= response.status_code < 300:
            details = ""
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                # Only numerical diagnostic fields are safe to include in logs.
                for field in ("code", "error_subcode"):
                    value = error.get(field)
                    if type(value) is int:
                        details += f", {field}={value}"
            raise GraphAPIError(f"Meta Graph API rejected the request (HTTP {response.status_code}{details})")

        if not isinstance(payload, dict):
            raise GraphAPIError("Meta Graph API returned an invalid response object")
        return payload
