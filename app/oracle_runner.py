"""Open Oracle sessions and run change scripts."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from app.models import ConnectionProfile
from app.script_splitter import (
    ScriptStatement,
    is_ddl,
    preview_sql,
    statement_label,
    strip_leading_comments,
)

MAX_FETCH_ROWS = 1000
_SCHEMA_RE = re.compile(r"[A-Za-z][A-Za-z0-9_$#]{0,127}")
Report = Callable[..., None]


@dataclass
class SessionInfo:
    username: str
    schema: str
    db_name: str
    instance_name: str
    server_host: str

    def summary(self) -> str:
        return (
            f"사용자 {self.username} · 스키마 {self.schema} · "
            f"DB {self.db_name} · 인스턴스 {self.instance_name} · 서버 {self.server_host}"
        )


class OracleSession:
    def __init__(self, profile: ConnectionProfile) -> None:
        self.profile = profile
        self.conn: Any = None
        self.info: SessionInfo | None = None
        self.error: str | None = None
        self.lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self.conn is not None

    def connect(self) -> None:
        self.close()
        conn = open_connection(self.profile)
        try:
            try:
                conn.autocommit = False
            except Exception:
                pass
            self.info = read_session_info(conn)
            enable_dbms_output(conn)
            self.conn = conn
            self.error = None
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            self.conn = None
            self.info = None
            raise

    def close(self) -> None:
        conn = self.conn
        self.conn = None
        self.info = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def test_connection(profile: ConnectionProfile) -> SessionInfo:
    session = OracleSession(profile)
    try:
        session.connect()
        if session.info is None:
            raise RuntimeError("세션 정보를 읽지 못했습니다.")
        return session.info
    finally:
        session.close()


def open_connection(profile: ConnectionProfile) -> Any:
    oracledb = _require_oracledb()
    try:
        oracledb.defaults.fetch_lobs = False
    except Exception:
        pass
    if not profile.username.strip():
        raise ValueError("사용자 이름을 입력하세요.")
    if not profile.password:
        raise ValueError("비밀번호를 입력하세요.")
    dsn = build_dsn(profile)
    kwargs = {
        "user": profile.username.strip(),
        "password": profile.password,
        "dsn": dsn,
    }
    try:
        conn = oracledb.connect(**kwargs, tcp_connect_timeout=10)
    except TypeError:
        conn = oracledb.connect(**kwargs)
    try:
        _apply_schema(conn, profile.current_schema)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise
    return conn


def build_dsn(profile: ConnectionProfile) -> str:
    custom = profile.dsn.strip()
    if custom:
        return custom
    oracledb = _require_oracledb()
    host = profile.host.strip()
    identifier = profile.identifier.strip()
    if not host:
        raise ValueError("호스트 또는 DSN을 입력하세요.")
    if not identifier:
        raise ValueError("서비스명 또는 SID를 입력하세요.")
    if profile.identifier_type == "sid":
        return oracledb.makedsn(host, int(profile.port), sid=identifier)
    return oracledb.makedsn(host, int(profile.port), service_name=identifier)


def read_session_info(conn: Any) -> SessionInfo:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                SYS_CONTEXT('USERENV','SESSION_USER'),
                SYS_CONTEXT('USERENV','CURRENT_SCHEMA'),
                SYS_CONTEXT('USERENV','DB_NAME'),
                SYS_CONTEXT('USERENV','INSTANCE_NAME'),
                SYS_CONTEXT('USERENV','SERVER_HOST')
            FROM DUAL
            """
        )
        row = cursor.fetchone()
    values = [str(value) if value else "-" for value in row]
    return SessionInfo(*values)


def enable_dbms_output(conn: Any) -> None:
    try:
        with conn.cursor() as cursor:
            cursor.callproc("dbms_output.enable", [1_000_000])
    except Exception:
        pass


def read_dbms_output(cursor: Any) -> list[str]:
    lines: list[str] = []
    try:
        oracledb = _require_oracledb()
        status = cursor.var(oracledb.NUMBER)
        line = cursor.var(oracledb.STRING)
    except Exception:
        return lines
    for _ in range(5000):
        try:
            cursor.callproc("dbms_output.get_line", (line, status))
        except Exception:
            break
        code = status.getvalue()
        try:
            if code is None or int(code) != 0:
                break
        except (TypeError, ValueError):
            break
        value = line.getvalue()
        lines.append("" if value is None else str(value))
    else:
        lines.append("... DBMS_OUTPUT 이 너무 길어 이후 줄은 생략했습니다.")
    return lines


