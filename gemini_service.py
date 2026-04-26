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
from typing import Iterable

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
