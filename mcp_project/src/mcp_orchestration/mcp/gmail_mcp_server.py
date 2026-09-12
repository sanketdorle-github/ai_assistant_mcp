"""Custom Gmail MCP server.

Exposes Gmail as MCP tools over stdio, using the standard, generally-available
Gmail API (google-api-python-client) + OAuth 2.0 — NOT Google's Workspace
Developer Preview "Gmail MCP" (gmailmcp.googleapis.com), which requires
special enrollment and was the source of the 404s in the old setup.

    AI Agent -> MCPClientManager (stdio) -> THIS SERVER -> Gmail API -> Gmail

Run standalone for a quick manual check:
    python -m mcp_orchestration.mcp.gmail_mcp_server

Normally it's launched over stdio by MCPClientManager.connect_stdio_server,
via the "gmail" entry produced in config.py.

Auth: reuses the credentials already written by the existing
`/auth/gmail/login` -> `/auth/gmail/callback` flow in gmail_oauth.py — this
server does NOT re-implement OAuth, it only *consumes* the stored token and
refreshes it via google-auth when it's near expiry.

Credentials are stored per-user, encrypted, in the `gmail_credentials`
MongoDB collection (see repositories/gmail_credentials.py) rather than a
local file — this process is a separate subprocess from the FastAPI app, so
it opens its own short-lived pymongo connection using the env vars below
rather than sharing the app's `Database` singleton.

Required environment variables:
    GOOGLE_CLIENT_ID       (falls back to GMAIL_CLIENT_ID for compatibility)
    GOOGLE_CLIENT_SECRET   (falls back to GMAIL_CLIENT_SECRET)
    GOOGLE_REDIRECT_URI    (falls back to GMAIL_REDIRECT_URI)
    GOOGLE_USER_ID         which user's stored credentials to load
    MONGO_URI, DB_NAME     how to reach the same Mongo the API uses
    TOKEN_ENCRYPTION_KEY   symmetric key to decrypt the stored tokens
                            (see core/token_encryption.py)
"""

import base64
import logging
import os
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pymongo import MongoClient

from mcp.server.fastmcp import FastMCP

from mcp_orchestration.core.token_encryption import decrypt, encrypt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp_orchestration.mcp.gmail_mcp_server")

# Minimum scopes needed for the tools below. gmail.readonly covers all read
# operations; gmail.compose covers creating drafts AND sending mail (Gmail's
# `compose` scope includes send). We deliberately avoid the much broader
# `gmail.modify` unless you add label-mutation tools later.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]

mcp = FastMCP("gmail")

_mongo_client: Optional[MongoClient] = None


def _credentials_collection():
    """Lazily open (and cache) this subprocess's own short-lived Mongo
    connection - separate from the FastAPI app's, since this runs as its
    own process over stdio."""
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(os.environ["MONGO_URI"])
    db_name = os.getenv("DB_NAME", "mcp_orchestration")
    return _mongo_client[db_name]["gmail_credentials"]


def _user_id() -> Optional[str]:
    return os.getenv("GOOGLE_USER_ID")


def _client_id() -> Optional[str]:
    return os.getenv("GOOGLE_CLIENT_ID") or os.getenv("GMAIL_CLIENT_ID")


def _client_secret() -> Optional[str]:
    return os.getenv("GOOGLE_CLIENT_SECRET") or os.getenv("GMAIL_CLIENT_SECRET")


class GmailAuthError(Exception):
    """Raised when we can't produce a usable, authorized Gmail client."""


def _load_credentials() -> Credentials:
    """Load the credentials written by the existing /auth/gmail/login flow
    (stored encrypted in Mongo, keyed by GOOGLE_USER_ID) and refresh them if
    needed. Never prints the token itself anywhere."""
    user_id = _user_id()
    if not user_id:
        raise GmailAuthError(
            "No GOOGLE_USER_ID configured for this Gmail MCP connection."
        )

    data = _credentials_collection().find_one({"user_id": user_id})
    if not data:
        raise GmailAuthError(
            "No Gmail credentials found for this user. Please authenticate "
            "first at /auth/gmail/login."
        )

    try:
        access_token = decrypt(data["access_token"]) if data.get("access_token") else None
        refresh_token = decrypt(data["refresh_token"]) if data.get("refresh_token") else None
    except Exception as e:
        raise GmailAuthError(f"Could not decrypt stored Gmail credentials: {e}")

    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id") or _client_id(),
        client_secret=_client_secret(),
        scopes=SCOPES,
    )

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(GoogleAuthRequest())
            except RefreshError as e:
                raise GmailAuthError(
                    "Gmail token expired and could not be refreshed. "
                    "Please re-authenticate at /auth/gmail/login."
                ) from e
            # Persist the refreshed access token back to the same Mongo doc
            # so the FastAPI side (/auth/gmail/status) stays consistent.
            try:
                _credentials_collection().update_one(
                    {"user_id": user_id},
                    {
                        "$set": {
                            "access_token": encrypt(creds.token),
                            "updated_at": datetime.now(timezone.utc),
                        }
                    },
                )
            except Exception as e:
                logger.warning(f"Could not persist refreshed token: {e}")
        else:
            raise GmailAuthError(
                "No valid Gmail credentials. Please authenticate at "
                "/auth/gmail/login."
            )

    return creds


