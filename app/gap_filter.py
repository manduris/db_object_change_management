"""Exclusion rules for ERwin model tables and subject areas."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

_MAIN_SA_RE = re.compile(r"[^A-Za-z]+")
_SA_COMPACT_RE = re.compile(r"[\s<>_]+")

MATCH_PREFIX = "prefix"
MATCH_SUFFIX = "suffix"
MATCH_PREFIX_SUFFIX = "prefix_suffix"
MATCH_CONTAINS = "contains"
MATCH_EXACT = "exact"
MATCH_KINDS = (MATCH_PREFIX, MATCH_SUFFIX, MATCH_PREFIX_SUFFIX, MATCH_CONTAINS, MATCH_EXACT)
MATCH_LABELS = {
    MATCH_PREFIX: "시작",
    MATCH_SUFFIX: "끝",
    MATCH_PREFIX_SUFFIX: "시작+끝",
    MATCH_CONTAINS: "포함",
    MATCH_EXACT: "일치",
}


def is_main_subject_area(name: str) -> bool:
    compact = _MAIN_SA_RE.sub("", name or "").casefold()
    return compact == "mainsubjectarea"


@dataclass
class TablePattern:
    match: str = MATCH_PREFIX
    value: str = ""
    end: str = ""

    def public(self) -> dict:
        kind = self.match if self.match in MATCH_KINDS else MATCH_PREFIX
        payload = {"match": kind, "value": self.value, "label": MATCH_LABELS.get(kind, kind)}
        if kind == MATCH_PREFIX_SUFFIX or self.end:
            payload["end"] = self.end
        return payload


@dataclass
class GapFilter:
    subject_areas: list[str] = field(default_factory=list)
    table_patterns: list[TablePattern] = field(default_factory=list)
    exclude_pk_order: bool = True

    def public(self) -> dict:
        return {
            "subject_areas": list(self.subject_areas),
            "table_patterns": [item.public() for item in self.table_patterns],
            "exclude_pk_order": self.exclude_pk_order,
        }


def default_gap_filter() -> GapFilter:
    return GapFilter(
        subject_areas=["Main Subject Area"],
        table_patterns=[
            TablePattern(MATCH_PREFIX, "_"),
            TablePattern(MATCH_SUFFIX, "_PRE"),
            TablePattern(MATCH_PREFIX, "querymedic."),
        ],
    )


def parse_gap_filter(raw: object) -> GapFilter:
    if not isinstance(raw, dict):
        return default_gap_filter()
    has_areas = "subject_areas" in raw
    has_patterns = "table_patterns" in raw
    has_pk_order = "exclude_pk_order" in raw
    if not has_areas and not has_patterns and not has_pk_order:
        return default_gap_filter()
    defaults = default_gap_filter()
    areas = _unique_names(raw.get("subject_areas") if has_areas else defaults.subject_areas)
    patterns = _parse_patterns(raw.get("table_patterns")) if has_patterns else list(defaults.table_patterns)
    exclude_pk_order = (
        _as_bool(raw.get("exclude_pk_order"), defaults.exclude_pk_order)
        if has_pk_order
        else defaults.exclude_pk_order
    )
    return GapFilter(subject_areas=areas, table_patterns=patterns, exclude_pk_order=exclude_pk_order)


def filter_from_payload(payload: object) -> GapFilter:
    if not isinstance(payload, dict):
        raise ValueError("제외 설정이 올바르지 않습니다.")
    areas = _unique_names(payload.get("subject_areas"))
    patterns = _parse_patterns(payload.get("table_patterns"))
    exclude_pk_order = (
        _as_bool(payload.get("exclude_pk_order"), True)
        if "exclude_pk_order" in payload
        else True
    )
    return GapFilter(subject_areas=areas, table_patterns=patterns, exclude_pk_order=exclude_pk_order)


def subject_area_excluded(name: str, settings: GapFilter | None = None) -> bool:
    filt = settings or default_gap_filter()
    text = (name or "").strip()
    if not text:
        return False
    return any(_subject_area_same(text, item) for item in filt.subject_areas)


def table_name_excluded(name: str, settings: GapFilter | None = None) -> bool:
    filt = settings or default_gap_filter()
    text = (name or "").strip()
    if not text:
        return False
    folded = text.casefold()
    for pattern in filt.table_patterns:
        needle = (pattern.value or "").strip()
        if not needle:
            continue
        kind = pattern.match if pattern.match in MATCH_KINDS else MATCH_PREFIX
        target = needle.casefold()
        if kind == MATCH_PREFIX and folded.startswith(target):
            return True
        if kind == MATCH_SUFFIX and folded.endswith(target):
            return True
        if kind == MATCH_PREFIX_SUFFIX:
            tail = (pattern.end or "").strip().casefold()
            if target and tail and folded.startswith(target) and folded.endswith(tail):
                return True
            continue
        if kind == MATCH_CONTAINS and target in folded:
            return True
        if kind == MATCH_EXACT and folded == target:
            return True
    return False


def table_in_scope(names: list[str], subject_areas: list[str], settings: GapFilter | None = None) -> bool:
    filt = settings or default_gap_filter()
    for name in names:
        if table_name_excluded(name, filt):
            return False
    areas = [item.strip() for item in subject_areas if str(item).strip()]
    if not areas:
        return True
    return any(not subject_area_excluded(item, filt) for item in areas)


def read_gap_state(path) -> dict:
    from pathlib import Path

    file = Path(path)
    if not file.exists():
        return {}
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_gap_state(path, payload: dict) -> None:
    from pathlib import Path

    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _unique_names(raw: object) -> list[str]:
    names: list[str] = []
    if not isinstance(raw, list):
        return names
    seen: set[str] = set()
    for item in raw:
        text = item.strip() if isinstance(item, str) else ""
        if not text:
            continue
        key = _subject_compact(text)
        if key in seen:
            continue
        seen.add(key)
        names.append(text)
    return names


def _parse_patterns(raw: object) -> list[TablePattern]:
    if not isinstance(raw, list):
        return []
    patterns: list[TablePattern] = []
    seen: set[tuple[str, str, str]] = set()
    for item in raw:
        if isinstance(item, str):
            match, value, end = MATCH_EXACT, item.strip(), ""
        elif isinstance(item, dict):
            match = str(item.get("match") or MATCH_PREFIX).strip().casefold()
            if match not in MATCH_KINDS:
                match = MATCH_PREFIX
            value = item.get("value")
            value = value.strip() if isinstance(value, str) else ""
            end = item.get("end")
            end = end.strip() if isinstance(end, str) else ""
        else:
            continue
        if not value:
            continue
        if match == MATCH_PREFIX_SUFFIX and not end:
            continue
        key = (match, value.casefold(), end.casefold())
        if key in seen:
            continue
        seen.add(key)
        patterns.append(TablePattern(match, value, end))
    return patterns


def _subject_area_same(left: str, right: str) -> bool:
    if is_main_subject_area(left) and is_main_subject_area(right):
        return True
    return _subject_compact(left) == _subject_compact(right)


def _subject_compact(name: str) -> str:
    return _SA_COMPACT_RE.sub("", name or "").casefold()


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in {"1", "true", "yes", "on", "exclude"}:
            return True
        if text in {"0", "false", "no", "off", "include"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default
