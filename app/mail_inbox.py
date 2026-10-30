"""Poll POP3, cache unread summaries, and raise a desktop notice on new mail."""

from __future__ import annotations

import os
import subprocess
import threading
from datetime import datetime
from html import escape
from typing import Protocol

from app.mail_archive import MailArchive
from app.mail_client import MailMessage, Pop3MailClient
from app.mail_store import MailSettings, MailStore
from app.mail_summary import notice_text, summarize_unread


class MailClient(Protocol):
    def ping(self, settings: MailSettings) -> None: ...
    def fetch_unread(self, settings: MailSettings) -> list[MailMessage]: ...
    def mark_read(self, settings: MailSettings, uid: str) -> None: ...


class MailInbox:
    def __init__(
        self,
        store: MailStore | None = None,
        client: MailClient | None = None,
        archive: MailArchive | None = None,
        *,
        notify=None,
    ) -> None:
        self.store = store or MailStore()
        self.client = client or Pop3MailClient()
        self.archive = archive or MailArchive(self.store.path.with_name("mail_read.json"))
        self._notify = notify or notify_desktop
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._uids: set[str] = set()
        self._seen_once = False
        self._digest: dict = {
            "unread": 0,
            "headline": "웹메일 비밀번호를 저장하면 메일을 확인합니다.",
            "digest": "아직 받은편지함을 확인하지 않았습니다. 웹메일 비밀번호를 저장한 뒤 지금 확인을 누르세요.",
            "groups": [],
            "messages": [],
        }
        self._error = ""
        self._last_check = ""
        self._notice = ""
        self._notice_id = 0
        self._checking = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="mail-poll")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2)
        self._thread = None

    def public_state(self) -> dict:
        settings = self.store.load()
        with self._lock:
            return {
                "configured": settings.configured,
                "email": settings.email,
                "filter_domain": settings.filter_domain,
                "unread": int(self._digest.get("unread") or 0),
                "headline": self._digest.get("headline") or "",
                "error": self._error,
                "last_check": self._last_check,
                "notice": self._notice,
                "notice_id": self._notice_id,
                "checking": self._checking,
            }

    def snapshot(self) -> dict:
        settings = self.store.load()
        state = self.public_state()
        with self._lock:
            digest = dict(self._digest)
        state.update(
            {
                "pop_host": settings.pop_host,
                "pop_port": settings.pop_port,
                "poll_seconds": settings.poll_seconds,
                "has_password": bool(settings.password) and not settings.password_error,
                "password_error": settings.password_error,
                "digest": digest.get("digest") or "",
                "groups": digest.get("groups") or [],
                "messages": digest.get("messages") or [],
            }
        )
        return state

    def save_settings(self, payload: dict) -> dict:
        current = self.store.load()
        email = _clean(payload.get("email")) or current.email
        host = (
            _clean(payload.get("pop_host"))
            or _clean(payload.get("imap_host"))
            or current.pop_host
        )
        password = payload.get("password")
        if not isinstance(password, str) or not password.strip():
            password = current.password
        else:
            password = password.strip()
        settings = MailSettings(
            email=email,
            password=password,
            pop_host=host,
            pop_port=_number(payload.get("pop_port") or payload.get("imap_port"), current.pop_port),
            filter_domain="",
            poll_seconds=max(15, _number(payload.get("poll_seconds"), current.poll_seconds)),
        )
        if not settings.email:
            raise ValueError("메일 주소를 입력하세요.")
        self.store.save(settings)
        return self.refresh()

    def test_settings(self, payload: dict) -> str:
        settings = self._merged(payload)
        self.client.ping(settings)
        return f"{settings.email} 에 연결되었습니다."

    def mark_read(self, uid: str) -> dict:
        chosen = str(uid or "").strip()
        if not chosen:
            raise ValueError("읽을 메일을 선택하세요.")
        with self._lock:
            items = list(self._digest.get("messages") or [])
        found = next((item for item in items if str(item.get("uid") or "") == chosen), None)
        if found is None:
            if chosen in self.archive.uids():
                return self.snapshot()
            raise ValueError("메일을 찾을 수 없습니다. 다시 확인하세요.")
        settings = self.store.load()
        self.client.mark_read(settings, chosen)
        self.archive.mark_read(chosen)
        return self.refresh()

    def refresh(self) -> dict:
        settings = self.store.load()
        if settings.password_error:
            self._set_idle(settings, "저장된 웹메일 비밀번호를 열 수 없습니다. 다시 저장하세요.", configured=False)
            return self.snapshot()
        if not settings.password:
            self._set_idle(settings, "", configured=False)
            return self.snapshot()
        with self._lock:
            self._checking = True
        try:
            fetched = self.client.fetch_unread(settings)
            read_uids = self.archive.uids()
            messages = [item for item in fetched if item.uid not in read_uids]
            digest = summarize_unread(messages, settings.filter_domain)
            current_uids = {item.uid for item in messages}
            with self._lock:
                arrived = [
                    item
                    for item in messages
                    if self._seen_once and item.uid not in self._uids and item.uid not in read_uids
                ]
                self._uids = current_uids
                self._seen_once = True
                self._digest = digest
                self._error = ""
                self._last_check = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self._checking = False
                if arrived:
                    self._notice = notice_text(arrived)
                    self._notice_id += 1
                    notice = self._notice
                else:
                    notice = ""
            if notice:
                self._notify("OMC연구소 메일", notice)
        except Exception as exc:
            with self._lock:
                self._checking = False
                self._error = str(exc)
                self._last_check = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return self.snapshot()

    def _poll_loop(self) -> None:
        self.refresh()
        while not self._stop.wait(self._wait_seconds()):
            self.refresh()

    def _wait_seconds(self) -> int:
        return max(15, int(self.store.load().poll_seconds or 45))

    def _merged(self, payload: dict) -> MailSettings:
        current = self.store.load()
        password = payload.get("password")
        if not isinstance(password, str) or not password.strip():
            password = current.password
        else:
            password = password.strip()
        return MailSettings(
            email=_clean(payload.get("email")) or current.email,
            password=password,
            pop_host=_clean(payload.get("pop_host")) or _clean(payload.get("imap_host")) or current.pop_host,
            pop_port=_number(payload.get("pop_port") or payload.get("imap_port"), current.pop_port),
            filter_domain="",
            poll_seconds=max(15, _number(payload.get("poll_seconds"), current.poll_seconds)),
        )

    def _set_idle(self, settings: MailSettings, error: str, *, configured: bool) -> None:
        with self._lock:
            if configured:
                self._digest = summarize_unread([], settings.filter_domain)
            else:
                self._digest = {
                    "unread": 0,
                    "headline": "웹메일 비밀번호를 저장하면 메일을 확인합니다.",
                    "digest": "아직 받은편지함을 확인하지 않았습니다. 웹메일 비밀번호를 저장한 뒤 지금 확인을 누르세요.",
                    "groups": [],
                    "messages": [],
                }
            self._error = error
            self._checking = False


def notify_desktop(title: str, body: str) -> None:
    if os.name != "nt" or not title:
        return
    xml = (
        "<toast><visual><binding template=\"ToastGeneric\">"
        f"<text>{escape(title)[:80]}</text><text>{escape(body)[:180]}</text>"
        "</binding></visual></toast>"
    )
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null; "
        f"$xml = @'\n{xml}\n'@; "
        "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument; "
        "$doc.LoadXml($xml); "
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($doc); "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('OMC연구소').Show($toast);"
    )
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    except OSError:
        pass


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)
