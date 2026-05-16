"""
Inbox Forge - per-user AI email summarizer with rolling per-contact dossiers.

Architecture:
- Persistence: Turso (libSQL over HTTPS) on Vercel, local SQLite otherwise.
- Identity: each visitor gets a signed-cookie session pointing at users.id.
  All API calls scope by user_id, so multiple visitors on the same Vercel
  instance see isolated data. The OAuth callback either creates a new user
  on first visit, or attaches the new connection to the existing user_id
  from the session.
- Sweep pipeline: pulls messages from Gmail / Outlook via stored OAuth tokens,
  dedupes by (contact_id, provider, message_id), feeds prior dossier + last
  five summaries into DeepSeek for context, writes back summary + refreshed
  dossier.
"""
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import (
    Cookie, Depends, FastAPI, File, Form, HTTPException, Query,
    Request, Response, UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel

import db
import inbox_gmail
import inbox_outlook
import oauth_providers as oauth
from crypto_util import decrypt, encrypt
from deepseek_service import analyze_with_context
from vision_service import analyze_email

load_dotenv()

app = FastAPI(title="Inbox Forge")

BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# OAuth state CSRF token: short-lived signed string (no server cache, so it
# survives Vercel multi-instance).
_OAUTH_STATE_TTL = 600
_state_signer = URLSafeTimedSerializer(
    os.getenv("TOKEN_ENC_KEY") or "dev-fallback-key-please-set-TOKEN_ENC_KEY",
    salt="oauth-state",
)

# Session: signed cookie carrying user_id. 30-day TTL.
SESSION_COOKIE = "ifs"
SESSION_TTL = 30 * 24 * 3600
_session_signer = URLSafeTimedSerializer(
    os.getenv("TOKEN_ENC_KEY") or "dev-fallback-key-please-set-TOKEN_ENC_KEY",
    salt="session",
)


def _norm_lang(lang: Optional[str]) -> str:
    return "zh" if str(lang or "").lower() == "zh" else "en"


def _make_state() -> str:
    return _state_signer.dumps(secrets.token_urlsafe(16))


def _consume_state(state: str) -> bool:
    try:
        _state_signer.loads(state, max_age=_OAUTH_STATE_TTL)
        return True
    except (BadSignature, SignatureExpired):
        return False


def _cookie_secure() -> bool:
    # APP_BASE_URL starting with https → secure cookie. Local http dev → no.
    return (os.getenv("APP_BASE_URL", "").lower().startswith("https://"))


def _issue_session(response: Response, user_id: int) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=_session_signer.dumps(user_id),
        max_age=SESSION_TTL,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
    )


def _clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def _user_id_from_cookie(token: Optional[str]) -> Optional[int]:
    if not token:
        return None
    try:
        return int(_session_signer.loads(token, max_age=SESSION_TTL))
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        return None


def current_user_id(request: Request) -> Optional[int]:
    """Optional: returns user_id if a valid session cookie is present, else None.
    Endpoints that return data scope by this; absence is treated as "anonymous"
    (returns empty list / hero state) rather than 401."""
    return _user_id_from_cookie(request.cookies.get(SESSION_COOKIE))


def require_user_id(request: Request) -> int:
    uid = current_user_id(request)
    if uid is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    return uid