def request_cancel(session: OracleSession) -> None:
    conn = session.conn
    cancel = getattr(conn, "cancel", None) if conn is not None else None
    if cancel is None:
        return
    try:
        cancel()
    except Exception:
        pass


def run_script(
    session: OracleSession,
    statements: list[ScriptStatement],
    *,
    commit: bool,
    stop_on_error: bool,
    cancel_event: threading.Event,
    report: Report,
) -> None:
    import time

    started = time.perf_counter()
    stats = {"ok": 0, "fail": 0, "skip": 0, "cancelled": False, "reconnect": False, "elapsed_ms": 0.0}
    total = len(statements)
    with session.lock:
        had_connection = session.connected
        try:
            if session.conn is None or not _alive(session.conn):
                if had_connection:
                    report("log", "warn", "연결이 끊어져 다시 접속합니다.")
                session.connect()
        except Exception as exc:
            stats["fail"] = 1
            session.error = format_db_error(exc)
            report("log", "err", "연결할 수 없습니다. " + session.error)
            stats["elapsed_ms"] = (time.perf_counter() - started) * 1000
            report("finished", stats)
            return

        if any(is_ddl(item.sql) for item in statements if item.mode != "skip"):
            report(
                "log",
                "warn",
                "DDL(테이블, 인덱스, 프로시저, 함수, 패키지 등의 생성·변경·삭제)은 실행 즉시 반영되며 롤백되지 않습니다.",
            )

        for index, statement in enumerate(statements, 1):
            if cancel_event.is_set():
                stats["cancelled"] = True
                report("log", "warn", "사용자 요청으로 남은 문장을 실행하지 않습니다.")
                _safe_rollback(session, report)
                break
            report("progress", index, total)
            if statement.mode == "skip":
                stats["skip"] += 1
                report("log", "warn", f"[{index}/{total}] 건너뜀 ({statement.note})")
                report("log", "sql", preview_sql(statement.sql, 360))
                continue
            label = statement_label(statement.sql, statement.mode)
            started_stmt = time.perf_counter()
            try:
                with session.conn.cursor() as cursor:
                    cursor.execute(statement.sql)
                    elapsed = format_elapsed((time.perf_counter() - started_stmt) * 1000)
                    if cursor.description:
                        columns, rows, truncated = _fetch_rows(cursor)
                        report("result", f"{index}. {label}", columns, rows, truncated)
                        suffix = f"{len(rows)}행 조회"
                        if truncated:
                            suffix += f", {MAX_FETCH_ROWS}행까지만 표시"
                        report("log", "ok", f"[{index}/{total}] {label} 성공, {suffix}, {elapsed}")
                    else:
                        detail = _rowcount_text(statement.sql, cursor.rowcount)
                        report(
                            "log",
                            "ok",
                            f"[{index}/{total}] {label} 성공{detail}, {elapsed}",
                        )
                    report("log", "sql", preview_sql(statement.sql, 360))
                    for output in read_dbms_output(cursor):
                        report("log", "info", "DBMS_OUTPUT | " + output)
                stats["ok"] += 1
            except Exception as exc:
                elapsed = format_elapsed((time.perf_counter() - started_stmt) * 1000)
                if cancel_event.is_set():
                    stats["cancelled"] = True
                    report("log", "warn", f"[{index}/{total}] 실행 중인 문장을 중지했습니다. {elapsed}")
                    report("log", "sql", preview_sql(statement.sql, 360))
                    _safe_rollback(session, report)
                    break
                stats["fail"] += 1
                report("log", "err", f"[{index}/{total}] {label} 실패, {elapsed}")
                report("log", "sql", preview_sql(statement.sql, 360))
                report("log", "err", format_db_error(exc))
                if stop_on_error:
                    _safe_rollback(session, report)
                    report("log", "warn", "이후 문장은 실행하지 않았습니다.")
                    break

        if stats["cancelled"]:
            pass
        elif stats["fail"]:
            if not stop_on_error:
                report("log", "warn", "실패한 문장이 있어 자동 커밋하지 않았습니다.")
        elif commit:
            try:
                session.conn.commit()
                report("log", "ok", "커밋했습니다.")
            except Exception as exc:
                stats["fail"] += 1
                report("log", "err", "커밋하지 못했습니다. " + format_db_error(exc))
        else:
            report(
                "log",
                "warn",
                "자동 커밋이 꺼져 있습니다. 데이터 변경을 남기려면 커밋을 누르세요. DDL은 이미 반영되었습니다.",
            )

        if stats["cancelled"] and not _alive(session.conn):
            session.close()
            session.error = "실행 중지로 연결이 끊어졌습니다."
            stats["reconnect"] = True

    stats["elapsed_ms"] = (time.perf_counter() - started) * 1000
    report("finished", stats)


