"""Persist site shortcuts (name + URL) and named groups."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ALLOWED_SCHEMES = {"http", "https"}
DEFAULT_GROUP_NAMES = ("개발", "테스트", "운영")


def default_site_store_path() -> Path:
    override = os.environ.get("OCM_SITE_STORE_PATH")
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    root = Path(appdata) if appdata else Path.home()
    return root / "OracleObjectChangeManager" / "sites.json"


@dataclass(frozen=True)
class SiteGroup:
    id: str
    name: str

    def public(self) -> dict:
        return {"id": self.id, "name": self.name}


@dataclass(frozen=True)
class Site:
    id: str
    name: str
    url: str
    group_id: str = ""

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "url": self.url, "group_id": self.group_id}


class SiteStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_site_store_path()
        self._lock = threading.RLock()

    def list(self) -> list[Site]:
        with self._lock:
            return self._read()[1]

    def groups(self) -> list[SiteGroup]:
        with self._lock:
            return self._read()[0]

    def add_group(self, name: str) -> SiteGroup:
        clean = _clean_group_name(name)
        with self._lock:
            groups, sites = self._read()
            if any(item.name.casefold() == clean.casefold() for item in groups):
                raise ValueError("같은 이름의 그룹이 이미 있습니다.")
            group = SiteGroup(id=str(uuid.uuid4()), name=clean)
            groups.append(group)
            self._write(groups, sites)
            return group

    def rename_group(self, group_id: str, name: str) -> SiteGroup:
        if not group_id:
            raise ValueError("그룹을 찾을 수 없습니다.")
        clean = _clean_group_name(name)
        with self._lock:
            groups, sites = self._read()
            if any(item.id != group_id and item.name.casefold() == clean.casefold() for item in groups):
                raise ValueError("같은 이름의 그룹이 이미 있습니다.")
            for index, existing in enumerate(groups):
                if existing.id == group_id:
                    groups[index] = SiteGroup(id=group_id, name=clean)
                    self._write(groups, sites)
                    return groups[index]
            raise ValueError("그룹을 찾을 수 없습니다.")

    def delete_group(self, group_id: str) -> None:
        if not group_id:
            raise ValueError("그룹을 찾을 수 없습니다.")
        with self._lock:
            groups, sites = self._read()
            kept = [item for item in groups if item.id != group_id]
            if len(kept) == len(groups):
                raise ValueError("그룹을 찾을 수 없습니다.")
            sites = [
                Site(id=item.id, name=item.name, url=item.url, group_id="")
                if item.group_id == group_id
                else item
                for item in sites
            ]
            self._write(kept, sites)

    def upsert(
        self,
        name: str,
        url: str,
        site_id: str | None = None,
        group_id: str = "",
    ) -> Site:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("사이트명을 입력하세요.")
        if len(clean_name) > 80:
            raise ValueError("사이트명은 80자 이하여야 합니다.")
        clean_url = normalize_site_url(url)
        with self._lock:
            groups, items = self._read()
            clean_group = _resolved_group_id(groups, group_id)
            if site_id:
                for index, existing in enumerate(items):
                    if existing.id == site_id:
                        items[index] = Site(
                            id=site_id,
                            name=clean_name,
                            url=clean_url,
                            group_id=clean_group,
                        )
                        self._write(groups, items)
                        return items[index]
                raise ValueError("사이트를 찾을 수 없습니다.")
            site = Site(id=str(uuid.uuid4()), name=clean_name, url=clean_url, group_id=clean_group)
            items.append(site)
            self._write(groups, items)
            return site

    def delete(self, site_id: str) -> None:
        if not site_id:
            raise ValueError("사이트를 찾을 수 없습니다.")
        with self._lock:
            groups, items = self._read()
            kept = [item for item in items if item.id != site_id]
            if len(kept) == len(items):
                raise ValueError("사이트를 찾을 수 없습니다.")
            self._write(groups, kept)

    def reorder(
        self,
        ids: list[str],
        moved_id: str | None = None,
        group_id: object = None,
    ) -> list[Site]:
        if not isinstance(ids, list) or not ids or not all(isinstance(item, str) and item for item in ids):
            raise ValueError("사이트 순서가 올바르지 않습니다.")
        with self._lock:
            groups, items = self._read()
            by_id = {item.id: item for item in items}
            if len(ids) != len(by_id) or set(ids) != set(by_id):
                raise ValueError("사이트 순서가 올바르지 않습니다.")
            if moved_id:
                if moved_id not in by_id:
                    raise ValueError("사이트를 찾을 수 없습니다.")
                current = by_id[moved_id]
                new_group = current.group_id if group_id is None else _resolved_group_id(groups, group_id)
                by_id[moved_id] = Site(
                    id=current.id,
                    name=current.name,
                    url=current.url,
                    group_id=new_group,
                )
            ordered = [by_id[item_id] for item_id in ids]
            self._write(groups, ordered)
            return ordered

    def reorder_groups(self, ids: list[str]) -> list[SiteGroup]:
        if not isinstance(ids, list) or not ids or not all(isinstance(item, str) and item for item in ids):
            raise ValueError("그룹 순서가 올바르지 않습니다.")
        with self._lock:
            groups, sites = self._read()
            by_id = {item.id: item for item in groups}
            if len(ids) != len(by_id) or set(ids) != set(by_id):
                raise ValueError("그룹 순서가 올바르지 않습니다.")
            ordered = [by_id[item_id] for item_id in ids]
            self._write(ordered, sites)
            return ordered

    def _read(self) -> tuple[list[SiteGroup], list[Site]]:
        if not self.path.exists():
            groups = _default_groups()
            self._write(groups, [])
            return groups, []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._quarantine()
            groups = _default_groups()
            self._write(groups, [])
            return groups, []
        if not isinstance(raw, dict):
            groups = _default_groups()
            self._write(groups, [])
            return groups, []
        migrate = "groups" not in raw
        groups = _groups_from_raw(raw.get("groups")) if not migrate else _default_groups()
        rows = raw.get("sites")
        if not isinstance(rows, list):
            rows = []
        known = {item.id for item in groups}
        items: list[Site] = []
        seen: set[str] = set()
        for row in rows:
            site = _site_from_dict(row, known)
            if site is None or site.id in seen:
                continue
            seen.add(site.id)
            items.append(site)
        if migrate:
            self._write(groups, items)
        return groups, items

    def _write(self, groups: list[SiteGroup], items: list[Site]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "groups": [item.public() for item in groups],
            "sites": [item.public() for item in items],
        }
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def _quarantine(self) -> None:
        if not self.path.exists():
            return
        target = self.path.with_name("sites.broken.json")
        if target.exists():
            target = self.path.with_name(f"sites.broken.{int(time.time())}.json")
        try:
            self.path.replace(target)
        except OSError:
            pass


def normalize_site_url(value: object) -> str:
    raw = value.strip() if isinstance(value, str) else ""
    if not raw:
        raise ValueError("사이트 URL을 입력하세요.")
    if len(raw) > 2000:
        raise ValueError("사이트 URL이 너무 깁니다.")
    if "://" not in raw:
        guessed = urlsplit(raw)
        if guessed.scheme:
            raise ValueError("http 또는 https 주소만 저장할 수 있습니다.")
        raw = "https://" + raw.lstrip("/")
    parts = urlsplit(raw)
    scheme = (parts.scheme or "").casefold()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError("http 또는 https 주소만 저장할 수 있습니다.")
    if not parts.netloc:
        raise ValueError("올바른 사이트 URL을 입력하세요.")
    path = parts.path or ""
    return urlunsplit((scheme, parts.netloc, path, parts.query, parts.fragment))


def _default_groups() -> list[SiteGroup]:
    return [SiteGroup(id=str(uuid.uuid4()), name=name) for name in DEFAULT_GROUP_NAMES]


def _clean_group_name(value: object) -> str:
    name = value.strip() if isinstance(value, str) else ""
    if not name:
        raise ValueError("그룹명을 입력하세요.")
    if len(name) > 40:
        raise ValueError("그룹명은 40자 이하여야 합니다.")
    return name


def _resolved_group_id(groups: list[SiteGroup], group_id: object) -> str:
    if group_id is None or group_id == "":
        return ""
    if not isinstance(group_id, str):
        raise ValueError("그룹 정보가 올바르지 않습니다.")
    clean = group_id.strip()
    if not clean:
        return ""
    if any(item.id == clean for item in groups):
        return clean
    raise ValueError("그룹을 찾을 수 없습니다.")


def _groups_from_raw(raw: object) -> list[SiteGroup]:
    if not isinstance(raw, list):
        return []
    items: list[SiteGroup] = []
    seen: set[str] = set()
    names: set[str] = set()
    for row in raw:
        if not isinstance(row, dict):
            continue
        group_id = row.get("id")
        name = row.get("name")
        if not isinstance(group_id, str) or not group_id.strip():
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        group_id = group_id.strip()
        name = name.strip()
        folded = name.casefold()
        if group_id in seen or folded in names:
            continue
        seen.add(group_id)
        names.add(folded)
        items.append(SiteGroup(id=group_id, name=name))
    return items


def _site_from_dict(raw: object, known_groups: set[str]) -> Site | None:
    if not isinstance(raw, dict):
        return None
    site_id = raw.get("id")
    name = raw.get("name")
    url = raw.get("url")
    if not isinstance(site_id, str) or not site_id.strip():
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        clean_url = normalize_site_url(url)
    except ValueError:
        return None
    group_id = raw.get("group_id")
    if not isinstance(group_id, str) or group_id.strip() not in known_groups:
        group_id = ""
    else:
        group_id = group_id.strip()
    return Site(id=site_id.strip(), name=name.strip(), url=clean_url, group_id=group_id)
