# Inbox Forge

**A per-contact AI email assistant.** Inbox Forge connects to Gmail or Outlook and keeps a living dossier for every
person you correspond with: who they are, what you are discussing, what is still open, and whose turn it is to reply.
Each new email updates the dossier instead of starting from scratch, so context builds up over months without the
prompt growing.

Bilingual (English and Chinese), self-hosted, and deployable as a personal instance in one click.

## What it does

- **Rolling contact dossiers.** Every incoming email produces a short summary and an update to that contact's
  dossier: current topic, open action items, and a relationship stage (Needs reply, In progress, Awaiting them,
  Resolved). Past summaries are kept as written; only the dossier moves forward.
- **Contacts grouped by company.** Senders are matched by address and grouped under their normalized company, ordered
  by recency, so a week of mail reads as a short list of relationships.
- **Selective sync.** Pull mail by date range and read state (unread, read or all), with duplicates removed per
  provider and message.
- **Quick Analyze.** Paste an email body or drop a screenshot for a one-off structured summary without connecting an
  inbox, and optionally pin it to a manual archive.

## Architecture

```
Gmail / Outlook (OAuth 2.0)            Screenshot or pasted text
        │                                         │
        ▼                                         ▼
deduplicate by (provider, message id)     vision model, one-shot summary
        │                                         │
        ▼                                         ▼
upsert contact, group by company          manual archive (optional)
        │
        ▼
load current dossier + last 5 summaries
        │
        ▼
text model: summarize the new email, rewrite the dossier
        │
        ▼
store summary, update dossier and relationship stage
```

- **Backend:** Python, FastAPI. Both model calls go through OpenAI-compatible APIs, so the provider is configuration,
  not code.
- **Storage:** SQLite locally; Turso (hosted libSQL) in the cloud. Data is scoped per signed-in user.
- **Security:** OAuth tokens are encrypted at rest; OAuth state is signed; sign-in by magic link is optional.
- **Frontend:** a single static page, served by the same app.
- **Hosting:** Vercel, one deployment per user.

## Deploy your own instance

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https://github.com/ChenxingJi-Innovate/inbox-forge&env=DEEPSEEK_API_KEY,VISION_API_KEY,VISION_BASE_URL,VISION_MODEL,GOOGLE_CLIENT_ID,GOOGLE_CLIENT_SECRET,APP_BASE_URL,TOKEN_ENC_KEY,TURSO_DATABASE_URL,TURSO_AUTH_TOKEN)

Fork the repository first and point the button at your fork; each fork is one personal instance. Setup takes about
ten minutes:

| Key | Where to get it |
|---|---|
| `DEEPSEEK_API_KEY` | [DeepSeek platform](https://platform.deepseek.com), API keys. Powers the dossier pipeline. |
| `VISION_API_KEY`, `VISION_BASE_URL`, `VISION_MODEL` | Any OpenAI-compatible vision endpoint; defaults are in `.env.example`. Powers Quick Analyze. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | [Google Cloud console](https://console.cloud.google.com): create an OAuth client (web application), enable the Gmail API, and add `<APP_BASE_URL>/api/auth/google/callback` as a redirect URI. |
| `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` | [Turso](https://turso.tech): `turso db create inbox-forge`, then `turso db show inbox-forge --url` and `turso db tokens create inbox-forge`. |
| `TOKEN_ENC_KEY` | `openssl rand -base64 32` |
| `APP_BASE_URL` | Your deployment's URL, no trailing slash. |

Outlook is optional: register an app in [Microsoft Entra](https://entra.microsoft.com) with the delegated `Mail.Read`
permission and redirect URI `<APP_BASE_URL>/api/auth/microsoft/callback`, then set `MICROSOFT_CLIENT_ID` and
`MICROSOFT_CLIENT_SECRET`.

## Run locally

No hosted database needed; data is stored in `~/.inbox-forge/data.db`.

```bash
pip install -r requirements.txt
cp .env.example .env     # fill in the keys above; leave TURSO_* empty to use local SQLite
                         # set APP_BASE_URL=http://127.0.0.1:8000
python3 main.py
open http://127.0.0.1:8000
```

To remove all local data: `rm -rf ~/.inbox-forge`.

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/auth/google/start`, `/api/auth/microsoft/start` | Connect an inbox |
| `POST` | `/api/inbox/{id}/sweep` | Sync a connected inbox and update dossiers |
| `POST` | `/api/analyze` | One-shot summary of pasted text or a screenshot |
| `GET` | `/api/contacts`, `/api/contacts/{id}` | Contacts with their dossiers and summaries |
| `GET`, `POST` | `/api/manual-entries` | The manual archive |
| `GET` | `/api/health` | Health check |
