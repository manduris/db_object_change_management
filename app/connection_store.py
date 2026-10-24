"""Persist connection profiles. Passwords are encrypted with Windows DPAPI."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.dpapi import protect_text, unprotect_text
from app.models import ConnectionProfile


def default_store_path() -> Path:
    override = os.environ.get("OCM_STORE_PATH")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    root = Path(appdata) if appdata else Path.home()
    return root / "OracleObjectChangeManager" / "connections.json"


@dataclass
class StoreData:
    connections: list[ConnectionProfile] = field(default_factory=list)
    last_active_id: str | None = None
    warning: str = ""
    script_store: ConnectionProfile | None = None


class ConnectionStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_store_path()
        self._lock = threading.RLock()

    def load(self) -> StoreData:
        with self._lock:
            return self._read()

    def upsert(self, profile: ConnectionProfile) -> None:
        with self._lock:
            data = self._read()
            for index, existing in enumerate(data.connections):
                if existing.id == profile.id:
                    data.connections[index] = profile
                    break
            else:
                data.connections.append(profile)
            self._write(data)

    def delete(self, profile_id: str) -> None:
        with self._lock:
            data = self._read()
            data.connections = [item for item in data.connections if item.id != profile_id]
            if data.last_active_id == profile_id:
                data.last_active_id = None
            self._write(data)

    def save_script_store(self, profile: ConnectionProfile) -> None:
        with self._lock:
            data = self._read()
            data.script_store = profile
            self._write(data)

    def clear_script_store(self) -> None:
        with self._lock:
            data = self._read()
            data.script_store = None
            self._write(data)

    def update_last_active(self, active_id: str | None) -> None:
        with self._lock:
            data = self._read()
            if active_id is not None and all(item.id != active_id for item in data.connections):
                active_id = None
            data.last_active_id = active_id
            self._write(data)

    def _read(self) -> StoreData:
        if not self.path.exists():
            return StoreData()
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            return StoreData(warning=f"연결 설정을 읽지 못했습니다.\n{exc}")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            self._quarantine()
            return StoreData(
                warning=(
                    "연결 설정 파일을 읽지 못해 새로 시작합니다. "
                    "기존 파일은 connections.broken.json 으로 옮겨 두었습니다."
                )
            )
        if not isinstance(raw, dict):
            self._quarantine()
            return StoreData(warning="연결 설정 형식이 올바르지 않아 새로 시작합니다.")

        connections: list[ConnectionProfile] = []
        skipped = 0
        items = raw.get("connections", [])
        if isinstance(items, list):
            for item in items:
                profile = _profile_from_dict(item)
                if profile is None:
                    skipped += 1
                    continue
                connections.append(profile)
        last_active = raw.get("last_active_id")
        if not isinstance(last_active, str) or all(item.id != last_active for item in connections):
            last_active = None
        script_store = None
        if "script_store" in raw and raw.get("script_store") is not None:
            script_store = _profile_from_dict(raw.get("script_store"))
            if script_store is None:
                skipped += 1
        warning = ""
        if skipped:
            warning = f"형식이 잘못된 연결 {skipped}개를 건너뛰었습니다."
        return StoreData(
            connections=connections,
            last_active_id=last_active,
            warning=warning,
            script_store=script_store,
        )

    def _write(self, data: StoreData) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "last_active_id": data.last_active_id,
            "connections": [_profile_to_dict(item) for item in data.connections],
            "script_store": _profile_to_dict(data.script_store) if data.script_store else None,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(self.path)

    def _quarantine(self) -> None:
        if not self.path.exists():
            return
        target = self.path.with_name("connections.broken.json")
        if target.exists():
            target = self.path.with_name(f"connections.broken.{int(time.time())}.json")
        try:
            self.path.replace(target)
        except OSError:
            pass


def _profile_to_dict(profile: ConnectionProfile) -> dict:
    return {
        "id": profile.id,
        "name": profile.name,
        "host": profile.host,
        "port": int(profile.port),
        "identifier_type": profile.identifier_type,
        "identifier": profile.identifier,
        "username": profile.username,
        "password_protected": protect_text(profile.password),
        "current_schema": profile.current_schema,
        "dsn": profile.dsn,
    }


def _profile_from_dict(item: object) -> ConnectionProfile | None:
    if not isinstance(item, dict):
        return None
    identifier_type = item.get("identifier_type") or "service_name"
    if identifier_type not in {"service_name", "sid"}:
        identifier_type = "service_name"
    try:
        port = int(item.get("port", 1521))
    except (TypeError, ValueError):
        port = 1521
    password = ""
    password_error = False
    protected = item.get("password_protected")
    if not isinstance(protected, str) or not protected:
        password_error = True
    else:
        try:
            password = unprotect_text(protected)
        except Exception:
            password_error = True
    profile_id = item.get("id")
    if not isinstance(profile_id, str) or not profile_id.strip():
        profile_id = str(uuid.uuid4())
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        name = "연결"
    return ConnectionProfile(
        id=profile_id,
        name=name.strip(),
        host=_text(item.get("host")),
        port=port,
        identifier_type=identifier_type,
        identifier=_text(item.get("identifier")),
        username=_text(item.get("username")),
        password=password,
        current_schema=_text(item.get("current_schema")),
        dsn=_text(item.get("dsn")),
        password_error=password_error,
    )


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""
