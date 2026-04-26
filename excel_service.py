"""Per-user, per-language Excel storage.

Each user owns one workbook per language so the column headers always match the
language the user picked when saving. Files live at:

  data/users/{user_id}/records_en.xlsx
  data/users/{user_id}/records_zh.xlsx
  data/users/{user_id}/uploads/...
"""
import os
import threading
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).parent / "data"))
USERS_DIR = DATA_DIR / "users"

HEADERS = {
    "en": ["Date", "Customer", "Company", "Summary", "Follow-ups", "Status", "Screenshot", "Created at"],
    "zh": ["日期", "客户名", "公司", "摘要", "待跟进", "状态", "截图", "创建时间"],
}
SHEET_TITLE = {"en": "Email Records", "zh": "邮件记录"}
COL_WIDTHS = [12, 16, 22, 60, 50, 12, 30, 20]

# Pushpin-450 red as accent (matches workspace design language)
HEADER_FILL_HEX = "E60023"

_locks: dict[tuple[int, str], threading.Lock] = {}
_locks_master = threading.Lock()


def _norm_lang(lang: str) -> str:
    return "zh" if str(lang or "").lower() == "zh" else "en"


def _user_lock(user_id: int, lang: str) -> threading.Lock:
    key = (user_id, lang)
    with _locks_master:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def user_dir(user_id: int) -> Path:
    p = USERS_DIR / str(user_id)
    p.mkdir(parents=True, exist_ok=True)
    (p / "uploads").mkdir(exist_ok=True)
    return p


def excel_path(user_id: int, lang: str = "en") -> Path:
    lang = _norm_lang(lang)
    return user_dir(user_id) / f"records_{lang}.xlsx"


def uploads_dir(user_id: int) -> Path:
    return user_dir(user_id) / "uploads"


def _new_workbook(lang: str) -> Workbook:
    lang = _norm_lang(lang)
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_TITLE[lang]
    ws.append(HEADERS[lang])
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor=HEADER_FILL_HEX)
    for i, w in enumerate(COL_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
        cell = ws.cell(row=1, column=i)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.freeze_panes = "A2"
    return wb


def _ensure_file(user_id: int, lang: str):
    path = excel_path(user_id, lang)
    if not path.exists():
        wb = _new_workbook(lang)
        wb.save(path)


def append_record(user_id: int, record: dict, lang: str = "en") -> int:
    """Append one record to the user's language-specific workbook.
    Returns the new row number (1-indexed, header is row 1).
    """
    from datetime import datetime

    lang = _norm_lang(lang)
    _ensure_file(user_id, lang)
    path = excel_path(user_id, lang)
    with _user_lock(user_id, lang):
        wb = load_workbook(path)
        ws = wb.active
        row = [
            record.get("date", ""),
            record.get("customer_name", ""),
            record.get("company", ""),
            record.get("summary", ""),
            record.get("action_items", ""),
            record.get("status", ""),
            record.get("file_path", ""),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ]
        ws.append(row)
        new_row = ws.max_row
        for col_idx in (4, 5):
            c = ws.cell(row=new_row, column=col_idx)
            c.alignment = Alignment(wrap_text=True, vertical="top")
        wb.save(path)
        return new_row


def list_records(user_id: int, lang: str = "en") -> list[dict]:
    lang = _norm_lang(lang)
    path = excel_path(user_id, lang)
    if not path.exists():
        return []
    with _user_lock(user_id, lang):
        wb = load_workbook(path, read_only=True)
        ws = wb.active
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                continue
            if not row or all(v is None or v == "" for v in row):
                continue
            rows.append({
                "row": i + 1,
                "date": row[0] or "",
                "customer_name": row[1] or "",
                "company": row[2] or "",
                "summary": row[3] or "",
                "action_items": row[4] or "",
                "status": row[5] or "",
                "file_path": row[6] or "",
                "created_at": row[7] or "",
            })
        wb.close()
        rows.reverse()
        return rows


def list_customers(user_id: int, lang: str = "en") -> list[dict]:
    pairs: dict[tuple[str, str], int] = {}
    for r in list_records(user_id, lang):
        name = (r["customer_name"] or "").strip()
        company = (r["company"] or "").strip()
        if not name:
            continue
        key = (name, company)
        pairs[key] = pairs.get(key, 0) + 1
    customers = [{"name": n, "company": c, "count": cnt} for (n, c), cnt in pairs.items()]
    customers.sort(key=lambda x: (x["company"].lower(), x["name"].lower()))
    return customers
