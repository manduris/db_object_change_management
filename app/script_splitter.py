"""Split Oracle SQL*Plus scripts into statements that python-oracledb can run."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ScriptStatement:
    sql: str
    mode: str  # sql, plsql, skip
    note: str = ""


_PLSQL_RE = re.compile(
    r"(?is)^(?:"
    r"DECLARE\b"
    r"|BEGIN\b"
    r"|CREATE\s+"
    r"(?:OR\s+REPLACE\s+)?"
    r"(?:(?:EDITIONABLE|NONEDITIONABLE)\s+)?"
    r"(?:AND\s+(?:RESOLVE|COMPILE)\s+)?"
    r"(?:PACKAGE\s+BODY|TYPE\s+BODY|PROCEDURE|FUNCTION|PACKAGE|TRIGGER|TYPE|LIBRARY|JAVA)\b"
    r")"
)

_CREATE_RE = re.compile(
    r"(?is)^CREATE\s+"
    r"(?:OR\s+REPLACE\s+)?"
    r"(?:(?:EDITIONABLE|NONEDITIONABLE)\s+)?"
    r"(?:AND\s+(?:RESOLVE|COMPILE)\s+)?"
    r"(PACKAGE\s+BODY|TYPE\s+BODY|GLOBAL\s+TEMPORARY\s+TABLE|PRIVATE\s+TEMPORARY\s+TABLE|"
    r"UNIQUE\s+INDEX|BITMAP\s+INDEX|TABLE|INDEX|PROCEDURE|FUNCTION|PACKAGE|TRIGGER|"
    r"TYPE|VIEW|SEQUENCE|SYNONYM|JAVA|LIBRARY)\b"
)

_DROP_RE = re.compile(
    r"(?is)^DROP\s+"
    r"(PACKAGE\s+BODY|TYPE\s+BODY|TABLE|INDEX|PROCEDURE|FUNCTION|PACKAGE|TRIGGER|"
    r"TYPE|SEQUENCE|VIEW|SYNONYM)\b"
)

_ALTER_RE = re.compile(
    r"(?is)^ALTER\s+(TABLE|INDEX|PROCEDURE|FUNCTION|PACKAGE|TRIGGER|SEQUENCE|VIEW)\b"
)

_COMPILE_TARGET_RE = re.compile(
    r"(?is)^(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:FORCE\s+|NOFORCE\s+)?"
    r"(?:(?:EDITIONABLE|NONEDITIONABLE)\s+)?(?:AND\s+(?:RESOLVE|COMPILE)\s+)?|"
    r"ALTER\s+)"
    r"(?P<kind>PACKAGE\s+BODY|TYPE\s+BODY|PROCEDURE|FUNCTION|PACKAGE|TRIGGER|TYPE)\s+"
    r'(?:(?P<schema>"[^"]+"|[A-Za-z][\w$#]*)\s*\.\s*)?'
    r'(?P<name>"[^"]+"|[A-Za-z][\w$#]*)'
)

_SQLPLUS_RE = re.compile(
    r"(?i)^\s*(?:"
    r"SET|PROMPT|PRO|SPOOL|WHENEVER|REM|REMARK|COLUMN|COL|TTITLE|BTITLE|"
    r"BREAK|COMPUTE|DEFINE|DEF|UNDEFINE|UNDEF|PAUSE|EXIT|QUIT|CONNECT|CONN|"
    r"DISCONNECT|SHOW|HOST|CLEAR|STORE|REPHEADER|REPFOOTER|ACCEPT|VARIABLE|VAR|"
    r"PRINT|DESCRIBE|DESC"
    r")\b"
)

_EXEC_RE = re.compile(r"(?i)^EXEC(?:UTE)?\b\s*(.*)$")
_EXEC_IMMEDIATE_RE = re.compile(r"(?i)^EXECUTE\s+IMMEDIATE\b")
_INCLUDE_RE = re.compile(r"(?i)^\s*@\S+")
_SLASH_LINE_RE = re.compile(r"^\s*/\s*$")
_RISK_PLSQL_RE = re.compile(
    r"\b(?:DROP\s+(?:TABLE|INDEX|PROCEDURE|FUNCTION|PACKAGE|TRIGGER|SEQUENCE|VIEW|TYPE)"
    r"|TRUNCATE\s+TABLE)\b",
    re.IGNORECASE,
)

_CREATE_LABELS = {
    "TABLE": "테이블 생성",
    "GLOBAL TEMPORARY TABLE": "테이블 생성",
    "PRIVATE TEMPORARY TABLE": "테이블 생성",
    "INDEX": "인덱스 생성",
    "UNIQUE INDEX": "인덱스 생성",
    "BITMAP INDEX": "인덱스 생성",
    "PROCEDURE": "프로시저",
    "FUNCTION": "함수",
    "PACKAGE": "패키지",
    "PACKAGE BODY": "패키지 바디",
    "TRIGGER": "트리거",
    "TYPE": "타입",
    "TYPE BODY": "타입 바디",
    "VIEW": "뷰 생성",
    "SEQUENCE": "시퀀스 생성",
    "SYNONYM": "동의어 생성",
    "JAVA": "자바",
    "LIBRARY": "라이브러리",
}

_DROP_LABELS = {
    "TABLE": "테이블 삭제",
    "INDEX": "인덱스 삭제",
    "PROCEDURE": "프로시저 삭제",
    "FUNCTION": "함수 삭제",
    "PACKAGE": "패키지 삭제",
    "PACKAGE BODY": "패키지 바디 삭제",
    "TRIGGER": "트리거 삭제",
    "TYPE": "타입 삭제",
    "TYPE BODY": "타입 바디 삭제",
    "SEQUENCE": "시퀀스 삭제",
    "VIEW": "뷰 삭제",
    "SYNONYM": "동의어 삭제",
}

_ALTER_LABELS = {
    "TABLE": "테이블 변경",
    "INDEX": "인덱스 변경",
    "PROCEDURE": "프로시저 변경",
    "FUNCTION": "함수 변경",
    "PACKAGE": "패키지 변경",
    "TRIGGER": "트리거 변경",
    "SEQUENCE": "시퀀스 변경",
    "VIEW": "뷰 변경",
}

_PREFIX_LABELS = (
    ("INSERT", "데이터 입력"),
    ("UPDATE", "데이터 수정"),
    ("DELETE", "데이터 삭제"),
    ("MERGE", "데이터 병합"),
    ("SELECT", "조회"),
    ("WITH", "조회"),
    ("TRUNCATE", "테이블 비우기"),
    ("COMMENT", "코멘트"),
    ("GRANT", "권한 부여"),
    ("REVOKE", "권한 회수"),
    ("COMMIT", "커밋"),
    ("ROLLBACK", "롤백"),
    ("CALL", "호출"),
)


def strip_leading_comments(sql: str) -> str:
    index = 0
    length = len(sql)
    while index < length:
        while index < length and sql[index] in " \t\r\n":
            index += 1
        if sql.startswith("--", index):
            newline = sql.find("\n", index)
            if newline < 0:
                return ""
            index = newline + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                return ""
            index = end + 2
            continue
        break
    return sql[index:]


def classify_statement(text: str) -> str:
    head = strip_leading_comments(text).strip()
    if head and _PLSQL_RE.match(head):
        return "plsql"
    return "sql"


def preview_sql(sql: str, limit: int = 180) -> str:
    compact = re.sub(r"\s+", " ", sql).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def statement_label(sql: str, mode: str) -> str:
    head = strip_leading_comments(sql).strip()
    upper = head.upper()
    if mode == "plsql" or _PLSQL_RE.match(head):
        created = _create_label(upper)
        return created or "PL/SQL"
    for prefix, label in _PREFIX_LABELS:
        if upper.startswith(prefix):
            return label
    created = _create_label(upper)
    if created:
        return created
    dropped = _drop_label(upper)
    if dropped:
        return dropped
    altered = _alter_label(upper)
    if altered:
        return altered
    if upper.startswith("CREATE"):
        return "오브젝트 생성"
    if upper.startswith("DROP"):
        return "오브젝트 삭제"
    if upper.startswith("ALTER"):
        return "오브젝트 변경"
    return "실행"


def summarize_statements(statements: list[ScriptStatement]) -> str:
    counts: dict[str, int] = {}
    for statement in statements:
        label = "건너뜀" if statement.mode == "skip" else statement_label(statement.sql, statement.mode)
        counts[label] = counts.get(label, 0) + 1
    if not counts:
        return "실행할 문장 없음"
    return ", ".join(f"{name} {count}" for name, count in counts.items())


def is_ddl(sql: str) -> bool:
    head = strip_leading_comments(sql).lstrip().upper()
    return head.startswith(
        ("CREATE", "ALTER", "DROP", "TRUNCATE", "COMMENT", "GRANT", "REVOKE", "RENAME")
    )


def plsql_compile_target(sql: str) -> tuple[str, str, str] | None:
    """Return (ALL_ERRORS type, object name, schema) for compilable PL/SQL."""
    head = strip_leading_comments(sql).strip()
    matched = _COMPILE_TARGET_RE.match(head)
    if not matched:
        return None
    kind = re.sub(r"\s+", " ", matched.group("kind")).upper()
    if kind == "PACKAGE" and re.search(r"(?is)\bCOMPILE\s+BODY\b", head):
        kind = "PACKAGE BODY"
    schema = _ident_name(matched.group("schema")) if matched.group("schema") else ""
    name = _ident_name(matched.group("name"))
    if not name:
        return None
    return kind, name, schema


def _ident_name(raw: str | None) -> str:
    name = (raw or "").strip()
    if name.startswith('"') and name.endswith('"') and len(name) >= 2:
        return name[1:-1].replace('""', '"')
    return name.upper()


def find_risks(statements: list[ScriptStatement]) -> list[str]:
    found: list[str] = []
    for index, statement in enumerate(statements, 1):
        if statement.mode == "skip":
            continue
        compact = re.sub(r"\s+", " ", _rough_uncomment(statement.sql)).strip()
        reason = _risk_reason(compact.upper(), statement.mode)
        if not reason:
            continue
        shown = compact if len(compact) <= 140 else compact[:137] + "..."
        found.append(f"[{index}] {reason}: {shown}")
    return found


def split_oracle_script(script: str) -> list[ScriptStatement]:
    if not script:
        return []
    script = script.replace("\r\n", "\n").replace("\r", "\n")
    if script.startswith("\ufeff"):
        script = script[1:]

    statements: list[ScriptStatement] = []
    buf: list[str] = []
    has_sql = False
    index = 0
    length = len(script)
    state = "code"
    q_close = ""
    line_ws_only = True

    def line_bounds(pos: int) -> tuple[int, int]:
        start = script.rfind("\n", 0, pos) + 1
        end = script.find("\n", pos)
        if end < 0:
            end = length
        return start, end

    def emit(forced_mode: str | None = None) -> None:
        nonlocal has_sql
        raw = "".join(buf).strip()
        buf.clear()
        has_sql = False
        if not raw:
            return
        mode = forced_mode or classify_statement(raw)
        sql = raw
        if mode == "sql" and sql.endswith(";"):
            sql = sql[:-1].rstrip()
        if sql:
            statements.append(ScriptStatement(sql=sql, mode=mode))

    def append_code(text: str, counts: bool) -> None:
        nonlocal has_sql
        buf.append(text)
        if counts:
            has_sql = True

    while index < length:
        char = script[index]
        nxt = script[index + 1] if index + 1 < length else ""

        if state == "line_comment":
            buf.append(char)
            if char == "\n":
                state = "code"
                line_ws_only = True
            index += 1
            continue

        if state == "block_comment":
            buf.append(char)
            if char == "\n":
                line_ws_only = True
            if char == "*" and nxt == "/":
                buf.append(nxt)
                index += 2
                state = "code"
                continue
            index += 1
            continue

        if state == "sq":
            buf.append(char)
            if char == "\n":
                line_ws_only = True
            if char == "'":
                if nxt == "'":
                    buf.append(nxt)
                    index += 2
                    continue
                state = "code"
            index += 1
            continue

        if state == "dq":
            buf.append(char)
            if char == "\n":
                line_ws_only = True
            if char == '"':
                if nxt == '"':
                    buf.append(nxt)
                    index += 2
                    continue
                state = "code"
            index += 1
            continue

        if state == "qquote":
            buf.append(char)
            if char == "\n":
                line_ws_only = True
            if q_close and "".join(buf).endswith(q_close):
                state = "code"
            index += 1
            continue

        if char == "\n":
            buf.append(char)
            line_ws_only = True
            index += 1
            continue

        if char in " \t":
            buf.append(char)
            index += 1
            continue

        if line_ws_only:
            start, end = line_bounds(index)
            line = script[start:end]
            if _SLASH_LINE_RE.match(line):
                if has_sql:
                    emit()
                else:
                    buf.clear()
                    has_sql = False
                index = end
                line_ws_only = False
                continue
            if not has_sql:
                directive = _directive_statement(line)
                if directive is not None:
                    buf.clear()
                    has_sql = False
                    statements.append(directive)
                    index = end
                    line_ws_only = False
                    continue

        line_ws_only = False

        if char == "-" and nxt == "-":
            buf.append("--")
            index += 2
            state = "line_comment"
            continue

        if char == "/" and nxt == "*":
            buf.append("/*")
            index += 2
            state = "block_comment"
            continue

        if char == "'":
            if _q_prefix("".join(buf)) and index + 1 < length:
                delimiter = nxt
                pairs = {"[": "]", "(": ")", "{": "}", "<": ">"}
                q_close = pairs.get(delimiter, delimiter) + "'"
                append_code("'" + delimiter, True)
                index += 2
                state = "qquote"
                continue
            append_code("'", True)
            state = "sq"
            index += 1
            continue

        if char == '"':
            append_code('"', True)
            state = "dq"
            index += 1
            continue

        if char == ";":
            append_code(";", True)
            if classify_statement("".join(buf)) != "plsql":
                emit("sql")
            index += 1
            continue

        append_code(char, True)
        index += 1

    if has_sql:
        emit()
    return statements


def _q_prefix(text: str) -> int:
    if len(text) >= 2 and text[-2:].lower() == "nq":
        previous = text[-3] if len(text) >= 3 else ""
        if not previous or not (previous.isalnum() or previous in "_$#"):
            return 2
    if text and text[-1].lower() == "q":
        previous = text[-2] if len(text) >= 2 else ""
        if not previous or not (previous.isalnum() or previous in "_$#"):
            return 1
    return 0


def _directive_statement(line: str) -> ScriptStatement | None:
    stripped = line.strip()
    if _EXEC_IMMEDIATE_RE.match(stripped):
        return None
    matched = _EXEC_RE.match(stripped)
    if matched:
        body = (matched.group(1) or "").strip()
        if body.endswith(";"):
            body = body[:-1].rstrip()
        if not body:
            return ScriptStatement(sql=stripped, mode="skip", note="빈 EXEC 명령은 건너뜁니다.")
        sql = f"BEGIN\n  {body};\nEND;"
        return ScriptStatement(sql=sql, mode="plsql", note="EXEC")
    if _INCLUDE_RE.match(line):
        return ScriptStatement(
            sql=stripped,
            mode="skip",
            note="다른 파일을 포함하는 @ 명령은 지원하지 않습니다.",
        )
    if _SQLPLUS_RE.match(line):
        return ScriptStatement(sql=stripped, mode="skip", note="SQL*Plus 명령은 실행하지 않습니다.")
    return None


def _create_label(upper: str) -> str | None:
    matched = _CREATE_RE.match(upper)
    if not matched:
        return None
    return _CREATE_LABELS.get(matched.group(1).upper())


def _drop_label(upper: str) -> str | None:
    matched = _DROP_RE.match(upper)
    if not matched:
        return None
    return _DROP_LABELS.get(matched.group(1).upper())


def _alter_label(upper: str) -> str | None:
    matched = _ALTER_RE.match(upper)
    if not matched:
        return None
    return _ALTER_LABELS.get(matched.group(1).upper())


def _rough_uncomment(sql: str) -> str:
    without_block = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"--[^\n]*", " ", without_block)


def _risk_reason(upper: str, mode: str) -> str | None:
    if re.match(r"DROP\b", upper):
        return "오브젝트 삭제"
    if re.match(r"TRUNCATE\b", upper):
        return "테이블 비우기"
    if re.match(r"DELETE\b", upper) and not re.search(r"\bWHERE\b", upper):
        return "WHERE 없는 DELETE"
    if re.match(r"UPDATE\b", upper) and not re.search(r"\bWHERE\b", upper):
        return "WHERE 없는 UPDATE"
    if re.match(r"ALTER\s+TABLE\b", upper) and re.search(r"\bDROP\b", upper):
        return "테이블 컬럼 또는 제약 삭제"
    if mode == "plsql" and _RISK_PLSQL_RE.search(upper):
        return "PL/SQL 안의 삭제 또는 TRUNCATE"
    if mode == "plsql" and _plsql_delete_without_where(upper):
        return "PL/SQL 안의 WHERE 없는 DELETE"
    return None


def _plsql_delete_without_where(upper: str) -> bool:
    for matched in re.finditer(r"\bDELETE\s+FROM\b", upper):
        end = upper.find(";", matched.end())
        segment = upper[matched.start() : end if end >= 0 else None]
        if not re.search(r"\bWHERE\b", segment):
            return True
    return False
