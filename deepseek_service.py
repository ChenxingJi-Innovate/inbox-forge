"""DeepSeek client for the contact-aware dossier pipeline.

Text-only. Used by the OAuth-driven `/api/inbox/{id}/sweep` route: a new email
body arrives, we fold in the prior dossier and the last few summaries for that
contact, ask DeepSeek to (a) summarize the new email and (b) rewrite the
dossier, then return both.

Why DeepSeek here and Gemini elsewhere:
- This path is text-only (Gmail / Outlook APIs return parsed body text), so we
  don't need vision and don't want to burn Gemini's free-tier quota on it.
- DeepSeek's API is paid (cheap), no rate-limiting drama.
- Native Chinese is strong, which matches the default UI language.

The stateless screenshot path (`analyze_email` in gemini_service.py) still
uses Gemini because that one *does* need vision.
"""
import json
import os
from typing import Optional

from openai import OpenAI


_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY not set in env")
        _client = OpenAI(
            api_key=api_key,
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
    return _client


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
    "relationship_stage": "从 [待跟进, 跟进中, 等待对方, 已处理] 中选一个 (含义: 待跟进=对方发来邮件我还没回; 跟进中=正在来回沟通; 等待对方=我发出去等他回; 已处理=话题已闭环)"
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

Prior emails from this contact (newest first, for your reference only, do not repeat verbatim):
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
    "relationship_stage": "one of [Needs reply, In progress, Awaiting them, Resolved] (Needs reply = they wrote, I haven't answered; In progress = active back-and-forth; Awaiting them = I sent, waiting for response; Resolved = thread closed)"
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
    """Summarize a new email for a known contact, returning per-email summary
    plus a refreshed dossier. Backed by DeepSeek-chat over the OpenAI-compatible
    endpoint.
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
    resp = client.chat.completions.create(
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        response_format={"type": "json_object"},
    )

    out = (resp.choices[0].message.content or "").strip()
    if not out:
        raise RuntimeError("Empty response from DeepSeek")
    # Strip any stray markdown fences just in case.
    if out.startswith("```"):
        out = out.split("\n", 1)[-1]
        if out.rstrip().endswith("```"):
            out = out.rsplit("```", 1)[0]
        out = out.strip()
        if out.startswith("json"):
            out = out[4:].lstrip()

    parsed = json.loads(out)
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