# ─── Pages ────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
async def health():
    return {
        "deepseek_key_set": bool(os.getenv("DEEPSEEK_API_KEY")),
        "vision_key_set": bool(os.getenv("VISION_API_KEY")),
        "google_oauth_set": bool(os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET")),
        "microsoft_oauth_set": bool(os.getenv("MICROSOFT_CLIENT_ID") and os.getenv("MICROSOFT_CLIENT_SECRET")),
        "token_enc_key_set": bool(os.getenv("TOKEN_ENC_KEY")),
        "turso_set": bool(os.getenv("TURSO_DATABASE_URL")),
    }


@app.get("/api/me")
async def me(request: Request):
    """Anonymous-tolerant: if no session, return an empty shell so the
    frontend renders the hero/empty state rather than a 401."""
    uid = current_user_id(request)
    if uid is None:
        return {"connections": [], "stats": {"contacts": 0, "connections": 0}, "signed_in": False}
    conns = db.list_oauth_connections(uid)
    contacts = db.list_contacts(uid)
    return {
        "connections": [
            {"id": c["id"], "provider": c["provider"], "provider_email": c["provider_email"]}
            for c in conns
        ],
        "stats": {"contacts": len(contacts), "connections": len(conns)},
        "signed_in": True,
    }


@app.post("/api/logout")
async def logout(response: Response):
    _clear_session(response)
    return {"success": True}


# ─── OAuth: Google ────────────────────────────────────────────────────────

@app.get("/api/auth/google/start")
async def google_start(switch: int = 0):
    state = _make_state()
    return RedirectResponse(oauth.google_auth_url(state, force_account_picker=bool(switch)), status_code=302)


@app.get("/api/auth/google/callback")
async def google_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    if error:
        return RedirectResponse(f"/?oauth_error={error}", status_code=302)
    if not code or not state or not _consume_state(state):
        return RedirectResponse("/?oauth_error=bad_state", status_code=302)
    try:
        tokens = oauth.google_exchange_code(code)
        info = oauth.google_userinfo(tokens["access_token"])
    except Exception as e:
        return RedirectResponse(f"/?oauth_error={type(e).__name__}", status_code=302)

    email = (info.get("email") or "").lower()
    if not email:
        return RedirectResponse("/?oauth_error=no_email", status_code=302)

    uid = _resolve_user_for_oauth(request, "google", email)
    db.upsert_oauth_connection(
        user_id=uid,
        provider="google",
        provider_email=email,
        access_token_enc=encrypt(tokens["access_token"]),
        refresh_token_enc=encrypt(tokens["refresh_token"]) if tokens.get("refresh_token") else None,
        expires_at=oauth.expires_at_from_seconds(tokens.get("expires_in")),
        scope=tokens.get("scope"),
    )
    response = RedirectResponse("/?connected=google", status_code=302)
    _issue_session(response, uid)
    return response


# ─── OAuth: Microsoft ─────────────────────────────────────────────────────

@app.get("/api/auth/microsoft/start")
async def microsoft_start(switch: int = 0):
    state = _make_state()
    return RedirectResponse(oauth.microsoft_auth_url(state, force_account_picker=bool(switch)), status_code=302)


@app.get("/api/auth/microsoft/callback")
async def microsoft_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
    if error:
        return RedirectResponse(f"/?oauth_error={error}", status_code=302)
    if not code or not state or not _consume_state(state):
        return RedirectResponse("/?oauth_error=bad_state", status_code=302)
    try:
        tokens = oauth.microsoft_exchange_code(code)
        info = oauth.microsoft_userinfo(tokens["access_token"])
    except Exception as e:
        return RedirectResponse(f"/?oauth_error={type(e).__name__}", status_code=302)

    email = (info.get("mail") or info.get("userPrincipalName") or "").lower()
    if not email:
        return RedirectResponse("/?oauth_error=no_email", status_code=302)

    uid = _resolve_user_for_oauth(request, "microsoft", email)
    db.upsert_oauth_connection(
        user_id=uid,
        provider="microsoft",
        provider_email=email,
        access_token_enc=encrypt(tokens["access_token"]),
        refresh_token_enc=encrypt(tokens["refresh_token"]) if tokens.get("refresh_token") else None,
        expires_at=oauth.expires_at_from_seconds(tokens.get("expires_in")),
        scope=tokens.get("scope"),
    )
    response = RedirectResponse("/?connected=microsoft", status_code=302)
    _issue_session(response, uid)
    return response


def _resolve_user_for_oauth(request: Request, provider: str, provider_email: str) -> int:
    """Decide which user_id this OAuth callback belongs to.

    Priority:
    1. If this OAuth identity (provider+email) already maps to a user in DB,
       always honor that — OAuth proved ownership, so this is the rightful
       owner returning. (This handles the "returning visitor, no cookie"
       case: they re-OAuth and land back in their account.)
    2. Otherwise, if the browser has a valid session, link the new connection
       to that user (e.g. adding a second mailbox to an existing account).
    3. Otherwise, create a fresh user.
    """
    existing = db.find_user_id_by_oauth_email(provider, provider_email)
    if existing:
        return existing
    cookie_uid = current_user_id(request)
    if cookie_uid:
        return cookie_uid
    return db.create_user(primary_email=provider_email)["id"]


# ─── Inbox: tokens + fetch ────────────────────────────────────────────────

def _live_access_token(user_id: int, conn_id: int, provider: str) -> str:
    conn = db.get_oauth_connection(user_id, conn_id)
    if not conn or conn["provider"] != provider:
        raise HTTPException(status_code=404, detail="Connection not found")

    access = decrypt(conn["access_token_enc"])
    expires_at_iso = conn.get("expires_at")
    expires_at = None
    if expires_at_iso:
        try:
            expires_at = datetime.fromisoformat(expires_at_iso)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        except ValueError:
            expires_at = None

    if not (expires_at and expires_at <= datetime.now(timezone.utc)):
        return access

    refresh = decrypt(conn["refresh_token_enc"]) if conn.get("refresh_token_enc") else None
    if not refresh:
        raise HTTPException(status_code=400, detail="Token expired and no refresh token; please re-connect")
    try:
        new_tokens = (oauth.google_refresh if provider == "google" else oauth.microsoft_refresh)(refresh)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Refresh failed: {e}; please re-connect")
    new_access = new_tokens["access_token"]
    db.update_oauth_tokens(
        conn_id=conn_id,
        access_token_enc=encrypt(new_access),
        expires_at=oauth.expires_at_from_seconds(new_tokens.get("expires_in")),
    )
    return new_access


def _fetch_messages(
    conn: dict,
    access: str,
    *,
    read_state: str,
    since: Optional[str],
    until: Optional[str],
    keyword: Optional[str],
    limit: int,
) -> list[dict]:
    if conn["provider"] == "google":
        return inbox_gmail.fetch_messages(
            access, read_state=read_state, since=since, until=until, keyword=keyword, limit=limit,
        )
    if conn["provider"] == "microsoft":
        return inbox_outlook.fetch_messages(
            access, read_state=read_state, since=since, until=until, keyword=keyword, limit=limit,
        )
    raise HTTPException(status_code=400, detail=f"Unsupported provider: {conn['provider']}")


@app.get("/api/inbox/{conn_id}/unread")
async def inbox_unread(conn_id: int, limit: int = 5, request: Request = None, user_id: int = Depends(require_user_id)):
    conn = db.get_oauth_connection(user_id, conn_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    access = _live_access_token(user_id, conn_id, conn["provider"])
    try:
        emails = _fetch_messages(conn, access, read_state="unread", since=None, until=None, keyword=None, limit=limit)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fetch failed: {e}")
    return {"success": True, "emails": emails}


@app.delete("/api/inbox/{conn_id}")
async def inbox_disconnect(conn_id: int, user_id: int = Depends(require_user_id)):
    db.delete_oauth_connection(user_id, conn_id)
    return {"success": True}


# ─── Inbox sweep: the auto-pipeline ───────────────────────────────────────

class SweepBody(BaseModel):
    limit: int = 50
    lang: str = "zh"
    read_state: str = "unread"
    since: Optional[str] = None
    until: Optional[str] = None
    keyword: Optional[str] = None


@app.post("/api/inbox/{conn_id}/sweep")
async def inbox_sweep(conn_id: int, body: SweepBody = SweepBody(), user_id: int = Depends(require_user_id)):
    conn = db.get_oauth_connection(user_id, conn_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    lang = _norm_lang(body.lang)
    read_state = body.read_state if body.read_state in ("unread", "read", "all") else "unread"
    access = _live_access_token(user_id, conn_id, conn["provider"])
    try:
        raw_emails = _fetch_messages(
            conn, access,
            read_state=read_state,
            since=body.since or None,
            until=body.until or None,
            keyword=(body.keyword or "").strip() or None,
            limit=body.limit,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fetch failed: {e}")

    processed: list[dict] = []
    skipped_duplicate = 0
    failed: list[dict] = []
    provider = conn["provider"]

    for e in raw_emails:
        msg_id = e.get("id")
        if not msg_id:
            continue

        from_addr = (e.get("from_addr") or "").lower().strip()
        if not from_addr:
            continue

        contact = db.upsert_contact(user_id=user_id, email=from_addr, name=(e.get("from_name") or None))

        if db.is_message_processed(contact["id"], provider, msg_id):
            skipped_duplicate += 1
            continue

        prior_dossier = db.get_dossier(contact["id"])
        history = db.get_recent_summaries(contact["id"], n=5)

        try:
            result = analyze_with_context(
                body=e.get("body", "") or "",
                subject=e.get("subject", "") or "",
                dossier=prior_dossier,
                history=history,
                lang=lang,
            )
        except Exception as exc:
            failed.append({"id": msg_id, "from": from_addr, "error": str(exc)})
            continue

        stored = db.add_contact_email(
            contact_id=contact["id"],
            provider=provider,
            message_id=msg_id,
            connection_id=conn_id,
            received_at=e.get("date") or None,
            subject=e.get("subject", "") or "",
            summary=result.get("summary", "") or "",
            action_items=result.get("action_items", "") or "",
            raw_snippet=(e.get("body", "") or "")[:2000],
        )
        if not stored:
            skipped_duplicate += 1
            continue

        ud = result.get("updated_dossier") or {}
        db.upsert_dossier(
            contact_id=contact["id"],
            rolling_summary=ud.get("rolling_summary", "") or (prior_dossier or {}).get("rolling_summary", "") or "",
            open_action_items=ud.get("open_action_items", "") or "",
            current_topic=ud.get("current_topic", "") or "",
            relationship_stage=ud.get("relationship_stage", "") or "",
        )

        processed.append({
            "contact_id": contact["id"],
            "contact_email": from_addr,
            "subject": e.get("subject", "") or "",
            "summary": result.get("summary", "") or "",
        })

    return {
        "success": True,
        "fetched": len(raw_emails),
        "processed": len(processed),
        "skipped_duplicate": skipped_duplicate,
        "failed": failed,
        "items": processed,
    }


# ─── Stateless analyze ────────────────────────────────────────────────────

MAX_TOTAL_BYTES = 40 * 1024 * 1024
MAX_FILES = 8


@app.post("/api/analyze")
async def analyze(
    files: List[UploadFile] = File(default=[]),
    text: str = Form(""),
    lang: str = Form("en"),
):
    """Public, anonymous-friendly screenshot/text analysis. Not user-scoped
    because it doesn't read or write the contact archive."""
    lang = _norm_lang(lang)
    text = (text or "").strip()
    images_bytes: list[bytes] = []
    total = 0

    if files and len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"Too many screenshots (max {MAX_FILES})")
    for f in files or []:
        if not f or not f.filename:
            continue
        if not f.content_type or not f.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="Only image files are supported")
        content = await f.read()
        if not content:
            continue
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise HTTPException(status_code=400, detail="Total image size too large (max 40MB)")
        images_bytes.append(content)

    if not images_bytes and not text:
        raise HTTPException(status_code=400, detail="Provide at least one screenshot or paste email text")

    try:
        result = analyze_email(images=images_bytes, text=text, lang=lang)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Vision LLM error: {e}")

    return {
        "success": True,
        "summary": result.get("summary", ""),
        "action_items": result.get("action_items", ""),
        "sender": result.get("sender", ""),
        "company": result.get("company", ""),
        "subject": result.get("subject", ""),
    }


# ─── Contacts ─────────────────────────────────────────────────────────────

@app.get("/api/contacts")
async def get_contacts(request: Request):
    """Anonymous-tolerant: empty list for visitors without a session."""
    uid = current_user_id(request)
    if uid is None:
        return {"contacts": []}
    return {"contacts": db.list_contacts(uid)}


@app.get("/api/contacts/{contact_id}")
async def get_contact_detail(contact_id: int, limit: int = Query(50, ge=1, le=200), user_id: int = Depends(require_user_id)):
    contact = db.get_contact(user_id, contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    emails = db.list_contact_emails(user_id, contact_id, limit=limit)
    return {"contact": contact, "emails": emails}


@app.delete("/api/contacts/{contact_id}")
async def remove_contact(contact_id: int, user_id: int = Depends(require_user_id)):
    db.delete_contact(user_id, contact_id)
    return {"success": True}


@app.delete("/api/contact-emails/{email_id}")
async def remove_contact_email(email_id: int, user_id: int = Depends(require_user_id)):
    db.delete_contact_email(user_id, email_id)
    return {"success": True}


# ─── Errors ───────────────────────────────────────────────────────────────

@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# ─── Local run ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import socket
    import uvicorn

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"

    print()
    print("=" * 50)
    print(" Inbox Forge running (local mode)")
    print(f"   Local:    http://127.0.0.1:8000")
    print(f"   Network:  http://{local_ip}:8000")
    print(f"   Data:     {Path(os.getenv('DB_PATH') or (Path.home() / '.inbox-forge' / 'data.db'))}")
    print("=" * 50)
    print()

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
