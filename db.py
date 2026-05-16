"""Storage layer with per-user scoping.

Mode selection is automatic:
- TURSO_DATABASE_URL set → libsql HTTPS client (Vercel serverless)
- Otherwise               → local SQLite at ~/.inbox-forge/data.db
  (override with DB_PATH)

Schema v2 (2026-05-16) adds a `users` table and a `user_id` column on every
data-bearing row. Every API entry point must pass the current `user_id` so
queries are scoped to one identity. There is a one-shot migration that
detects the old schema (no user_id on oauth_connections) and drops + rebuilds.
That's safe because both the Vercel demo and the local installs had only
testing data at this point.
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    primary_email TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS oauth_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_email TEXT NOT NULL,
    access_token_enc TEXT NOT NULL,
    refresh_token_enc TEXT,
    expires_at TEXT,
    scope TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (provider, provider_email)
);

CREATE INDEX IF NOT EXISTS oauth_conn_user ON oauth_connections(user_id);
CREATE INDEX IF NOT EXISTS oauth_conn_provider_email ON oauth_connections(provider, provider_email);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    name TEXT,
    company TEXT,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    total_emails INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, email)
);

CREATE INDEX IF NOT EXISTS contacts_user ON contacts(user_id, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS contact_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    connection_id INTEGER REFERENCES oauth_connections(id) ON DELETE SET NULL,
    provider TEXT NOT NULL,
    message_id TEXT NOT NULL,
    received_at TEXT,
    subject TEXT,
    summary TEXT,
    action_items TEXT,
    raw_snippet TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (contact_id, provider, message_id)
);

CREATE INDEX IF NOT EXISTS contact_emails_contact ON contact_emails(contact_id, received_at DESC);

CREATE TABLE IF NOT EXISTS contact_dossier (
    contact_id INTEGER PRIMARY KEY REFERENCES contacts(id) ON DELETE CASCADE,
    rolling_summary TEXT,
    open_action_items TEXT,
    current_topic TEXT,
    relationship_stage TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


_USE_TURSO = bool(os.getenv("TURSO_DATABASE_URL"))


def _local_path() -> Path:
    custom = os.getenv("DB_PATH")
    if custom:
        p = Path(custom).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    base = Path.home() / ".inbox-forge"
    base.mkdir(parents=True, exist_ok=True)
    return base / "data.db"


_turso_client = None


def _turso():
    global _turso_client
    if _turso_client is None:
        import libsql_client  # type: ignore
        # Force HTTPS over Hrana; WebSocket fails on Vercel serverless.
        url = os.environ["TURSO_DATABASE_URL"]
        if url.startswith("libsql://"):
            url = "https://" + url[len("libsql://"):]
        _turso_client = libsql_client.create_client_sync(
            url=url,
            auth_token=os.environ.get("TURSO_AUTH_TOKEN"),
        )
    return _turso_client


class _TursoCursor:
    def __init__(self):
        self._rs = None
        self.lastrowid = None

    def execute(self, sql: str, params=()):
        rs = _turso().execute(sql, list(params) if params else [])
        self._rs = rs
        self.lastrowid = getattr(rs, "last_insert_rowid", None)
        return self

    @staticmethod
    def _row_to_dict(row, columns):
        return {col: row[i] for i, col in enumerate(columns)}

    def fetchone(self):
        rs = self._rs
        if rs is None or not getattr(rs, "rows", None):
            return None
        return self._row_to_dict(rs.rows[0], list(rs.columns))

    def fetchall(self):
        rs = self._rs
        if rs is None or not getattr(rs, "rows", None):
            return []
        cols = list(rs.columns)
        return [self._row_to_dict(r, cols) for r in rs.rows]


_initialized = False


def _split_schema(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


def _migrate_if_old_schema() -> None:
    """One-shot migration: drop legacy v1 tables so the new v2 schema can be
    created cleanly. v1 had no user_id column. We detect that and wipe.

    Safe because at this point both the local installs and the Vercel demo
    only had testing data. Future migrations should be additive, not
    destructive."""
    if _USE_TURSO:
        try:
            _turso().execute("SELECT user_id FROM oauth_connections LIMIT 1")
            return  # already v2
        except Exception:
            for tbl in ["contact_dossier", "contact_emails", "contacts", "oauth_connections", "users"]:
                try:
                    _turso().execute(f"DROP TABLE IF EXISTS {tbl}")
                except Exception:
                    pass
    else:
        with sqlite3.connect(_local_path()) as conn:
            try:
                conn.execute("SELECT user_id FROM oauth_connections LIMIT 1")
                return  # already v2
            except sqlite3.OperationalError:
                pass
            for tbl in ["contact_dossier", "contact_emails", "contacts", "oauth_connections", "users"]:
                try:
                    conn.execute(f"DROP TABLE IF EXISTS {tbl}")
                except sqlite3.OperationalError:
                    pass
            conn.commit()


def init_db() -> None:
    global _initialized
    if _initialized:
        return
    _migrate_if_old_schema()
    if _USE_TURSO:
        for stmt in _split_schema(SCHEMA_SQL):
            _turso().execute(stmt)
    else:
        with sqlite3.connect(_local_path()) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.commit()
    _initialized = True


@contextmanager
def cursor():
    init_db()
    if _USE_TURSO:
        yield _TursoCursor()
        return
    conn = sqlite3.connect(_local_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def _row(r) -> Optional[dict]:
    if r is None:
        return None
    if isinstance(r, dict):
        return r
    return dict(r)


def _iso(d: Optional[datetime]) -> Optional[str]:
    return d.isoformat() if d else None


# ─── users ───────────────────────────────────────────────────────────────

def create_user(primary_email: Optional[str] = None) -> dict:
    with cursor() as cur:
        cur.execute("INSERT INTO users (primary_email) VALUES (?)", (primary_email,))
        new_id = cur.lastrowid
        cur.execute("SELECT * FROM users WHERE id = ?", (new_id,))
        return _row(cur.fetchone())


def get_user(user_id: int) -> Optional[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        return _row(cur.fetchone())


def find_user_id_by_oauth_email(provider: str, provider_email: str) -> Optional[int]:
    """Look up which user owns this OAuth identity. Used so a returning visitor
    (no session cookie) lands back into their own account after re-OAuth."""
    with cursor() as cur:
        cur.execute(
            "SELECT user_id FROM oauth_connections WHERE provider = ? AND provider_email = ?",
            (provider, provider_email.lower().strip()),
        )
        row = _row(cur.fetchone())
        return row["user_id"] if row else None


# ─── oauth connections ───────────────────────────────────────────────────

def upsert_oauth_connection(
    user_id: int,
    provider: str,
    provider_email: str,
    access_token_enc: str,
    refresh_token_enc: Optional[str],
    expires_at: Optional[datetime],
    scope: Optional[str],
) -> dict:
    pe = provider_email.lower().strip()
    with cursor() as cur:
        cur.execute(
            """INSERT INTO oauth_connections
                  (user_id, provider, provider_email, access_token_enc, refresh_token_enc, expires_at, scope, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(provider, provider_email) DO UPDATE SET
                   user_id = excluded.user_id,
                   access_token_enc = excluded.access_token_enc,
                   refresh_token_enc = COALESCE(excluded.refresh_token_enc, oauth_connections.refresh_token_enc),
                   expires_at = excluded.expires_at,
                   scope = excluded.scope,
                   updated_at = CURRENT_TIMESTAMP""",
            (user_id, provider, pe, access_token_enc, refresh_token_enc, _iso(expires_at), scope),
        )
        cur.execute(
            "SELECT * FROM oauth_connections WHERE provider = ? AND provider_email = ?",
            (provider, pe),
        )
        return _row(cur.fetchone())


def list_oauth_connections(user_id: int) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """SELECT id, provider, provider_email, expires_at, scope, created_at
               FROM oauth_connections WHERE user_id = ? ORDER BY created_at DESC""",
            (user_id,),
        )
        return [_row(r) for r in cur.fetchall()]


def get_oauth_connection(user_id: int, conn_id: int) -> Optional[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM oauth_connections WHERE id = ? AND user_id = ?",
            (conn_id, user_id),
        )
        return _row(cur.fetchone())


def update_oauth_tokens(conn_id: int, access_token_enc: str, expires_at: Optional[datetime]) -> None:
    with cursor() as cur:
        cur.execute(
            """UPDATE oauth_connections
               SET access_token_enc = ?, expires_at = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (access_token_enc, _iso(expires_at), conn_id),
        )