def _gmail_service():
    creds = _load_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _handle_http_error(e: HttpError, context: str) -> str:
    status_code = e.resp.status if hasattr(e, "resp") else None
    if status_code == 401:
        return (
            f"{context}: Gmail authorization is invalid or expired. "
            "Please re-authenticate at /auth/gmail/login."
        )
    if status_code == 403:
        return (
            f"{context}: Access forbidden — the authorized Gmail account "
            "may lack permission, or the requested scope wasn't granted."
        )
    if status_code == 404:
        return f"{context}: The requested Gmail resource was not found."
    if status_code == 429:
        return f"{context}: Gmail API rate limit exceeded. Please retry shortly."
    logger.error(f"{context}: Gmail API error: {e}")
    return f"{context}: Gmail API error ({status_code})."


def _header(headers: List[Dict[str, str]], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _extract_body(payload: Dict[str, Any]) -> str:
    """Walk a Gmail message payload (which may be multipart/nested) and pull
    out the best available plain-text body."""
    if payload is None:
        return ""

    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")

    if mime_type == "text/plain" and body_data:
        return base64.urlsafe_b64decode(body_data.encode("utf-8")).decode(
            "utf-8", errors="replace"
        )

    parts = payload.get("parts") or []
    # First pass: look for a direct text/plain part.
    for part in parts:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(
                part["body"]["data"].encode("utf-8")
            ).decode("utf-8", errors="replace")
    # Second pass: recurse into nested multipart parts (e.g. multipart/alternative
    # inside multipart/mixed).
    for part in parts:
        text = _extract_body(part)
        if text:
            return text

    # Fall back to HTML if no plain text was found anywhere.
    if mime_type == "text/html" and body_data:
        return base64.urlsafe_b64decode(body_data.encode("utf-8")).decode(
            "utf-8", errors="replace"
        )
    for part in parts:
        if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(
                part["body"]["data"].encode("utf-8")
            ).decode("utf-8", errors="replace")

    return ""


def _extract_attachments(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    attachments = []

    def walk(part: Dict[str, Any]):
        filename = part.get("filename")
        body = part.get("body", {})
        if filename and body.get("attachmentId"):
            attachments.append(
                {
                    "filename": filename,
                    "mime_type": part.get("mimeType", ""),
                    "size": body.get("size", 0),
                    "attachment_id": body.get("attachmentId"),
                }
            )
        for sub in part.get("parts") or []:
            walk(sub)

    walk(payload or {})
    return attachments


def _message_summary(msg: Dict[str, Any]) -> Dict[str, Any]:
    headers = msg.get("payload", {}).get("headers", [])
    return {
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "subject": _header(headers, "Subject"),
        "sender": _header(headers, "From"),
        "date": _header(headers, "Date"),
        "snippet": msg.get("snippet", ""),
    }


def _message_full(msg: Dict[str, Any]) -> Dict[str, Any]:
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    return {
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "sender": _header(headers, "From"),
        "recipients": _header(headers, "To"),
        "subject": _header(headers, "Subject"),
        "date": _header(headers, "Date"),
        "body": _extract_body(payload),
        "labels": msg.get("labelIds", []),
        "attachments": _extract_attachments(payload),
    }


@mcp.tool()
def search_emails(query: str, max_results: int = 10) -> Dict[str, Any]:
    """Search Gmail using standard Gmail search syntax (e.g. 'from:x@y.com',
    'subject:invoice', 'is:unread newer_than:7d'). Returns lightweight
    message summaries — call get_email for the full body of a specific
    message.
    """
    try:
        service = _gmail_service()
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        ids = [m["id"] for m in resp.get("messages", [])]
        messages = []
        for mid in ids:
            full = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=mid,
                    format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                )
                .execute()
            )
            messages.append(_message_summary(full))
        return {
            "messages": messages,
            "result_size_estimate": resp.get("resultSizeEstimate", len(messages)),
        }
    except GmailAuthError as e:
        return {"error": str(e)}
    except HttpError as e:
        return {"error": _handle_http_error(e, "search_emails")}
    except Exception as e:
        logger.error(f"search_emails failed: {e}", exc_info=True)
        return {"error": f"search_emails failed: {e}"}


