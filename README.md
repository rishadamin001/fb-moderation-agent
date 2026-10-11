# Facebook Page moderation agent

A FastAPI server that verifies Meta Webhooks, accepts signed Page comment events, asks OpenAI for a structured moderation decision, and hides or deletes violating comments through the Meta Graph API. Accepted comments enter a durable local queue before the webhook returns HTTP 200; analysis and Graph requests run in background workers.

Dry-run mode is enabled by default. It runs OpenAI analysis and records the proposed Graph action without hiding or deleting anything. Legitimate criticism and negative sentiment alone are not violations; the model is instructed to avoid treating unverified allegations as proven fake reviews.

## Files

| File | Purpose |
| --- | --- |
| `main.py` | Webhook verification, signature validation, durable queue, background processing, and health endpoint |
| `moderator.py` | OpenAI classification and strict decision validation |
| `facebook_api.py` | Graph API hide/delete requests |
| `config.py` | Environment configuration |
| `inbox.py` | SQLite event persistence, delivery deduplication, and retry state |
| `.env.example` | Configuration template without credentials |
| `requirements.txt` | Runtime dependencies |
| `requirements-dev.txt` | Runtime dependencies plus the HTTP test client |
| `tests/` | Automated tests using mocked external services |

## Local setup

Use Python 3.11 or later. Run commands from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

On Windows, activate the environment with `.venv\Scripts\activate` instead. Edit `.env` and supply:

| Variable | Meaning |
| --- | --- |
| `OPENAI_API_KEY` | API key for the OpenAI project used for classification |
| `FB_PAGE_ACCESS_TOKEN` | Page access token with the permissions needed for this Page |
| `FB_VERIFY_TOKEN` | A random string you choose and enter identically in the Meta callback configuration |
| `FB_APP_SECRET` | App Secret from Meta App Dashboard → App settings → Basic |
| `FB_PAGE_ID` | Optional Page ID to restrict delivery to one Page |
| `OPENAI_MODEL` | Optional model supporting structured outputs; default `gpt-4o-mini` |
| `FB_GRAPH_VERSION` | Optional supported Graph version; default `v24.0`, configurable for v20.0 or newer |
| `DRY_RUN` | `true` by default; set `false` only when ready for actual moderation |

An access token is not the verification token. Do not commit `.env` or paste credentials into webhook payloads, URLs, issues, or logs. Review additional operational settings documented in `.env.example` before deploying.

Start the server:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --no-access-log
```

Alternatively, the command-line launcher forces dry-run mode for this process:

```bash
python main.py --dry-run
```

Check that the server is running:

```bash
curl --fail http://localhost:8000/healthz
```

`/healthz` reports process health, queue counts, and `moderation_ready`. The service can start before credentials are supplied, but eligible comment deliveries return HTTP 503 until moderation credentials and workers are available. `moderation_ready=true` confirms local configuration, not remote token permissions or API access. Inspect `queue.failed` and the log for events that exhausted their five processing attempts; they need investigation and deliberate replay.

Use one Uvicorn process (`--workers 1`, the default). `WORKER_COUNT` controls background tasks within that process. The queue is stored in `.local/moderation.sqlite3`; accepted events survive process restarts. Background workers retry temporary failures and the queue deduplicates deliveries. Completed/failed records are retained for seven days for deduplication and pruned at startup. Execution is at least once: a crash after a Graph action but before completion is recorded can cause a retry. Queue storage must remain writable and persistent.

## Expose localhost with Ngrok

Install Ngrok from [ngrok.com/download](https://ngrok.com/download), authenticate it according to its setup instructions, and start a tunnel in another terminal:

```bash
ngrok http 8000
```

Copy the public **HTTPS** forwarding URL. If the URL is `https://example.ngrok-free.app`, the webhook callback is:

```text
https://example.ngrok-free.app/webhook
```

Leave both Uvicorn and Ngrok running. When your tunnel URL changes, update the callback in the Meta dashboard. A cloud deployment can use its own public HTTPS URL instead of Ngrok.

## Configure Meta and subscribe the Page

Meta's dashboard labels vary with app type and selected use case. The configuration requires both an app webhook subscription and a subscription of the individual Page to that app.