def format_elapsed(milliseconds: float) -> str:
    if milliseconds < 1000:
        return f"{milliseconds:.0f}ms"
    return f"{milliseconds / 1000:.1f}초"


def format_db_error(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    if "DPY-3010" in text:
        return (
            "이 Oracle 서버 버전은 추가 클라이언트 없이 접속할 수 없습니다. "
            "Oracle 12.1 이상이 필요합니다.\n" + text
        )
    if "DPY-6005" in text or "DPY-6001" in text:
        return "데이터베이스 서버에 연결하지 못했습니다. 호스트, 포트, 서비스명을 확인하세요.\n" + text
    if "ORA-01017" in text:
        return "사용자 이름 또는 비밀번호가 올바르지 않습니다.\n" + text
    if "ORA-12514" in text:
        return "리스너가 서비스명을 알지 못합니다. 서비스명을 확인하세요.\n" + text
    if "ORA-12541" in text:
        return "리스너에 연결하지 못했습니다. 호스트와 포트를 확인하세요.\n" + text
    if "ORA-12154" in text:
        return "연결 문자열을 해석하지 못했습니다. DSN 또는 서비스명을 확인하세요.\n" + text
    return text


def format_cell(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, bytes):
        shown = value[:32].hex()
        return "0x" + shown + ("..." if len(value) > 32 else "")
    text = str(value)
    if len(text) > 400:
        return text[:400] + "..."
    return text


def _apply_schema(conn: Any, schema: str) -> None:
    schema = schema.strip()
    if not schema:
        return
    if not _SCHEMA_RE.fullmatch(schema):
        raise ValueError("현재 스키마는 영문으로 시작하고 영문, 숫자, _, $, # 만 사용할 수 있습니다.")
    with conn.cursor() as cursor:
        cursor.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {schema}")


def _require_oracledb() -> Any:
    try:
        import oracledb
    except ImportError as exc:
        raise RuntimeError(
            "oracledb 패키지가 없습니다. run.bat 으로 프로그램을 실행해 주세요."
        ) from exc
    return oracledb


def _alive(conn: Any) -> bool:
    if conn is None:
        return False
    try:
        conn.ping()
        return True
    except Exception:
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1 FROM DUAL")
                cursor.fetchone()
            return True
        except Exception:
            return False


def _safe_rollback(session: OracleSession, report: Report) -> None:
    if session.conn is None:
        return
    try:
        session.conn.rollback()
        report(
            "log",
            "warn",
            "아직 커밋되지 않은 데이터 변경은 롤백했습니다. DDL은 롤백되지 않습니다.",
        )
    except Exception as exc:
        report("log", "err", "롤백하지 못했습니다. " + format_db_error(exc))


def _rowcount_text(sql: str, rowcount: int) -> str:
    head = strip_leading_comments(sql).lstrip().upper()
    if rowcount is None or rowcount < 0:
        return ""
    if head.startswith(("INSERT", "UPDATE", "DELETE", "MERGE")):
        return f", {rowcount}행"
    return ""


def _fetch_rows(cursor: Any) -> tuple[list[str], list[list[str]], bool]:
    columns = _unique_column_names([item[0] for item in cursor.description])
    rows: list[list[str]] = []
    truncated = False
    batch = cursor.fetchmany(200)
    while batch:
        for item in batch:
            if len(rows) >= MAX_FETCH_ROWS:
                truncated = True
                break
            rows.append([format_cell(value) for value in item])
        if truncated:
            break
        batch = cursor.fetchmany(200)
    return columns, rows, truncated


def _unique_column_names(names: list[Any]) -> list[str]:
    used: set[str] = set()
    result: list[str] = []
    for name in names:
        base = str(name or "COLUMN")
        candidate = base
        number = 2
        while candidate in used:
            candidate = f"{base}_{number}"
            number += 1
        used.add(candidate)
        result.append(candidate)
    return result
