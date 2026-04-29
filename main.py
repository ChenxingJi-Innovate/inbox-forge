"""
Email Summarizer - stateless API.

No accounts, no database, no on-disk persistence. The browser holds the
session's records in localStorage; download builds an .xlsx in memory.

Endpoints:
  GET  /                  -> serve frontend
  GET  /api/health        -> health
  POST /api/analyze       -> Gemini analyzes screenshots and/or pasted text
  POST /api/fetch-emails  -> IMAP fetch up to N unread emails
  POST /api/excel         -> build .xlsx from a JSON list of records
"""
import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from gemini_service import analyze_email
from excel_service import build_workbook
from imap_service import fetch_unread, guess_host

load_dotenv()

app = FastAPI(title="Email Summarizer (stateless)")

BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def _norm_lang(lang: str) -> str:
    return "zh" if str(lang or "").lower() == "zh" else "en"


@app.get("/")
async def root():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
async def health():
    return {"gemini_key_set": bool(os.getenv("GEMINI_API_KEY"))}


# ---------- Analyze ----------

MAX_TOTAL_BYTES = 40 * 1024 * 1024
MAX_FILES = 8


@app.post("/api/analyze")
async def analyze(
    files: List[UploadFile] = File(default=[]),
    text: str = Form(""),
    lang: str = Form("en"),
):
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


# ---------- IMAP fetch ----------

class FetchEmailsBody(BaseModel):
    email: str
    password: str
    host: str | None = None
    port: int = 993
    limit: int = 5


@app.post("/api/fetch-emails")
async def fetch_emails(body: FetchEmailsBody):
    addr = (body.email or "").strip()
    pwd = body.password or ""
    if not addr or not pwd:
        raise HTTPException(status_code=400, detail="Email and password are required")
    try:
        emails = fetch_unread(
            email_addr=addr,
            password=pwd,
            host=body.host or None,
            port=body.port or 993,
            limit=body.limit or 5,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fetch failed: {e}")
    return {"success": True, "emails": emails, "host": body.host or (guess_host(addr) or [None])[0]}


# ---------- Excel download ----------

class ExcelBody(BaseModel):
    records: list[dict]
    lang: str = "en"


@app.post("/api/excel")
async def excel(body: ExcelBody):
    lang = _norm_lang(body.lang)
    try:
        data = build_workbook(body.records or [], lang=lang)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Excel build failed: {e}")
    filename = "email_records_zh.xlsx" if lang == "zh" else "email_records_en.xlsx"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------- 4xx/5xx -> JSON ----------

@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


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
    print(" Email Summarizer (stateless) running!")
    print(f"   Local:    http://127.0.0.1:8000")
    print(f"   Network:  http://{local_ip}:8000")
    print("=" * 50)
    print()

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