1. Open [Meta for Developers](https://developers.facebook.com/apps/) and create or select an app supporting Facebook Page management. Select the appropriate business/Page management use case when creating the app. Associate the app with the business when required by the dashboard.
2. Under **App settings → Basic**, obtain the App Secret and save it as `FB_APP_SECRET` in `.env`. Choose `FB_VERIFY_TOKEN` yourself; Meta does not issue this value.
3. Configure the supported Facebook Login/Page authorization flow for the app. Authenticate a person who has the required management access to the target Page, request the necessary permissions, and obtain the **Page access token**. Where your app supports it, Graph API Explorer can obtain a user token, list authorized Pages with `GET /me/accounts`, and return the Page token. Save the Page token as `FB_PAGE_ACCESS_TOKEN` and the Page's numeric ID as `FB_PAGE_ID`. Do not use an App access token in place of the Page token.
4. Request `pages_manage_metadata` and `pages_show_list` for Page `feed` webhook setup, `pages_read_engagement` and `pages_read_user_content` for reading applicable Page content, and `pages_manage_engagement` for hiding/deleting eligible Page comments. Meta's Page Webhooks documentation lists both `pages_manage_metadata` and `pages_show_list` as required for `feed` webhooks. Permission availability, Page roles, and content ownership all affect what the app can read or moderate. Confirm requirements in the [Page Webhooks documentation](https://developers.facebook.com/docs/graph-api/webhooks/getting-started/webhooks-for-pages) and the reference for the selected Graph API version.
5. Add or open **Webhooks** in the app dashboard, choose the **Page** object, and select **Edit subscription** or **Subscribe to this object**. Enter the public HTTPS URL ending in `/webhook` and the exact value of `FB_VERIFY_TOKEN`. Click **Verify and save**. Meta sends `hub.mode=subscribe`, `hub.verify_token`, and `hub.challenge`; this server returns the matching challenge as plain text with HTTP 200.
6. In the Page object's available fields, find **feed** and click **Subscribe**. The `feed` field carries Page feed changes, including comment additions and edits. Select a supported Graph API version in the dashboard and use the same supported version in `FB_GRAPH_VERSION`; versions eventually expire.
7. Subscribe the actual Page to your app with `POST /{PAGE_ID}/subscribed_apps`, including `subscribed_fields=feed` and the Page access token. Run the following from the repository root with the virtual environment active. It reads credentials from `.env` and sends the token in an Authorization header:

   ```bash
   python - <<'PY'
   import hashlib
   import hmac
   import os
   import requests
   from dotenv import load_dotenv

   load_dotenv(".env")
   page_id = os.environ["FB_PAGE_ID"]
   token = os.environ["FB_PAGE_ACCESS_TOKEN"]
   secret = os.environ["FB_APP_SECRET"]
   version = os.getenv("FB_GRAPH_VERSION", "v24.0")
   proof = hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()
   response = requests.post(
       f"https://graph.facebook.com/{version}/{page_id}/subscribed_apps",
       headers={"Authorization": f"Bearer {token}"},
       data={"subscribed_fields": "feed", "appsecret_proof": proof},
       timeout=20,
   )
   print("Page subscription HTTP status:", response.status_code)
   if not response.ok:
       raise SystemExit("Subscription failed: check Page access, token permissions, and API version.")
   print("Page subscribed:", response.json().get("success", False))
   PY
   ```

8. Use the dashboard's **Test** button for `feed` to check delivery, then add a real test comment to a post on the subscribed Page with `DRY_RUN=true`. Review `webhook_moderation.log` for the extracted comment, classification, and proposed action. Dashboard test payloads may have a different shape or lack a real comment ID; a successful test delivery does not prove access to actual comments.
9. For real users outside app roles, complete applicable business verification and App Review, obtain Advanced Access to required permissions, and enable the app's live mode where the dashboard requires it. Development mode and role-only testing do not establish that the app can moderate public comments.
10. After validating authorized live comments in dry-run mode, set `DRY_RUN=false` in `.env` and restart the server. Deletion is permanent; consider leaving the service in dry-run mode while reviewing classifications. The model can recommend `hide` for reversible moderation or `delete` for clear severe violations.

The app must remain subscribed, its token must remain valid, and the Page must remain accessible to the authorizing account. Re-authorize or replace the Page token through Meta's supported process if access expires or is revoked.

## Webhook behavior and local testing

`GET /webhook` accepts the three Meta verification parameters. A token mismatch or unsupported mode fails verification. The challenge response is plain text, not JSON.

`POST /webhook` requires `X-Hub-Signature-256: sha256=<hex digest>`, calculated with `FB_APP_SECRET` over the **exact raw request body**. Requests with a missing or invalid signature are rejected before processing. Valid Page comment events are durably queued and acknowledged without waiting for OpenAI or Graph requests. If the event cannot be persisted, the server reports failure so Meta can retry delivery.

The handler scans every entry/change and processes `feed` changes whose `item` is `comment` and whose `verb` is `add` or `edited`. Other changes are acknowledged without moderation. Comments authored by the configured Page are skipped. Hiding/deleting a comment can generate more webhook events, so action notifications are excluded from the moderation pipeline.

To exercise a signed comment delivery locally, keep `DRY_RUN=true` and run this script with the server running. This synthetic comment is sent to OpenAI but will not cause a live Graph action in dry-run mode:

```bash
python - <<'PY'
import hashlib
import hmac
import json
import os
import time
import requests
from dotenv import load_dotenv

load_dotenv(".env")
payload = {
    "object": "page",
    "entry": [{
        "id": os.getenv("FB_PAGE_ID") or "123456789",
        "time": int(time.time()),
        "changes": [{
            "field": "feed",
            "value": {
                "item": "comment",
                "verb": "add",
                "comment_id": f"123456789_{time.time_ns()}",
                "message": "The delivery was late, and I would like a refund.",
                "from": {"id": "987654321", "name": "Local test user"},
            },
        }],
    }],
}
body = json.dumps(payload, separators=(",", ":")).encode()
signature = hmac.new(os.environ["FB_APP_SECRET"].encode(), body, hashlib.sha256).hexdigest()
response = requests.post(
    "http://localhost:8000/webhook",
    data=body,
    headers={"Content-Type": "application/json", "X-Hub-Signature-256": f"sha256={signature}"},
    timeout=10,
)
print("Webhook HTTP status:", response.status_code)
print(response.text)
PY
```

Run automated tests without live Meta or OpenAI calls:

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

## Classification, actions, and logs

OpenAI returns exactly `is_violating` (boolean), `recommended_action` (`delete`, `hide`, or `none`), and `reason` (string). The system prompt covers predatory conduct, harassment, threats, abusive language, and clear spam or scam behavior. Comment text is treated as untrusted content, not model instructions. A comment alone cannot establish whether a criticism is baseless or whether its author represents a competitor, so the model is told not to invent those facts. Invalid or inconsistent model output does not authorize a Graph action.

For actionable decisions, the wrapper sends:

- `DELETE https://graph.facebook.com/{FB_GRAPH_VERSION}/{comment_id}` to delete.
- `POST https://graph.facebook.com/{FB_GRAPH_VERSION}/{comment_id}` with `is_hidden=true` to hide.

Both calls require a valid Page token and permission to manage that specific comment. Successful webhook delivery does not guarantee action permission. API errors are logged and temporary failures are retried. Completion depends on API latency and availability; the HTTP acknowledgement is immediate after durable acceptance, while moderation takes place asynchronously.

Before a live action, the agent reads the comment's current `message` from Graph and skips the action if it differs from the classified text. This prevents queued decisions from acting on edits already visible to Graph. Meta does not offer an atomic compare-and-delete here, so an edit during the brief interval between that check and the action can still race with moderation.

`webhook_moderation.log` contains JSON lines with the incoming event, original comment text, AI decision, and action outcome/Meta response. API tokens and app secrets are not logged. Comment text and sender IDs can be personal data: restrict access, set a retention policy, and protect persisted logs and queue files.

## Cloud deployment

Install `requirements.txt`, inject configuration through the host's secret/environment settings, and launch `uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1 --no-access-log`. Disabling access logs avoids recording the verification token in Meta's verification query string; apply the same query-string redaction to your reverse proxy. Use the host's public HTTPS endpoint as the callback URL. Ngrok is unnecessary when the host already provides HTTPS. Configure `/healthz` as the health check and allow outbound HTTPS to `api.openai.com` and `graph.facebook.com`.

Mount persistent writable storage for `.local/moderation.sqlite3` and the log location, and restart failed processes. A platform that discards the filesystem on restart will also discard the local queue unless storage is mounted. This implementation is intended for a single running server process; use a shared queue with explicit coordination before deploying multiple replicas. SQLite and the in-process worker are not a distributed job system.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Callback verification fails | Public HTTPS URL, exact `/webhook` path, running server, and matching `FB_VERIFY_TOKEN` |
| Signature is rejected | Correct App Secret, unmodified raw JSON body, and `X-Hub-Signature-256` header |
| No real comments arrive | Both app `feed` subscription and Page `subscribed_apps` registration, app mode, Page access, and token permissions |
| OpenAI analysis fails | API key/project access, billing/quota, chosen model's structured-output support, and outbound network access |
| Hide/delete fails | `DRY_RUN`, supported Graph version, token validity, `pages_manage_engagement`, and access to that comment |
| Queue cannot accept events | Writable persistent storage, available disk space, and a single server process |

Keep secrets out of diagnostic output. Review log entries to distinguish successful delivery, classification, dry-run decisions, and actual Graph actions.
