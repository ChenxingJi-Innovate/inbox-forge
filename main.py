"""
Email Summarizer - FastAPI backend (multi-user, per-user-per-language Excel)

Public:
  GET  /                 -> serve frontend
  GET  /api/health       -> health
  POST /api/register     -> create account
  POST /api/login        -> login (sets cookie)
  POST /api/logout       -> logout
  GET  /api/me           -> current user

Authenticated (most accept ?lang=en|zh):
  POST /api/analyze      -> Gemini analyzes 1+ screenshots and/or pasted text
  POST /api/save         -> append row to user's per-language Excel
  GET  /api/records      -> list rows for the requested language
  GET  /api/customers    -> unique customers for the requested language
  GET  /api/download     -> download user's Excel file for the requested language
  GET  /uploads/{name}   -> serve user's uploaded screenshot
"""
import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import auth
import db
import excel_service
from gemini_service import analyze_email

load_dotenv()
db.init_db()

app = FastAPI(title="Email Summarizer")

BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def _norm_lang(lang: Optional[str]) -> str:
    return "zh" if str(lang or "").lower() == "zh" else "en"


@app.get("/")
async def root():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/health")
async def health():
    return {
        "gemini_key_set": bool(os.getenv("GEMINI_API_KEY")),
        "session_secret_set": bool(os.getenv("SESSION_SECRET")),
    }


# ---------- Auth ----------

@app.post("/api/register")
async def register(response: Response, email: str = Form(...), password: str = Form(...)):
    email = auth.validate_email(email)
    auth.validate_password(password)
    if db.get_user_by_email(email):
        raise HTTPException(status_code=400, detail="Email already registered")
    user_id = db.create_user(email, auth.hash_password(password))
    auth.issue_session(response, user_id)
    return {"success": True, "user": {"id": user_id, "email": email}}


@app.post("/api/login")
async def login(response: Response, email: str = Form(...), password: str = Form(...)):
    email = auth.validate_email(email)
    user = db.get_user_by_email(email)
    if not user or not auth.verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    auth.issue_session(response, user["id"])
    return {"success": True, "user": {"id": user["id"], "email": user["email"]}}


@app.post("/api/logout")
async def logout(response: Response):
    auth.clear_session(response)
    return {"success": True}


@app.get("/api/me")
async def me(user: dict = Depends(auth.current_user)):
    return {"user": {"id": user["id"], "email": user["email"]}}


# ---------- Email actions (auth required) ----------

MAX_TOTAL_BYTES = 40 * 1024 * 1024  # all images combined
MAX_FILES = 8


@app.post("/api/analyze")
async def analyze(
    files: List[UploadFile] = File(default=[]),
    text: str = Form(""),
    lang: str = Form("en"),
    user: dict = Depends(auth.current_user),
):
    lang = _norm_lang(lang)
    text = (text or "").strip()

    images_bytes: list[bytes] = []
    saved_paths: list[str] = []
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

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_name = f"{timestamp}_{(f.filename or 'image').replace(' ', '_')}"
        out_path = excel_service.uploads_dir(user["id"]) / safe_name
        out_path.write_bytes(content)
        saved_paths.append(safe_name)

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
        # Multiple screenshots are joined with ';' in the Excel cell
        "file_path": ";".join(saved_paths),
        "file_paths": saved_paths,
    }


@app.post("/api/save")
async def save(
    customer_name: str = Form(...),
    company: str = Form(""),
    date: str = Form(...),
    summary: str = Form(...),
    action_items: str = Form(""),
    status: str = Form(""),
    file_path: str = Form(""),
    lang: str = Form("en"),
    user: dict = Depends(auth.current_user),
):
    lang = _norm_lang(lang)
    if not customer_name.strip():
        raise HTTPException(status_code=400, detail="Customer name is required")
    try:
        row = excel_service.append_record(
            user["id"],
            {
                "customer_name": customer_name,
                "company": company,
                "date": date,
                "summary": summary,
                "action_items": action_items,
                "status": status,
                "file_path": file_path,
            },
            lang=lang,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Save failed: {e}")
    return {"success": True, "row": row}


@app.get("/api/records")
async def records(lang: str = "en", user: dict = Depends(auth.current_user)):
    return {"success": True, "records": excel_service.list_records(user["id"], lang=_norm_lang(lang))}


@app.get("/api/customers")
async def customers(lang: str = "en", user: dict = Depends(auth.current_user)):
    return {"success": True, "customers": excel_service.list_customers(user["id"], lang=_norm_lang(lang))}


@app.get("/api/download")
async def download(lang: str = "en", user: dict = Depends(auth.current_user)):
    lang = _norm_lang(lang)
    path = excel_service.excel_path(user["id"], lang)
    if not path.exists():
        wb = excel_service._new_workbook(lang)
        wb.save(path)
    filename = "email_records_zh.xlsx" if lang == "zh" else "email_records_en.xlsx"
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )


@app.get("/uploads/{filename}")
async def get_upload(filename: str, user: dict = Depends(auth.current_user)):
    """Serve a user's uploaded screenshot. Scoped to the user's own directory."""
    safe = Path(filename).name
    path = excel_service.uploads_dir(user["id"]) / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)


# ---------- 401 -> JSON (so frontend can detect) ----------

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
    print(" Email Summarizer running!")
    print(f"   Local:    http://127.0.0.1:8000")
    print(f"   Network:  http://{local_ip}:8000")
    print("=" * 50)
    print()

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
