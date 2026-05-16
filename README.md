# Inbox Forge

Local-first AI email assistant. Connect Gmail or Outlook, and Gemini builds a rolling per-contact dossier from every email. Each new message updates the dossier in place, so context never gets lost.

- One Vercel deploy = your personal cloud instance
- Or `python3 main.py` locally and everything lives in `~/.inbox-forge/data.db`

## One-click deploy

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https://github.com/YOUR_USERNAME/email-summarizer&env=DEEPSEEK_API_KEY,VISION_API_KEY,VISION_BASE_URL,VISION_MODEL,GOOGLE_CLIENT_ID,GOOGLE_CLIENT_SECRET,APP_BASE_URL,TOKEN_ENC_KEY,TURSO_DATABASE_URL,TURSO_AUTH_TOKEN)

> Replace `YOUR_USERNAME` with your GitHub username after forking. Each fork = one personal instance.

### 4 keys to grab (about 10 minutes total)

| # | What | Where | How long |
|---|---|---|---|
| 1 | `DEEPSEEK_API_KEY` | https://platform.deepseek.com → API keys. Drives the main dossier pipeline (default model `deepseek-v4-flash`). Top up ~$5, lasts months. | 2 min |
| 2 | `VISION_API_KEY` | https://bailian.console.aliyun.com → API-Key 管理. Drives the Quick Analyze screenshot path via Qwen3-VL-Plus (default). New users get 1M free tokens. | 2 min |
| 3 | `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` | https://console.cloud.google.com → APIs & Services → Credentials → Create OAuth client ID (Web app). Add `https://YOUR-PROJECT.vercel.app/api/auth/google/callback` as Authorized redirect URI. Also enable the Gmail API. | 5 min |
| 4 | `TURSO_DATABASE_URL` + `TURSO_AUTH_TOKEN` | https://turso.tech → sign up with GitHub → `turso db create inbox-forge` → `turso db show inbox-forge --url` and `turso db tokens create inbox-forge` | 2 min |
| 5 | `TOKEN_ENC_KEY` | Local shell: `openssl rand -base64 32` (signs OAuth state + encrypts stored tokens) | 10 sec |

Optional, only if you want Outlook too:

| # | What | Where |
|---|---|---|
| 5 | `MICROSOFT_CLIENT_ID` + `MICROSOFT_CLIENT_SECRET` | https://entra.microsoft.com → App registrations → New. Redirect URI: `https://YOUR-PROJECT.vercel.app/api/auth/microsoft/callback`. Permission: `Mail.Read` (delegated). |

Paste them into the Vercel deploy form, hit Deploy, done.

## Run locally instead

No external services needed. SQLite file lives in `~/.inbox-forge/data.db`.

```bash
pip install -r requirements.txt
cp .env.example .env   # or write a .env manually with the 4 keys above
                       # leave TURSO_* unset; local SQLite kicks in automatically
                       # set GOOGLE_REDIRECT_URI to http://127.0.0.1:8000/api/auth/google/callback
python3 main.py
open http://127.0.0.1:8000
```

To wipe everything: `rm -rf ~/.inbox-forge`.

## How it works

```
Gmail/Outlook unread  →  dedupe by message_id
                      →  upsert contact (by from address)
                      →  fetch contact's existing dossier + last 5 summaries
                      →  Gemini: summarize this email + rewrite the dossier
                      →  store summary + overwrite dossier
```

The dossier rolls forward every email, so context accumulates without unbounded growth.

## Env vars summary

| Var | Required | Notes |
|---|---|---|
| `DEEPSEEK_API_KEY` | yes | DeepSeek-V4-Flash drives the dossier pipeline (`/api/inbox/{id}/sweep`) |
| `VISION_API_KEY` | yes | Qwen3-VL-Plus on DashScope drives the Quick Analyze screenshot path (`/api/analyze`). OpenAI-compatible; swap providers by changing `VISION_BASE_URL` + `VISION_MODEL`. |
| `VISION_BASE_URL` | yes | Default `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `VISION_MODEL` | yes | Default `qwen3-vl-plus` |
| `APP_BASE_URL` | yes | `https://YOUR.vercel.app` (no trailing slash) or `http://127.0.0.1:8000` locally |
| `GOOGLE_CLIENT_ID` | yes for Gmail | Authorized redirect URI in the OAuth console: `<APP_BASE_URL>/api/auth/google/callback` |
| `GOOGLE_CLIENT_SECRET` | yes for Gmail | |
| `MICROSOFT_CLIENT_ID` | optional | for Outlook; redirect URI `<APP_BASE_URL>/api/auth/microsoft/callback` |
| `MICROSOFT_CLIENT_SECRET` | optional | |
| `TOKEN_ENC_KEY` | yes | Fernet/HMAC key. Encrypts stored OAuth tokens, signs OAuth state. |
| `TURSO_DATABASE_URL` | yes on Vercel | unset = local SQLite at `~/.inbox-forge/data.db` |
| `TURSO_AUTH_TOKEN` | yes on Vercel | |
