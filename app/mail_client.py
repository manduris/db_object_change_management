"""Read mailbox messages over POP3 SSL and keep only the requested sender domain."""

from __future__ import annotations

import poplib
import re
from dataclasses import dataclass
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape

from app.mail_store import DEFAULT_POP_HOST, DEFAULT_POP_PORT, MailSettings

_MAX_SCAN = 200
_MAX_KEEP = 50
_TOP_LINES = 800


@dataclass
class MailMessage:
    uid: str
    sender: str
    sender_email: str
    subject: str
    date: str
    body: str
    from_header: str = ""


class Pop3MailClient:
    def ping(self, settings: MailSettings) -> None:
        mailbox = self._connect(settings)
        try:
            mailbox.stat()
        finally:
            _close(mailbox)

    def fetch_unread(self, settings: MailSettings) -> list[MailMessage]:
        mailbox = self._connect(settings)
        try:
            messages: list[MailMessage] = []
            for number, uid in reversed(_uidl_list(mailbox)[-_MAX_SCAN:]):
                raw = _fetch_raw(mailbox, number, peek=True)
                if not raw:
                    continue
                messages.append(parse_rfc822(uid, raw))
                if len(messages) >= _MAX_KEEP:
                    break
            return messages
        finally:
            _close(mailbox)

    def mark_read(self, settings: MailSettings, uid: str) -> None:
        chosen = str(uid or "").strip()
        if not chosen:
            raise ValueError("읽을 메일을 선택하세요.")
        mailbox = self._connect(settings)
        try:
            for number, item_uid in _uidl_list(mailbox):
                if item_uid == chosen:
                    _fetch_raw(mailbox, number, peek=False)
                    return
        finally:
            _close(mailbox)

    def _connect(self, settings: MailSettings) -> poplib.POP3_SSL:
        host = settings.pop_host.strip() or DEFAULT_POP_HOST
        port = int(settings.pop_port or DEFAULT_POP_PORT)
        user = settings.email.strip()
        if not user:
            raise ValueError("메일 주소를 입력하세요. 아이디는 메일주소 전체입니다.")
        if "@" not in user:
            raise ValueError("아이디는 메일주소 전체로 입력하세요. 예: mgjeon@openmade.co.kr")
        if not settings.password:
            raise ValueError("웹메일 로그인 비밀번호를 입력하세요.")
        try:
            try:
                mailbox = poplib.POP3_SSL(host, port, timeout=25)
            except TypeError:
                mailbox = poplib.POP3_SSL(host, port)
            mailbox.user(user)
            mailbox.pass_(settings.password)
        except poplib.error_proto as exc:
            raise ValueError(
                "POP3 로그인에 실패했습니다. 메일주소 전체와 웹메일 비밀번호를 확인하세요."
            ) from exc
        except OSError as exc:
            raise ValueError(f"메일 서버에 연결하지 못했습니다. {exc}") from exc
        return mailbox


def parse_rfc822(uid: str, raw: bytes) -> MailMessage:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    from_header = _header(message, "From")
    name, address = _sender_parts(from_header)
    subject = _header(message, "Subject") or "(제목 없음)"
    return MailMessage(
        uid=str(uid),
        sender=name or address.split("@", 1)[0] or "(보낸 사람 없음)",
        sender_email=address,
        subject=subject,
        date=_format_date(message.get("Date")),
        body=extract_body(message),
        from_header=from_header,
    )


def sender_domain_matches(address: str, from_header: str, domain: str) -> bool:
    wanted = _domain(domain)
    if not wanted:
        return False
    candidates = [address]
    for _name, item in getaddresses([from_header or address]):
        if item:
            candidates.append(item)
    for item in candidates:
        host = item.rsplit("@", 1)[-1].strip().strip(">").lower()
        if host == wanted or host.endswith("." + wanted):
            return True
    return False


def extract_body(message: Message) -> str:
    plain = ""
    html = ""
    if message.is_multipart():
        for part in message.walk():
            disposition = str(part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            content_type = part.get_content_type()
            if content_type == "text/plain" and not plain:
                plain = _decode_part(part)
            elif content_type == "text/html" and not html:
                html = _decode_part(part)
    else:
        content_type = message.get_content_type()
        if content_type == "text/html":
            html = _decode_part(message)
        else:
            plain = _decode_part(message)
    return _clean_text(plain or strip_html(html))


def strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html or "")
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"(?i)</div>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return unescape(text)


def parse_uidl_lines(lines: list[bytes]) -> list[tuple[int, str]]:
    items: list[tuple[int, str]] = []
    for line in lines:
        text = line.decode("ascii", "ignore").strip() if isinstance(line, (bytes, bytearray)) else str(line).strip()
        parts = text.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        items.append((int(parts[0]), parts[1]))
    return items


def _uidl_list(mailbox: poplib.POP3) -> list[tuple[int, str]]:
    try:
        _resp, lines, _octets = mailbox.uidl()
    except poplib.error_proto as exc:
        raise ValueError("받은편지함 목록을 읽지 못했습니다.") from exc
    return parse_uidl_lines(list(lines or []))


def _fetch_raw(mailbox: poplib.POP3, number: int, *, peek: bool) -> bytes:
    lines = None
    if peek:
        try:
            _resp, lines, _octets = mailbox.top(number, _TOP_LINES)
        except poplib.error_proto:
            lines = None
    if lines is None:
        try:
            _resp, lines, _octets = mailbox.retr(number)
        except poplib.error_proto:
            return b""
    return b"\r\n".join(line if isinstance(line, (bytes, bytearray)) else str(line).encode("utf-8") for line in lines)


def _header(message: Message, name: str) -> str:
    value = message.get(name)
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value)))).strip()
    except Exception:
        return str(value).strip()


def _sender_parts(from_header: str) -> tuple[str, str]:
    people = getaddresses([from_header]) if from_header else []
    if not people:
        return "", ""
    name, address = people[0]
    return (name or "").strip(), (address or "").strip()


def _format_date(value: object) -> str:
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(str(value))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OverflowError):
        return str(value)


def _decode_part(part: Message) -> str:
    charset = part.get_content_charset() or "utf-8"
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    for encoding in (charset, "utf-8", "cp949", "euc-kr", "latin-1"):
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", "replace")


def _clean_text(text: str) -> str:
    lines: list[str] = []
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = line.strip()
        if _is_quote_boundary(stripped):
            break
        lines.append(line.rstrip())
    collapsed = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return collapsed


def _is_quote_boundary(line: str) -> bool:
    if line in {"--", "—", "-"}:
        return True
    return bool(
        re.match(
            r"^(>+|on .+wrote:|-----original message-----|보낸 사람\s*:|from\s*:|----- ?원본)",
            line,
            re.I,
        )
    )


def _domain(value: str) -> str:
    return value.strip().lstrip("@").lower()


def _close(mailbox: poplib.POP3) -> None:
    try:
        mailbox.quit()
    except Exception:
        try:
            mailbox.close()
        except Exception:
            pass
