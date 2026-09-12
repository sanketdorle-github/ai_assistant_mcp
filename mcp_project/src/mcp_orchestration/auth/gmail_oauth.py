"""Gmail OAuth helper for the MCP server.

Storage model: credentials are scoped per logged-in user and persisted
(encrypted) in the `gmail_credentials` MongoDB collection via
`GmailCredentialsRepository` - not a shared local JSON file. `/login`,
`/authorize`, and `/status` require a Bearer JWT (same `get_current_user`
dependency every other protected route uses); both `/login` and
`/authorize` encode the current user's id into the OAuth `state` param
(short-lived, signed with the app's existing JWT helpers) so `/callback` -
which Google redirects to directly, with no Authorization header - knows
which user the tokens belong to, and so a forged/replayed `state` is
rejected rather than silently accepted.

Two ways to start the flow, same underlying consent URL:
  - `/login` (HTML): for manual/curl/Swagger testing - returns a page with
    a clickable "Authenticate with Gmail" link.
  - `/authorize` (JSON): for the React frontend - a `fetch()` can attach
    the Authorization header a plain browser navigation can't, so it calls
    this to get `{"auth_url": ...}` and then does the actual navigation
    itself (`window.location.href = auth_url`).
`/callback` always ends in a redirect back to `settings.frontend_url`
(`?gmail=connected` or `?gmail=error&reason=...`) rather than rendering
HTML itself, so the SPA - not this backend - owns what the user sees.

This file already used the real Google OAuth endpoints
(accounts.google.com / oauth2.googleapis.com) - that part is unchanged and
reused as-is by the custom Gmail MCP server (`mcp/gmail_mcp_server.py`).
"""

import logging
import os
from datetime import timedelta
from typing import Optional
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from mcp_orchestration.api.routes.auth import get_current_user
from mcp_orchestration.core.config import settings
from mcp_orchestration.core.security import create_access_token, decode_access_token
from mcp_orchestration.core.token_encryption import encrypt
from mcp_orchestration.repositories.gmail_credentials import GmailCredentialsRepository

logger = logging.getLogger("mcp_orchestration.auth.gmail")

router = APIRouter(prefix="/auth/gmail", tags=["Gmail OAuth"])

_STATE_PURPOSE = "gmail_oauth"
_STATE_TTL_MINUTES = 10


def _client_id() -> Optional[str]:
    return os.getenv("GOOGLE_CLIENT_ID") or os.getenv("GMAIL_CLIENT_ID")


def _client_secret() -> Optional[str]:
    return os.getenv("GOOGLE_CLIENT_SECRET") or os.getenv("GMAIL_CLIENT_SECRET")


def _redirect_uri() -> str:
    return os.getenv("GOOGLE_REDIRECT_URI") or os.getenv(
        "GMAIL_REDIRECT_URI", "http://localhost:8000/auth/gmail/callback"
    )


def _make_state(user_id: str) -> str:
    return create_access_token(
        {"sub": user_id, "purpose": _STATE_PURPOSE},
        expires_delta=timedelta(minutes=_STATE_TTL_MINUTES),
    )


def _user_id_from_state(state: Optional[str]) -> str:
    if not state:
        raise HTTPException(status_code=400, detail="Missing OAuth state parameter.")
    payload = decode_access_token(state)
    if not payload or payload.get("purpose") != _STATE_PURPOSE or not payload.get("sub"):
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired OAuth state. Please restart the Gmail login flow.",
        )
    return payload["sub"]


def _scope() -> str:
    # Minimum scopes for the custom Gmail MCP server's tool set:
    # gmail.readonly -> search_emails/get_email/get_thread/get_latest_emails/list_labels
    # gmail.compose  -> create_draft/send_email
    return os.getenv(
        "GMAIL_SCOPES",
        "https://www.googleapis.com/auth/gmail.readonly "
        "https://www.googleapis.com/auth/gmail.compose",
    )


def _build_consent_url(state: str) -> str:
    """Build the Google consent-screen URL for a given (already-signed)
    `state`. Shared by `/login` (HTML, manual testing) and `/authorize`
    (JSON, the React frontend).

    Uses `urlencode` rather than raw f-string concatenation - `scope`
    contains a literal space between the two scope URLs, and `state` is a
    JWT. An unencoded space in a URL is exactly the kind of thing that gets
    silently truncated by copy-paste (e.g. a double-click text selection
    stops at whitespace) - which drops everything after it, including
    `state`, and produces "Missing OAuth state parameter" at /callback even
    though the flow up to that point looked fine.
    """
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
        {
            "client_id": _client_id(),
            "redirect_uri": _redirect_uri(),
            "response_type": "code",
            "scope": _scope(),
            "access_type": os.getenv("GMAIL_ACCESS_TYPE", "offline"),
            "prompt": "consent",
            "state": state,
        }
    )


@router.get("/authorize")
async def gmail_authorize(current_user: dict = Depends(get_current_user)):
    """JSON equivalent of /login for the React frontend: returns the
    consent URL to navigate the browser to, instead of an HTML page to
    click through. Requires the Bearer header a real browser navigation
    can't attach, which is exactly why this is a separate call the
    frontend makes with `fetch()` before navigating."""
    if not _client_id() or not _client_secret():
        raise HTTPException(
            status_code=400,
            detail="Gmail OAuth is not configured on the server "
            "(GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET missing).",
        )

    state = _make_state(current_user["id"])
    return {"auth_url": _build_consent_url(state)}


