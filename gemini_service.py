"""
Gemini 2.5 Flash client for email analysis.

Supports:
- One or more screenshots (long emails span multiple shots)
- Plain text emails (paste / forward)
- Mixed: text + screenshots
- Bilingual output (en / zh)
"""
import io
import json
import os
from typing import Iterable, Optional

from google import genai
from google.genai import types
from PIL import Image

_client = None


def _get_client():
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set in .env")
        _client = genai.Client(api_key=api_key)
    return _client


PROMPT_EN = """You are a professional business email summarizer.

You may receive any combination of:
- One or more screenshots that together form a single email thread (treat them as one continuous conversation, in order).
- A plain-text email body (pasted or forwarded).

If both are given, fuse them into a single coherent picture.

Return strictly the following JSON (only the JSON, no markdown fences, no extra prose):

{
  "sender": "sender's name",
  "company": "sender's company (if any)",
  "subject": "email subject",
  "summary": "the most essential summary, 2 to 3 sentences, only the most critical points",
  "action_items": "follow-up items, one per line, each starting with • ; if none, write: None"
}

Naming rules (very important):
- Keep names and company names in their original form whenever possible.
- Translate only when there is a universally accepted localized name.
- When unsure, keep the original.

Summary requirements:
- 2 to 3 sentences, no bullet points.
- Must include: the customer's core ask + any key numbers / dates / model numbers.
- Strip greetings, pleasantries, signature blocks.
- Output language: English.
- Do not use em dashes (—) or en dashes (–). Use commas or periods.

Action items:
- Make who / when / what explicit.
- One per line, prefixed with • .
"""


PROMPT_ZH = """你是一个专业的商务邮件摘要助手。

你可能收到以下任意组合:
- 一张或多张截图(同一封长邮件可能跨多张截图,按顺序拼接理解)。
- 一段纯文本的邮件正文(粘贴或转发)。

如果同时给到,请融合成一份连贯的理解。

请严格按以下 JSON 格式返回(只返回 JSON 本身,不要任何其他文字或 markdown 代码块):

{
  "sender": "发件人姓名",
  "company": "发件人公司名(如果有)",
  "subject": "邮件主题",
  "summary": "最核心的摘要,2 到 3 句话,只提取最关键的信息",
  "action_items": "需要跟进的事项,每项一行以 • 开头;如果没有就写:无"
}

姓名 / 公司名翻译规则(非常重要):
- 如果人名或公司名没有公认的中文译名,保留英文原文不翻译。
- 只有广为人知的固定译名才用中文(例:Apple Inc. → 苹果公司)。
- 不确定时,一律保留英文原文。

摘要要求:
- 2 到 3 句话,不要 bullet points。
- 必须包含:客户核心诉求 + 关键数字/日期/型号(如有)。
- 无关细节(问候语、客套话)全部去掉。
- 邮件正文用中文输出,但人名/公司名/产品型号按上面规则处理。
- 不要使用破折号(em dash 或 half-em dash),统一用逗号或句号。

待跟进要求:
- 明确 who / when / what。
- 每项一行,以 • 开头。
"""


def _to_pil(image_bytes: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    return img


def analyze_email(
    images: Iterable[bytes] = (),
    text: str = "",
    lang: str = "en",
) -> dict:
    """Analyze any combination of screenshots + plain text.

    At least one of images or non-empty text must be provided.
    """
    images = list(images or [])
    text = (text or "").strip()
    if not images and not text:
        raise ValueError("Provide at least one screenshot or some email text")

    prompt = PROMPT_ZH if lang == "zh" else PROMPT_EN

    contents: list = [prompt]
    if text:
        header = "邮件正文:\n" if lang == "zh" else "Email body:\n"
        contents.append(header + text)
    for raw in images:
        contents.append(_to_pil(raw))

    client = _get_client()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=contents,
        config=types.GenerateContentConfig(
            temperature=0.3,
            response_mime_type="application/json",
        ),
    )

    out = response.text.strip() if response.text else ""
    if not out:
        raise RuntimeError("Empty response from Gemini")

    if out.startswith("```"):
        out = out.split("\n", 1)[-1]
        if out.rstrip().endswith("```"):
            out = out.rsplit("```", 1)[0]
        out = out.strip()
        if out.startswith("json"):
            out = out[4:].lstrip()

    return json.loads(out)


# Back-compat shim
def analyze_email_screenshot(image_bytes: bytes, lang: str = "en") -> dict:
    return analyze_email(images=[image_bytes], lang=lang)


# ---------- Contact-aware analysis (dossier + history) ----------

CONTEXT_PROMPT_ZH = """你是这位用户的智能邮件助手。

你为每位联系人维护一份「档案」: 一段关于「这个人和用户之间整体进展」的滚动摘要,以及该联系人留下的未完成事项。每当这位联系人发来新邮件,你需要做两件事:
(1) 总结这封新邮件本身。
(2) 在原档案基础上更新档案 (融合新信息、推进或完结旧 action item、修正关系阶段等)。

联系人当前档案 (可能为空, 表示第一次接触):
- 关系阶段: {relationship_stage}
- 当前主题: {current_topic}
- 档案摘要: {rolling_summary}
- 未完成事项:
{open_action_items}

该联系人的历史邮件 (由新到旧, 仅供你回忆上下文, 不要原文复述):
{history_block}

【本次新邮件】
主题: {subject}
正文:
{body}

请严格返回以下 JSON (不要 markdown 代码块, 不要任何额外文字):
{{
  "summary": "本封邮件 2 到 3 句摘要, 不用 bullet, 包含核心诉求与关键数字/日期",
  "action_items": "本封新增的待跟进事项, 每行以 • 开头; 若无写「无」",
  "updated_dossier": {{
    "rolling_summary": "更新后的整体档案, 2 到 4 句, 描述这位联系人与用户之间的整体进展",
    "open_action_items": "更新后的未完成清单 (合并新旧, 已完成的删掉), 每行以 • 开头; 若无写「无」",
    "current_topic": "当前正在沟通的主要议题, 一行内",
    "relationship_stage": "从 [初识, 在谈, 已合作, 已结束] 中选一个最贴切的"
  }}
}}

规则:
- 不要使用破折号 (em dash / en dash), 用逗号或句号。
- 姓名、公司名、产品型号原样保留, 除非有公认中文译名。
- 档案是滚动覆盖的, 不要丢失之前积累的关键信息, 但也不要让它无限增长 (rolling_summary 严格控制在 4 句以内)。
"""


