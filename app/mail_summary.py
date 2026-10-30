"""Group and summarize unread mail from one sender domain."""

from __future__ import annotations

import re
from collections import OrderedDict

from app.mail_client import MailMessage

_SENTENCE_SPLIT = re.compile(r"(?<=다)\s+|(?<=요)\s+|(?<=습니다)\s+|(?<=[.!?])\s+")


def summarize_unread(messages: list[MailMessage], domain: str = "") -> dict:
    items = [summarize_message(item) for item in messages]
    groups = _group(items)
    if not items:
        return {
            "unread": 0,
            "headline": "읽지 않은 메일이 없습니다.",
            "digest": "받은편지함을 확인했습니다. 읽지 않은 메일이 없습니다.",
            "groups": [],
            "messages": [],
        }

    lines = [f"읽지 않은 메일 {len(items)}건입니다."]
    for group in groups:
        titles = ", ".join(entry["subject"] for entry in group["messages"][:3])
        extra = f" 외 {len(group['messages']) - 3}건" if len(group["messages"]) > 3 else ""
        lines.append(f"{group['sender']} {group['count']}건: {titles}{extra}")
        for entry in group["messages"][:2]:
            if entry["summary"] and entry["summary"] != entry["subject"]:
                lines.append(f"  · {entry['summary']}")
    return {
        "unread": len(items),
        "headline": f"읽지 않은 메일 {len(items)}건",
        "digest": "\n".join(lines),
        "groups": groups,
        "messages": items,
    }


def summarize_message(message: MailMessage) -> dict:
    summary = first_sentences(message.body) or message.subject
    preview = _preview(message.body)
    return {
        "uid": message.uid,
        "sender": message.sender,
        "email": message.sender_email,
        "subject": message.subject,
        "date": message.date,
        "summary": summary,
        "preview": preview,
        "body": message.body or preview,
    }


def first_sentences(text: str, limit: int = 180) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if not compact:
        return ""
    if len(compact) <= limit:
        return compact
    parts = [part.strip() for part in _SENTENCE_SPLIT.split(compact) if part.strip()]
    chosen: list[str] = []
    for part in parts:
        chosen.append(part)
        joined = " ".join(chosen)
        if len(joined) >= 80 or len(chosen) >= 2:
            compact = joined
            break
    if len(compact) > limit:
        compact = compact[: limit - 1].rstrip() + "…"
    return compact


def notice_text(messages: list[MailMessage]) -> str:
    if not messages:
        return ""
    if len(messages) == 1:
        item = messages[0]
        return f"새 메일 · {item.sender}: {item.subject}"
    names = []
    seen = set()
    for item in messages:
        if item.sender in seen:
            continue
        seen.add(item.sender)
        names.append(item.sender)
        if len(names) == 3:
            break
    extra = f" 외 {len(messages) - 3}건" if len(messages) > 3 else ""
    return f"새 메일 {len(messages)}건 · {', '.join(names)}{extra}"


def _group(items: list[dict]) -> list[dict]:
    grouped: OrderedDict[str, dict] = OrderedDict()
    for item in items:
        key = (item.get("email") or item.get("sender") or "").lower()
        bucket = grouped.get(key)
        if bucket is None:
            bucket = {
                "sender": item["sender"],
                "email": item["email"],
                "count": 0,
                "messages": [],
            }
            grouped[key] = bucket
        bucket["count"] += 1
        bucket["messages"].append(item)
    return list(grouped.values())


def _preview(text: str, limit: int = 420) -> str:
    compact = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"