def delete_oauth_connection(user_id: int, conn_id: int) -> None:
    """Sign out of a mailbox AND wipe everything that came through it,
    constrained to this user's scope."""
    with cursor() as cur:
        # Verify ownership before doing anything (cheap defense in depth).
        cur.execute(
            "SELECT id FROM oauth_connections WHERE id = ? AND user_id = ?",
            (conn_id, user_id),
        )
        if not _row(cur.fetchone()):
            return
        # Drop archived emails from this connection.
        cur.execute("DELETE FROM contact_emails WHERE connection_id = ?", (conn_id,))
        # Recompute counters on the user's contacts, drop orphans.
        cur.execute(
            """UPDATE contacts
               SET total_emails = (
                   SELECT COUNT(*) FROM contact_emails WHERE contact_id = contacts.id
               )
               WHERE user_id = ?""",
            (user_id,),
        )
        cur.execute("DELETE FROM contacts WHERE user_id = ? AND total_emails = 0", (user_id,))
        cur.execute("DELETE FROM oauth_connections WHERE id = ?", (conn_id,))


# ─── contacts ────────────────────────────────────────────────────────────

def upsert_contact(user_id: int, email: str, name: Optional[str] = None, company: Optional[str] = None) -> dict:
    e = email.lower().strip()
    with cursor() as cur:
        cur.execute(
            """INSERT INTO contacts (user_id, email, name, company)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, email) DO UPDATE SET
                   name = COALESCE(NULLIF(excluded.name, ''), contacts.name),
                   company = COALESCE(NULLIF(excluded.company, ''), contacts.company),
                   last_seen_at = CURRENT_TIMESTAMP""",
            (user_id, e, name or None, company or None),
        )
        cur.execute("SELECT * FROM contacts WHERE user_id = ? AND email = ?", (user_id, e))
        return _row(cur.fetchone())


