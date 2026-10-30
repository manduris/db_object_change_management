"""Persist POP3 settings. Webmail passwords are encrypted with Windows DPAPI."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.dpapi import protect_text, unprotect_text

DEFAULT_EMAIL = "mgjeon@openmade.co.kr"
DEFAULT_DOMAIN = ""
DEFAULT_POP_HOST = "nmail.nanuminet.com"
DEFAULT_POP_PORT = 995
DEFAULT_POLL_SECONDS = 45
_OLD_GMAIL_HOSTS = {"imap.gmail.com", "gmail.com"}


def default_mail_store_path() -> Path:
    override = os.environ.get("OCM_MAIL_STORE_PATH")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    root = Path(appdata) if appdata else Path.home()
    return root / "OracleObjectChangeManager" / "mail.json"


@dataclass
class MailSettings:
    email: str = DEFAULT_EMAIL
    password: str = ""
    pop_host: str = DEFAULT_POP_HOST
    pop_port: int = DEFAULT_POP_PORT
    filter_domain: str = DEFAULT_DOMAIN
    poll_seconds: int = DEFAULT_POLL_SECONDS
    password_error: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.email.strip() and self.password and not self.password_error)


class MailStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_mail_store_path()
        self._lock = threading.RLock()

    def load(self) -> MailSettings:
        with self._lock:
            return self._read()

    def save(self, settings: MailSettings) -> None:
        with self._lock:
            self._write(settings)

    def _read(self) -> MailSettings:
        if not self.path.exists():
            return MailSettings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._quarantine()
            return MailSettings()
        if not isinstance(raw, dict):
            return MailSettings()
        email = _text(raw.get("email")) or DEFAULT_EMAIL
        pop_host = _text(raw.get("pop_host"))
        imap_host = _text(raw.get("imap_host"))
        password = ""
        password_error = False
        protected = raw.get("password_protected")
        if isinstance(protected, str) and protected:
            try:
                password = unprotect_text(protected)
            except Exception:
                password_error = True
        if pop_host:
            host = pop_host
            port = _port(raw.get("pop_port"), DEFAULT_POP_PORT)
        elif imap_host.lower() in _OLD_GMAIL_HOSTS:
            email = DEFAULT_EMAIL
            host = DEFAULT_POP_HOST
            port = DEFAULT_POP_PORT
            password = ""
            password_error = False
        else:
            host = imap_host or DEFAULT_POP_HOST
            port = _port(raw.get("pop_port") or raw.get("imap_port"), DEFAULT_POP_PORT)
        domain = _text(raw.get("filter_domain"))
        if domain.lower() == "openmade.co.kr":
            domain = ""
        return MailSettings(
            email=email,
            password=password,
            pop_host=host or DEFAULT_POP_HOST,
            pop_port=port,
            filter_domain=domain,
            poll_seconds=max(15, _port(raw.get("poll_seconds"), DEFAULT_POLL_SECONDS)),
            password_error=password_error,
        )

    def _write(self, settings: MailSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 2,
            "email": settings.email,
            "pop_host": settings.pop_host,
            "pop_port": int(settings.pop_port),
            "filter_domain": settings.filter_domain,
            "poll_seconds": int(settings.poll_seconds),
            "password_protected": protect_text(settings.password) if settings.password else "",
        }
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def _quarantine(self) -> None:
        if not self.path.exists():
            return
        target = self.path.with_name("mail.broken.json")
        if target.exists():
            target = self.path.with_name(f"mail.broken.{int(time.time())}.json")
        try:
            self.path.replace(target)
        except OSError:
            pass


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _port(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