@mcp.tool()
def get_email(message_id: str) -> Dict[str, Any]:
    """Fetch a single Gmail message by id, including full body (handles
    multipart messages) and attachment metadata (not attachment bytes)."""
    try:
        service = _gmail_service()
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
        return _message_full(msg)
    except GmailAuthError as e:
        return {"error": str(e)}
    except HttpError as e:
        return {"error": _handle_http_error(e, "get_email")}
    except Exception as e:
        logger.error(f"get_email failed: {e}", exc_info=True)
        return {"error": f"get_email failed: {e}"}


@mcp.tool()
def get_thread(thread_id: str) -> Dict[str, Any]:
    """Fetch every message in a Gmail thread, in order."""
    try:
        service = _gmail_service()
        thread = (
            service.users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute()
        )
        messages = [_message_full(m) for m in thread.get("messages", [])]
        return {"thread_id": thread_id, "messages": messages}
    except GmailAuthError as e:
        return {"error": str(e)}
    except HttpError as e:
        return {"error": _handle_http_error(e, "get_thread")}
    except Exception as e:
        logger.error(f"get_thread failed: {e}", exc_info=True)
        return {"error": f"get_thread failed: {e}"}


@mcp.tool()
def get_latest_emails(count: int = 5) -> Dict[str, Any]:
    """Get the most recent emails in the inbox, newest first. Useful for
    requests like 'summarize my last two emails'."""
    try:
        service = _gmail_service()
        resp = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=count)
            .execute()
        )
        ids = [m["id"] for m in resp.get("messages", [])]
        messages = []
        for mid in ids:
            full = (
                service.users()
                .messages()
                .get(userId="me", id=mid, format="full")
                .execute()
            )
            messages.append(_message_full(full))
        return {"messages": messages}
    except GmailAuthError as e:
        return {"error": str(e)}
    except HttpError as e:
        return {"error": _handle_http_error(e, "get_latest_emails")}
    except Exception as e:
        logger.error(f"get_latest_emails failed: {e}", exc_info=True)
        return {"error": f"get_latest_emails failed: {e}"}


@mcp.tool()
def create_draft(to: str, subject: str, body: str) -> Dict[str, Any]:
    """Create a Gmail draft. Does NOT send it — the user (or agent, via
    send_email) must explicitly send it afterward."""
    try:
        service = _gmail_service()
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

        draft = (
            service.users()
            .drafts()
            .create(userId="me", body={"message": {"raw": raw}})
            .execute()
        )
        return {
            "draft_id": draft.get("id"),
            "message_id": draft.get("message", {}).get("id"),
            "status": "draft_created",
        }
    except GmailAuthError as e:
        return {"error": str(e)}
    except HttpError as e:
        return {"error": _handle_http_error(e, "create_draft")}
    except Exception as e:
        logger.error(f"create_draft failed: {e}", exc_info=True)
        return {"error": f"create_draft failed: {e}"}


@mcp.tool()
def send_email(to: str, subject: str, body: str) -> Dict[str, Any]:
    """Send an email immediately. Only call this when the user has
    explicitly asked for the email to be sent — never as a side effect of
    searching or reading mail."""
    try:
        service = _gmail_service()
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return {
            "message_id": sent.get("id"),
            "thread_id": sent.get("threadId"),
            "status": "sent",
        }
    except GmailAuthError as e:
        return {"error": str(e)}
    except HttpError as e:
        return {"error": _handle_http_error(e, "send_email")}
    except Exception as e:
        logger.error(f"send_email failed: {e}", exc_info=True)
        return {"error": f"send_email failed: {e}"}


@mcp.tool()
def list_labels() -> Dict[str, Any]:
    """List all Gmail labels (system + user-created) on the account."""
    try:
        service = _gmail_service()
        resp = service.users().labels().list(userId="me").execute()
        labels = [
            {"id": l["id"], "name": l["name"], "type": l.get("type", "user")}
            for l in resp.get("labels", [])
        ]
        return {"labels": labels}
    except GmailAuthError as e:
        return {"error": str(e)}
    except HttpError as e:
        return {"error": _handle_http_error(e, "list_labels")}
    except Exception as e:
        logger.error(f"list_labels failed: {e}", exc_info=True)
        return {"error": f"list_labels failed: {e}"}


if __name__ == "__main__":
    # Runs over stdio — this is what MCPClientManager.connect_stdio_server
    # launches as a subprocess.
    mcp.run(transport="stdio")