def list_contacts(user_id: int) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """SELECT c.id, c.email, c.name, c.company, c.first_seen_at,
                      c.last_seen_at, c.total_emails,
                      d.rolling_summary, d.current_topic, d.relationship_stage,
                      d.updated_at AS dossier_updated_at
               FROM contacts c
               LEFT JOIN contact_dossier d ON d.contact_id = c.id
               WHERE c.user_id = ?
               ORDER BY c.last_seen_at DESC""",
            (user_id,),
        )
        return [_row(r) for r in cur.fetchall()]


def get_contact(user_id: int, contact_id: int) -> Optional[dict]:
    with cursor() as cur:
        cur.execute(
            """SELECT c.*, d.rolling_summary, d.open_action_items, d.current_topic,
                      d.relationship_stage, d.updated_at AS dossier_updated_at
               FROM contacts c
               LEFT JOIN contact_dossier d ON d.contact_id = c.id
               WHERE c.id = ? AND c.user_id = ?""",
            (contact_id, user_id),
        )
        return _row(cur.fetchone())


def delete_contact(user_id: int, contact_id: int) -> None:
    with cursor() as cur:
        cur.execute("DELETE FROM contacts WHERE id = ? AND user_id = ?", (contact_id, user_id))


# ─── contact_emails ──────────────────────────────────────────────────────

