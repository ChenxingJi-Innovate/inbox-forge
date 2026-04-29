"""Postgres connection + schema.

DATABASE_URL must be a Postgres URL (Neon / Supabase / Vercel Postgres / Render
Postgres all work). The schema is created on first import.
"""
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg
from psycopg.rows import dict_row


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    name TEXT,
    avatar_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user_id_idx ON sessions(user_id);

CREATE TABLE IF NOT EXISTS oauth_connections (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_email TEXT NOT NULL,
    access_token_enc TEXT NOT NULL,
    refresh_token_enc TEXT,
    expires_at TIMESTAMPTZ,
    scope TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, provider, provider_email)
);
CREATE INDEX IF NOT EXISTS oauth_user_idx ON oauth_connections(user_id);

CREATE TABLE IF NOT EXISTS records (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    customer_name TEXT NOT NULL,
    company TEXT,
    date TEXT,
    summary TEXT,
    action_items TEXT,
    status TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS records_user_idx ON records(user_id, created_at DESC);
"""


def _conn_string() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    # Neon / some providers add ?sslmode=require, psycopg accepts it.
    return url


_initialized = False


def init_db():
    global _initialized
    if _initialized:
        return
    with psycopg.connect(_conn_string(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
    _initialized = True


@contextmanager
def cursor():
    """Yield a dict-row cursor in a fresh autocommit connection.

    Simple per-call connection: fine for serverless-style traffic and avoids
    pool lifecycle headaches. For higher load, swap in psycopg_pool later.
    """
    init_db()
    with psycopg.connect(_conn_string(), autocommit=True, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            yield cur


# ---------- users ----------

def upsert_user(email: str, name: Optional[str] = None, avatar_url: Optional[str] = None) -> dict:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO users (email, name, avatar_url) VALUES (%s, %s, %s)
               ON CONFLICT (email) DO UPDATE SET
                   name = COALESCE(EXCLUDED.name, users.name),
                   avatar_url = COALESCE(EXCLUDED.avatar_url, users.avatar_url)
               RETURNING id, email, name, avatar_url""",
            (email.lower().strip(), name, avatar_url),
        )
        return cur.fetchone()


def get_user(user_id: int) -> Optional[dict]:
    with cursor() as cur:
        cur.execute("SELECT id, email, name, avatar_url FROM users WHERE id = %s", (user_id,))
        return cur.fetchone()


# ---------- sessions ----------

SESSION_TTL_DAYS = 30


def create_session(user_id: int, token: str) -> None:
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)
    with cursor() as cur:
        cur.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (%s, %s, %s)",
            (token, user_id, expires),
        )


def get_user_by_session(token: str) -> Optional[dict]:
    with cursor() as cur:
        cur.execute(
            """SELECT u.id, u.email, u.name, u.avatar_url
               FROM sessions s JOIN users u ON s.user_id = u.id
               WHERE s.token = %s AND s.expires_at > NOW()""",
            (token,),
        )
        return cur.fetchone()


def delete_session(token: str) -> None:
    with cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE token = %s", (token,))


# ---------- oauth connections ----------

def upsert_oauth_connection(
    user_id: int,
    provider: str,
    provider_email: str,
    access_token_enc: str,
    refresh_token_enc: Optional[str],
    expires_at: Optional[datetime],
    scope: Optional[str],
) -> dict:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO oauth_connections
                  (user_id, provider, provider_email, access_token_enc,
                   refresh_token_enc, expires_at, scope)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (user_id, provider, provider_email) DO UPDATE SET
                   access_token_enc = EXCLUDED.access_token_enc,
                   refresh_token_enc = COALESCE(EXCLUDED.refresh_token_enc, oauth_connections.refresh_token_enc),
                   expires_at = EXCLUDED.expires_at,
                   scope = EXCLUDED.scope,
                   updated_at = NOW()
               RETURNING *""",
            (user_id, provider, provider_email.lower(), access_token_enc,
             refresh_token_enc, expires_at, scope),
        )
        return cur.fetchone()


def list_oauth_connections(user_id: int) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """SELECT id, provider, provider_email, expires_at, scope, created_at
               FROM oauth_connections WHERE user_id = %s ORDER BY created_at DESC""",
            (user_id,),
        )
        return cur.fetchall()


def get_oauth_connection(user_id: int, conn_id: int) -> Optional[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM oauth_connections WHERE id = %s AND user_id = %s",
            (conn_id, user_id),
        )
        return cur.fetchone()


def update_oauth_tokens(
    conn_id: int,
    access_token_enc: str,
    expires_at: Optional[datetime],
) -> None:
    with cursor() as cur:
        cur.execute(
            """UPDATE oauth_connections
               SET access_token_enc = %s, expires_at = %s, updated_at = NOW()
               WHERE id = %s""",
            (access_token_enc, expires_at, conn_id),
        )


def delete_oauth_connection(user_id: int, conn_id: int) -> None:
    with cursor() as cur:
        cur.execute(
            "DELETE FROM oauth_connections WHERE id = %s AND user_id = %s",
            (conn_id, user_id),
        )


# ---------- records ----------

def add_record(user_id: int, r: dict) -> dict:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO records (user_id, customer_name, company, date,
                                    summary, action_items, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               RETURNING id, customer_name, company, date, summary, action_items,
                         status, created_at""",
            (user_id, r.get("customer_name", ""), r.get("company", ""),
             r.get("date", ""), r.get("summary", ""), r.get("action_items", ""),
             r.get("status", "")),
        )
        return cur.fetchone()


def list_records(user_id: int) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """SELECT id, customer_name, company, date, summary, action_items,
                      status, created_at
               FROM records WHERE user_id = %s ORDER BY created_at DESC""",
            (user_id,),
        )
        return cur.fetchall()


def delete_record(user_id: int, record_id: int) -> None:
    with cursor() as cur:
        cur.execute(
            "DELETE FROM records WHERE id = %s AND user_id = %s",
            (record_id, user_id),
        )


def clear_records(user_id: int) -> None:
    with cursor() as cur:
        cur.execute("DELETE FROM records WHERE user_id = %s", (user_id,))
