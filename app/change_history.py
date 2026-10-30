"""Append executed change scripts to the Master history SQL files."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from app.script_splitter import ScriptStatement, strip_leading_comments
from app.svn_client import SvnResult, is_svn_working_copy, svn_commit, svn_update

DEFAULT_HISTORY_DIR = Path(r"D:\__SVN_D__\100.db\02.DB_work\02.변경관리")
OBJECT_FILE_NAME = "Master Object 변경이력.sql"
DATA_FILE_NAME = "Master Data 변경이력.sql"
PROCEDURE_FILE_NAME = "Master Procedure 변경이력.sql"
HISTORY_AUTHOR = "전만기"

_NAMED_DDL_RE = re.compile(
    r"(?is)^(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:FORCE\s+|NOFORCE\s+)?"
    r"(?:(?:EDITIONABLE|NONEDITIONABLE)\s+)?(?:AND\s+(?:RESOLVE|COMPILE)\s+)?|"
    r"(?:DROP|ALTER)\s+)"
    r"(?P<kind>PACKAGE\s+BODY|TYPE\s+BODY|GLOBAL\s+TEMPORARY\s+TABLE|PRIVATE\s+TEMPORARY\s+TABLE|"
    r"UNIQUE\s+INDEX|BITMAP\s+INDEX|TABLE|INDEX|PROCEDURE|FUNCTION|PACKAGE|TRIGGER|"
    r"TYPE|VIEW|SEQUENCE|SYNONYM)\s+"
    r"(?:IF\s+(?:NOT\s+)?EXISTS\s+)?"
    r'(?:(?P<schema>"[^"]+"|[A-Za-z][\w$#]*)\s*\.\s*)?'
    r'(?P<name>"[^"]+"|[A-Za-z][\w$#]*)'
)
_DML_RE = re.compile(r"(?is)^(INSERT|UPDATE|DELETE|MERGE|TRUNCATE)\b")
_COMMENT_DDL_RE = re.compile(r"(?is)^COMMENT\s+ON\s+(TABLE|COLUMN)\b")
_DATE_LINE_RE = re.compile(r"^--\s+(\d{4})[.\-](\d{2})[.\-](\d{2})\b")
_MARKER_DATE_RE = re.compile(r"^-{10,}\s+(\d{4})-(\d{2})-(\d{2})\s+오픈팝")
_PROCEDURE_KINDS = {"PROCEDURE", "FUNCTION", "PACKAGE", "PACKAGE BODY"}
_OBJECT_KINDS = {
    "TABLE",
    "GLOBAL TEMPORARY TABLE",
    "PRIVATE TEMPORARY TABLE",
    "INDEX",
    "UNIQUE INDEX",
    "BITMAP INDEX",
    "TYPE",
    "TYPE BODY",
    "VIEW",
    "SEQUENCE",
    "SYNONYM",
    "TRIGGER",
}


@dataclass(frozen=True)
class HistoryPaths:
    objects: Path
    data: Path
    procedures: Path

    @classmethod
    def from_dir(cls, directory: Path) -> HistoryPaths:
        return cls(
            objects=directory / OBJECT_FILE_NAME,
            data=directory / DATA_FILE_NAME,
            procedures=directory / PROCEDURE_FILE_NAME,
        )

    @property
    def directory(self) -> Path:
        return self.objects.parent


@dataclass
class HistoryRecord:
    path: Path
    kind: str
    text: str


@dataclass
class HistoryWriteResult:
    records: list[HistoryRecord] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def default_history_paths() -> HistoryPaths:
    raw = os.environ.get("OCM_HISTORY_DIR")
    root = Path(raw) if raw else DEFAULT_HISTORY_DIR
    return HistoryPaths.from_dir(root)


def history_kind(sql: str) -> str | None:
    head = strip_leading_comments(sql).strip()
    if _COMMENT_DDL_RE.match(head):
        return None
    named = named_ddl(sql)
    if named is not None:
        kind, _name = named
        if kind in _PROCEDURE_KINDS:
            return "procedure"
        if kind in _OBJECT_KINDS:
            return "object"
        return None
    if _DML_RE.match(head):
        return "data"
    return None


def named_ddl(sql: str) -> tuple[str, str] | None:
    head = strip_leading_comments(sql).strip()
    matched = _NAMED_DDL_RE.match(head)
    if not matched:
        return None
    kind = re.sub(r"\s+", " ", matched.group("kind")).upper()
    return kind, _normalize_name(matched.group("name"))


def format_history_block(
    kind: str,
    items: list[ScriptStatement],
    reason: str,
    when: date,
    *,
    include_date: bool = True,
) -> str:
    stamp = when.isoformat()
    lines: list[str] = []
    if include_date:
        lines.append(f"-- {stamp}")
    comment = _reason_comment(reason)
    if comment:
        lines.append(comment)
    if kind == "procedure":
        names: list[str] = []
        seen: set[str] = set()
        for item in items:
            named = named_ddl(item.sql)
            if named is None:
                continue
            name = named[1]
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            names.append(name)
        if not names:
            return ""
        lines.extend(names)
    else:
        for item in items:
            lines.append(format_statement_sql(item))
            lines.append("")
        while lines and lines[-1] == "":
            lines.pop()
    if not any(line.strip() and not line.startswith("--") for line in lines):
        return ""
    lines.append("")
    lines.append(_apply_marker(stamp, "오픈팝 데모 반영"))
    lines.append(_apply_marker(stamp, "오픈팝 배포 반영"))
    return "\n".join(lines) + "\n"


def format_statement_sql(statement: ScriptStatement) -> str:
    sql = statement.sql.strip()
    if statement.mode == "plsql":
        return sql + "\n/"
    if sql.endswith(";"):
        return sql
    return sql + ";"


def append_change_history(
    statements: list[ScriptStatement],
    reason: str = "",
    *,
    paths: HistoryPaths | None = None,
    when: date | None = None,
) -> HistoryWriteResult:
    result = HistoryWriteResult()
    groups = _group_statements(statements)
    if not groups:
        return result
    target = paths or default_history_paths()
    history_dir = target.directory
    if is_svn_working_copy(history_dir):
        updated = svn_update(history_dir)
        _append_svn_log(result, updated)
        if updated is None or not updated.ok:
            if updated is None or not updated.error:
                result.errors.append("SVN 업데이트에 실패해 변경이력을 기록하지 않았습니다.")
            else:
                result.errors.append("SVN 업데이트에 실패해 변경이력을 기록하지 않았습니다. " + updated.error)
            return result
    stamp = when or date.today()
    mapping = (
        ("object", target.objects, groups.get("object") or []),
        ("data", target.data, groups.get("data") or []),
        ("procedure", target.procedures, groups.get("procedure") or []),
    )
    for kind, path, items in mapping:
        if not items:
            continue
        try:
            block = _write_history_block(path, kind, items, reason, stamp)
        except OSError as exc:
            result.errors.append(f"{path.name}에 기록하지 못했습니다. {exc}")
            continue
        if not block:
            continue
        result.records.append(HistoryRecord(path=path, kind=kind, text=block))
        result.messages.append(f"{path.name}에 기록했습니다.")
    if result.records and is_svn_working_copy(history_dir):
        committed = svn_commit(history_dir, history_commit_message(reason))
        _append_svn_log(result, committed)
        if committed is None or not committed.ok:
            extra = f" {committed.error}" if committed and committed.error else ""
            result.errors.append("변경이력은 저장했지만 SVN 커밋에 실패했습니다." + extra)
    return result


def _append_svn_log(result: HistoryWriteResult, svn: SvnResult | None) -> None:
    if svn is None:
        return
    lines = svn.log_lines()
    if svn.ok:
        result.messages.extend(lines)
    else:
        result.errors.extend(lines)


def history_commit_message(reason: str, author: str = HISTORY_AUTHOR) -> str:
    text = (reason or "").strip()
    if text:
        return f"({author})\n{text}"
    return f"({author})"


def _group_statements(statements: list[ScriptStatement]) -> dict[str, list[ScriptStatement]]:
    groups: dict[str, list[ScriptStatement]] = {}
    for statement in statements:
        if statement.mode == "skip":
            continue
        kind = history_kind(statement.sql)
        if kind is None:
            continue
        if kind == "object" and _COMMENT_DDL_RE.match(strip_leading_comments(statement.sql).strip()):
            continue
        groups.setdefault(kind, []).append(statement)
    return groups


def _reason_comment(reason: str) -> str:
    text = (reason or "").strip()
    if not text:
        return ""
    text = text.replace("*/", "* /")
    return f"/* {text} */"


def _apply_marker(stamp: str, label: str) -> str:
    return (
        f"------------------------------------------------  {stamp} {label} ({HISTORY_AUTHOR}) "
        "---------------------------------------------------------"
    )


def _normalize_name(raw: str) -> str:
    name = raw.strip()
    if name.startswith('"') and name.endswith('"') and len(name) >= 2:
        return name[1:-1].replace('""', '"')
    return name.upper()


def _write_history_block(
    path: Path,
    kind: str,
    items: list[ScriptStatement],
    reason: str,
    when: date,
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoding, newline = _file_style(path)
    existing = path.read_bytes() if path.exists() else b""
    same_date = False
    if existing:
        same_date = _last_entry_date(_decode_tail(existing, encoding)) == when
        if same_date:
            existing = _drop_trailing_markers_bytes(existing, encoding, when)
    block = format_history_block(kind, items, reason, when, include_date=not same_date)
    if not block:
        return ""
    payload = block.replace("\n", newline).encode(encoding, errors="replace")
    if existing:
        prefix = existing.rstrip(b"\r\n")
        payload = prefix + newline.encode(encoding) + payload
        path.write_bytes(payload)
    else:
        path.write_bytes(payload)
    return block


def _last_entry_date(text: str) -> date | None:
    last: date | None = None
    for raw in text.splitlines():
        line = raw.strip()
        matched = _DATE_LINE_RE.match(line) or _MARKER_DATE_RE.match(line)
        if matched is None:
            continue
        last = date(int(matched.group(1)), int(matched.group(2)), int(matched.group(3)))
    return last


def _decode_tail(data: bytes, encoding: str, size: int = 32768) -> str:
    chunk = data[-size:] if len(data) > size else data
    for skip in range(4):
        try:
            return chunk[skip:].decode(encoding)
        except UnicodeDecodeError:
            continue
    return chunk.decode(encoding, errors="replace")


def _drop_trailing_markers_bytes(data: bytes, encoding: str, when: date) -> bytes:
    stamp = when.isoformat()
    markers = [
        _apply_marker(stamp, "오픈팝 배포 반영").encode(encoding, errors="replace"),
        _apply_marker(stamp, "오픈팝 데모 반영").encode(encoding, errors="replace"),
    ]
    out = data.rstrip(b"\r\n")
    changed = True
    while changed:
        changed = False
        for marker in markers:
            if out.endswith(marker):
                out = out[: -len(marker)].rstrip(b"\r\n")
                changed = True
    return out


def _file_style(path: Path) -> tuple[str, str]:
    if not path.exists() or path.stat().st_size == 0:
        return "utf-8", "\n"
    sample = path.read_bytes()[:8192]
    newline = "\r\n" if b"\r\n" in sample else "\n"
    for encoding in ("utf-8-sig", "utf-8", "cp949"):
        try:
            sample.decode(encoding)
            return ("utf-8" if encoding == "utf-8-sig" else encoding), newline
        except UnicodeDecodeError:
            continue
    return "cp949", newline