def is_message_processed(contact_id: int, provider: str, message_id: str) -> bool:
    with cursor() as cur:
        cur.execute(
            "SELECT 1 FROM contact_emails WHERE contact_id = ? AND provider = ? AND message_id = ? LIMIT 1",
            (contact_id, provider, message_id),
        )
        return cur.fetchone() is not None


def add_contact_email(
    contact_id: int,
    provider: str,
    message_id: str,
    connection_id: Optional[int],
    received_at: Optional[str],
    subject: str,
    summary: str,
    action_items: str,
    raw_snippet: str,
) -> Optional[dict]:
    """Insert one processed email. Returns None if it's a duplicate for this contact."""
    with cursor() as cur:
        try:
            cur.execute(
                """INSERT INTO contact_emails
                      (contact_id, connection_id, provider, message_id, received_at,
                       subject, summary, action_items, raw_snippet)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (contact_id, connection_id, provider, message_id, received_at,
                 subject, summary, action_items, raw_snippet),
            )
        except (sqlite3.IntegrityError, Exception) as e:
            msg = str(e).lower()
            if isinstance(e, sqlite3.IntegrityError) or "unique" in msg or "constraint" in msg:
                return None
            raise
        new_id = cur.lastrowid
        cur.execute(
            """UPDATE contacts
               SET total_emails = total_emails + 1, last_seen_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (contact_id,),
        )
        cur.execute("SELECT * FROM contact_emails WHERE id = ?", (new_id,))
        return _row(cur.fetchone())


def list_contact_emails(user_id: int, contact_id: int, limit: int = 50) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """SELECT ce.* FROM contact_emails ce
               JOIN contacts c ON c.id = ce.contact_id
               WHERE ce.contact_id = ? AND c.user_id = ?
               ORDER BY ce.received_at IS NULL, ce.received_at DESC, ce.created_at DESC
               LIMIT ?""",
            (contact_id, user_id, limit),
        )
        return [_row(r) for r in cur.fetchall()]


def get_recent_summaries(contact_id: int, n: int = 5) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            """SELECT subject, summary, action_items, received_at
               FROM contact_emails
               WHERE contact_id = ?
               ORDER BY received_at IS NULL, received_at DESC, created_at DESC
               LIMIT ?""",
            (contact_id, n),
        )
        return [_row(r) for r in cur.fetchall()]


def delete_contact_email(user_id: int, email_id: int) -> None:
    with cursor() as cur:
        cur.execute(
            """SELECT ce.contact_id FROM contact_emails ce
               JOIN contacts c ON c.id = ce.contact_id
               WHERE ce.id = ? AND c.user_id = ?""",
            (email_id, user_id),
        )
        row = _row(cur.fetchone())
        if not row:
            return
        cid = row["contact_id"]
        cur.execute("DELETE FROM contact_emails WHERE id = ?", (email_id,))
        cur.execute(
            "UPDATE contacts SET total_emails = MAX(0, total_emails - 1) WHERE id = ?",
            (cid,),
        )


# ─── dossier ─────────────────────────────────────────────────────────────

def upsert_dossier(
    contact_id: int,
    rolling_summary: str,
    open_action_items: str,
    current_topic: str,
    relationship_stage: str,
) -> dict:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO contact_dossier
                  (contact_id, rolling_summary, open_action_items, current_topic, relationship_stage, updated_at)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(contact_id) DO UPDATE SET
                   rolling_summary = excluded.rolling_summary,
                   open_action_items = excluded.open_action_items,
                   current_topic = excluded.current_topic,
                   relationship_stage = excluded.relationship_stage,
                   updated_at = CURRENT_TIMESTAMP""",
            (contact_id, rolling_summary, open_action_items, current_topic, relationship_stage),
        )
        cur.execute("SELECT * FROM contact_dossier WHERE contact_id = ?", (contact_id,))
        return _row(cur.fetchone())


def get_dossier(contact_id: int) -> Optional[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM contact_dossier WHERE contact_id = ?", (contact_id,))
        return _row(cur.fetchone())