@router.get("/login", response_class=HTMLResponse)
async def gmail_login_page(current_user: dict = Depends(get_current_user)):
    """Display Gmail OAuth login page with authentication button, scoped to
    the currently authenticated app user. Manual/curl/Swagger testing path
    - the React frontend uses /authorize instead."""
    redirect_uri = _redirect_uri()
    scope = _scope()

    if not _client_id():
        return HTMLResponse(
            """
            <html>
                <body>
                    <h1>Gmail OAuth Not Configured</h1>
                    <p>Please set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env</p>
                </body>
            </html>
            """,
            status_code=400,
        )

    state = _make_state(current_user["id"])
    auth_url = _build_consent_url(state)

    return HTMLResponse(f"""
    <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; max-width: 600px; margin: 50px auto; padding: 20px; }}
                .button {{
                    background: #4285f4;
                    color: white;
                    padding: 12px 24px;
                    text-decoration: none;
                    border-radius: 4px;
                    display: inline-block;
                    font-weight: bold;
                }}
                .button:hover {{ background: #357abd; }}
                .info {{ background: #f0f0f0; padding: 15px; border-radius: 4px; margin: 20px 0; }}
            </style>
        </head>
        <body>
            <h1>🔐 Gmail Authentication</h1>
            <div class="info">
                <p><strong>Redirect URI:</strong> {redirect_uri}</p>
                <p><strong>Scopes:</strong> {scope}</p>
            </div>
            <p>Click the button below to authenticate with Google and grant access to your Gmail.</p>
            <a href="{auth_url}" class="button">Authenticate with Gmail</a>
            <p style="margin-top: 20px; color: #666; font-size: 0.9em;">
                After authentication, you'll be redirected back and tokens will be stored
                for your account.
            </p>
        </body>
    </html>
    """)


def _error_redirect(reason: str) -> RedirectResponse:
    return RedirectResponse(
        f"{settings.frontend_url}/?gmail=error&reason={quote(reason)}"
    )


@router.get("/callback")
async def gmail_callback(code: str, state: Optional[str] = None):
    """Handle Gmail OAuth callback. `state` ties this callback back to the
    app user who started the flow at /login or /authorize - see module
    docstring. Every outcome ends in a redirect back to the frontend
    (`settings.frontend_url`) rather than rendering HTML here - this
    backend doesn't own what the user sees, the SPA does."""
    try:
        try:
            user_id = _user_id_from_state(state)
        except HTTPException as e:
            return _error_redirect(str(e.detail))

        client_id = _client_id()
        client_secret = _client_secret()
        redirect_uri = _redirect_uri()

        if not client_id or not client_secret:
            return _error_redirect(
                "Gmail OAuth is not configured on the server "
                "(GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET missing)."
            )

        # Exchange code for tokens
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                timeout=30.0,
            )
            response.raise_for_status()
            tokens = response.json()

        repo = GmailCredentialsRepository()
        existing = repo.find_by_user_id(user_id) or {}

        # A refresh token is only issued on the first consent (or when
        # `prompt=consent` forces it, which /login always sets) - fall back
        # to whatever we already had stored so a re-auth doesn't wipe it.
        new_refresh_token = tokens.get("refresh_token")
        refresh_token_to_store = (
            encrypt(new_refresh_token)
            if new_refresh_token
            else existing.get("refresh_token")
        )

        repo.upsert(
            user_id,
            access_token=encrypt(tokens["access_token"]),
            refresh_token=refresh_token_to_store,
            expires_in=tokens.get("expires_in", 3600),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
        )

        logger.info(f"✅ Gmail OAuth tokens stored for user '{user_id}'")

        return RedirectResponse(f"{settings.frontend_url}/?gmail=connected")

    except Exception as e:
        logger.error(f"Gmail OAuth callback error: {e}", exc_info=True)
        return _error_redirect(str(e))


@router.get("/status")
async def gmail_status(current_user: dict = Depends(get_current_user)):
    """Check Gmail authentication status for the current app user."""
    creds = GmailCredentialsRepository().find_by_user_id(current_user["id"])

    if creds:
        return {
            "authenticated": True,
            "has_refresh_token": bool(creds.get("refresh_token")),
            "expires_in": creds.get("expires_in", 0),
            "client_id": creds.get("client_id", _client_id() or "unknown"),
        }

    return {
        "authenticated": False,
        "message": "No valid tokens found. Visit /auth/gmail/login to authenticate.",
        "login_url": "/auth/gmail/login",
    }


@router.delete("/disconnect")
async def gmail_disconnect(current_user: dict = Depends(get_current_user)):
    """Revoke this user's Gmail token at Google and delete the stored
    credential. A file delete (the old behavior) never invalidated the
    token at Google's end - this does."""
    repo = GmailCredentialsRepository()
    creds = repo.find_by_user_id(current_user["id"])
    if creds:
        from mcp_orchestration.core.token_encryption import decrypt

        token_to_revoke = None
        try:
            token_to_revoke = decrypt(
                creds.get("refresh_token") or creds.get("access_token") or ""
            )
        except Exception as e:
            logger.warning(f"Could not decrypt token for revocation: {e}")

        if token_to_revoke:
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        "https://oauth2.googleapis.com/revoke",
                        data={"token": token_to_revoke},
                        timeout=10.0,
                    )
            except Exception as e:
                logger.warning(f"Failed to revoke Gmail token at Google: {e}")

        repo.delete(current_user["id"])

    return {"disconnected": True}