CONTEXT_PROMPT_EN = """You are this user's intelligent email assistant.

For every contact, you maintain a rolling "dossier": a short narrative of where things stand between this user and that contact, plus any open action items. Each time that contact sends a new email, do two things:
(1) Summarize the new email itself.
(2) Update the dossier (merging in new information, closing or advancing open items, refining the relationship stage).

Contact's current dossier (may be empty for first contact):
- Relationship stage: {relationship_stage}
- Current topic: {current_topic}
- Rolling summary: {rolling_summary}
- Open action items:
{open_action_items}

Prior emails from this contact (newest first, for your reference only — do not repeat verbatim):
{history_block}

【New email】
Subject: {subject}
Body:
{body}

Return strictly the following JSON (no markdown fences, no extra prose):
{{
  "summary": "2 to 3 sentence summary of this email, no bullets, include core ask and key numbers / dates",
  "action_items": "newly-introduced follow-up items, one per line prefixed with • ; if none, write None",
  "updated_dossier": {{
    "rolling_summary": "updated dossier, 2 to 4 sentences describing overall state between user and contact",
    "open_action_items": "merged open-items list (drop completed, add new), one per line prefixed with • ; if none, write None",
    "current_topic": "main topic currently in conversation, one line",
    "relationship_stage": "one of [New, In discussion, Active, Closed]"
  }}
}}

Rules:
- Do not use em dashes or en dashes. Use commas or periods.
- Keep names, companies, product codes as given unless a widely accepted translation exists.
- The dossier is rolling-overwrite. Preserve accumulated context, but cap rolling_summary at 4 sentences.
"""


def _format_history_block(history: list[dict], lang: str) -> str:
    if not history:
        return "(无)" if lang == "zh" else "(none)"
    lines = []
    for h in history:
        when = h.get("received_at") or ""
        subj = (h.get("subject") or "").strip()
        summ = (h.get("summary") or "").strip()
        if lang == "zh":
            lines.append(f"[{when}] 主题: {subj}\n  摘要: {summ}")
        else:
            lines.append(f"[{when}] Subject: {subj}\n  Summary: {summ}")
    return "\n".join(lines)


def _dossier_field(dossier: Optional[dict], key: str, fallback_zh: str, fallback_en: str, lang: str) -> str:
    if dossier and (dossier.get(key) or "").strip():
        return dossier[key].strip()
    return fallback_zh if lang == "zh" else fallback_en


def analyze_with_context(
    body: str,
    subject: str = "",
    dossier: Optional[dict] = None,
    history: Optional[list[dict]] = None,
    lang: str = "zh",
) -> dict:
    """Summarize a new email *for a known contact*, updating the rolling dossier.

    `dossier` is the row from contact_dossier (may be None for first contact).
    `history` is a list of prior contact_emails rows (newest first), used as context.

    Returns: {summary, action_items, updated_dossier: {...}}
    """
    body = (body or "").strip()
    subject = (subject or "").strip()
    if not body and not subject:
        raise ValueError("Empty email content")

    template = CONTEXT_PROMPT_ZH if lang == "zh" else CONTEXT_PROMPT_EN
    prompt = template.format(
        relationship_stage=_dossier_field(dossier, "relationship_stage", "(尚未确定)", "(not yet determined)", lang),
        current_topic=_dossier_field(dossier, "current_topic", "(无)", "(none)", lang),
        rolling_summary=_dossier_field(dossier, "rolling_summary", "(尚无档案,这是第一次记录)", "(no prior dossier, this is the first record)", lang),
        open_action_items=_dossier_field(dossier, "open_action_items", "(无)", "(none)", lang),
        history_block=_format_history_block(history or [], lang),
        subject=subject or ("(无主题)" if lang == "zh" else "(no subject)"),
        body=body[:15000],
    )

    client = _get_client()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[prompt],
        config=types.GenerateContentConfig(
            temperature=0.3,
            response_mime_type="application/json",
        ),
    )

    out = (response.text or "").strip()
    if not out:
        raise RuntimeError("Empty response from Gemini")
    if out.startswith("```"):
        out = out.split("\n", 1)[-1]
        if out.rstrip().endswith("```"):
            out = out.rsplit("```", 1)[0]
        out = out.strip()
        if out.startswith("json"):
            out = out[4:].lstrip()

    parsed = json.loads(out)
    # Sanity-default the dossier subfields so callers can always write something.
    ud = parsed.get("updated_dossier") or {}
    parsed["updated_dossier"] = {
        "rolling_summary": (ud.get("rolling_summary") or "").strip(),
        "open_action_items": (ud.get("open_action_items") or "").strip(),
        "current_topic": (ud.get("current_topic") or "").strip(),
        "relationship_stage": (ud.get("relationship_stage") or "").strip(),
    }
    parsed["summary"] = (parsed.get("summary") or "").strip()
    parsed["action_items"] = (parsed.get("action_items") or "").strip()
    return parsed
