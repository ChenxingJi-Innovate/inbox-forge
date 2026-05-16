"""
Inbox Forge - local-first AI email summarizer with rolling per-contact dossiers.

Architecture:
- All persistence is local SQLite at ~/.inbox-forge/data.db (override via DB_PATH).
- No user account / sign-in concept. The OAuth grant (Google or Microsoft) is
  itself the identity proof; the install is single-owner.
- Multiple inboxes per install (gmail + outlook) share the same contacts table,
  so the same person across both mailboxes shows up as one contact.
- Each contact has a rolling dossier (contact_dossier) that gets updated every
  time a new email from them is processed: the prior dossier and recent email
  summaries are fed back into Gemini as context.
"""
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import (
    FastAPI, File, Form, HTTPException, Query,
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

# OAuth CSRF: the state value is itself a signed, time-limited token. No
# server-side cache, so it survives Vercel's serverless multi-instance model.
# Falls back to a dev key locally if TOKEN_ENC_KEY is missing; production
# deploys MUST set TOKEN_ENC_KEY.
_OAUTH_STATE_TTL = 600
_state_signer = URLSafeTimedSerializer(
    os.getenv("TOKEN_ENC_KEY") or "dev-fallback-key-please-set-TOKEN_ENC_KEY",
    salt="oauth-state",
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


# ---------- Pages ----------

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
async def me():
    """No user account; just report which inboxes are connected and basic counts."""
    conns = db.list_oauth_connections()
    contacts = db.list_contacts()
    return {
        "connections": [
            {"id": c["id"], "provider": c["provider"], "provider_email": c["provider_email"]}
            for c in conns
        ],
        "stats": {
            "contacts": len(contacts),
            "connections": len(conns),
        },
    }


# ---------- OAuth: Google ----------

@app.get("/api/auth/google/start")
async def google_start(switch: int = 0):
    state = _make_state()
    return RedirectResponse(oauth.google_auth_url(state, force_account_picker=bool(switch)), status_code=302)


@app.get("/api/auth/google/callback")
async def google_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
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

    db.upsert_oauth_connection(
        provider="google",
        provider_email=email,
        access_token_enc=encrypt(tokens["access_token"]),
        refresh_token_enc=encrypt(tokens["refresh_token"]) if tokens.get("refresh_token") else None,
        expires_at=oauth.expires_at_from_seconds(tokens.get("expires_in")),
        scope=tokens.get("scope"),
    )
    return RedirectResponse("/?connected=google", status_code=302)


# ---------- OAuth: Microsoft ----------

@app.get("/api/auth/microsoft/start")
async def microsoft_start(switch: int = 0):
    state = _make_state()
    return RedirectResponse(oauth.microsoft_auth_url(state, force_account_picker=bool(switch)), status_code=302)


@app.get("/api/auth/microsoft/callback")
async def microsoft_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
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

    db.upsert_oauth_connection(
        provider="microsoft",
        provider_email=email,
        access_token_enc=encrypt(tokens["access_token"]),
        refresh_token_enc=encrypt(tokens["refresh_token"]) if tokens.get("refresh_token") else None,
        expires_at=oauth.expires_at_from_seconds(tokens.get("expires_in")),
        scope=tokens.get("scope"),
    )
    return RedirectResponse("/?connected=microsoft", status_code=302)


# ---------- Inbox: tokens + fetch ----------

def _live_access_token(conn_id: int, provider: str) -> str:
    """Return a fresh access token, refreshing via the stored refresh token if needed."""
    conn = db.get_oauth_connection(conn_id)
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

    needs_refresh = expires_at and expires_at <= datetime.now(timezone.utc)
    if not needs_refresh:
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
            access,
            read_state=read_state,
            since=since,
            until=until,
            keyword=keyword,
            limit=limit,
        )
    if conn["provider"] == "microsoft":
        return inbox_outlook.fetch_messages(
            access,
            read_state=read_state,
            since=since,
            until=until,
            keyword=keyword,
            limit=limit,
        )
    raise HTTPException(status_code=400, detail=f"Unsupported provider: {conn['provider']}")


@app.get("/api/inbox/{conn_id}/unread")
async def inbox_unread(conn_id: int, limit: int = 5):
    """Raw inspection endpoint: returns unread emails WITHOUT analyzing or storing them."""
    conn = db.get_oauth_connection(conn_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    access = _live_access_token(conn_id, conn["provider"])
    try:
        emails = _fetch_messages(
            conn, access,
            read_state="unread", since=None, until=None, keyword=None, limit=limit,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fetch failed: {e}")
    return {"success": True, "emails": emails}


@app.delete("/api/inbox/{conn_id}")
async def inbox_disconnect(conn_id: int):
    db.delete_oauth_connection(conn_id)
    return {"success": True}


# ---------- Inbox sweep: the auto-pipeline ----------

class SweepBody(BaseModel):
    limit: int = 50
    lang: str = "zh"
    read_state: str = "unread"  # "unread" | "read" | "all"
    since: Optional[str] = None  # "YYYY-MM-DD"
    until: Optional[str] = None  # "YYYY-MM-DD"
    keyword: Optional[str] = None


@app.post("/api/inbox/{conn_id}/sweep")
async def inbox_sweep(conn_id: int, body: SweepBody = SweepBody()):
    """Pull messages matching the filter → dedupe → analyze with contact context
    → store + update dossier.

    Idempotent: rerunning is safe because we skip any provider message_id we've
    already processed (UNIQUE(provider, message_id) in contact_emails).
    """
    conn = db.get_oauth_connection(conn_id)
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    lang = _norm_lang(body.lang)
    read_state = body.read_state if body.read_state in ("unread", "read", "all") else "unread"
    access = _live_access_token(conn_id, conn["provider"])
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

        if db.is_message_processed(provider, msg_id):
            skipped_duplicate += 1
            continue

        from_addr = (e.get("from_addr") or "").lower().strip()
        if not from_addr:
            continue

        contact = db.upsert_contact(email=from_addr, name=(e.get("from_name") or None))
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


# ---------- Stateless analyze (manual upload, no storage) ----------

MAX_TOTAL_BYTES = 40 * 1024 * 1024
MAX_FILES = 8


@app.post("/api/analyze")
async def analyze(
    files: List[UploadFile] = File(default=[]),
    text: str = Form(""),
    lang: str = Form("en"),
):
    """Kept for the manual screenshot / paste flow on the landing page.
    Does not touch the contacts database."""
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
        raise HTTPException(status_code=500, detail=f"Gemini error: {e}")

    return {
        "success": True,
        "summary": result.get("summary", ""),
        "action_items": result.get("action_items", ""),
        "sender": result.get("sender", ""),
        "company": result.get("company", ""),
        "subject": result.get("subject", ""),
    }


# ---------- Contacts ----------

@app.get("/api/contacts")
async def get_contacts():
    return {"contacts": db.list_contacts()}


@app.get("/api/contacts/{contact_id}")
async def get_contact_detail(contact_id: int, limit: int = Query(50, ge=1, le=200)):
    contact = db.get_contact(contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    emails = db.list_contact_emails(contact_id, limit=limit)
    return {"contact": contact, "emails": emails}


@app.delete("/api/contacts/{contact_id}")
async def remove_contact(contact_id: int):
    db.delete_contact(contact_id)
    return {"success": True}


@app.delete("/api/contact-emails/{email_id}")
async def remove_contact_email(email_id: int):
    db.delete_contact_email(email_id)
    return {"success": True}


# ---------- Errors ----------

@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# ---------- Local run ----------

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
