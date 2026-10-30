"""Remember which POP3 messages the user marked as read."""

from __future__ import annotations

import json
import threading
from pathlib import Path


class MailArchive:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def uids(self) -> set[str]:
        with self._lock:
            return set(self._read())

    def mark_read(self, uid: str) -> None:
        chosen = str(uid or "").strip()
        if not chosen:
            raise ValueError("메일을 선택하세요.")
        with self._lock:
            items = self._read()
            if chosen not in items:
                items.append(chosen)
            self._write(items[-5000:])

    def _candidates(self) -> list[Path]:
        return [self.path, self.path.with_name("mail_confirmed.json")]

    def _read(self) -> list[str]:
        for path in self._candidates():
            found = self._read_path(path)
            if found is not None:
                return found
        return []

    def _read_path(self, path: Path) -> list[str] | None:
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if isinstance(raw, dict):
            if isinstance(raw.get("read_uids"), list):
                return [str(item) for item in raw["read_uids"] if str(item).strip()]
            if isinstance(raw.get("confirmed"), list):
                uids = []
                for item in raw["confirmed"]:
                    if isinstance(item, dict) and item.get("uid"):
                        uids.append(str(item["uid"]))
                    elif item:
                        uids.append(str(item))
                return uids
        if isinstance(raw, list):
            return [str(item) for item in raw if str(item).strip()]
        return []

    def _write(self, items: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 2, "read_uids": items}
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
