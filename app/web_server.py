"""Local web UI served by the Python standard-library HTTP server."""

from __future__ import annotations

import json
import re
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from app.connection_store import ConnectionStore
from app.models import ConnectionProfile, connection_fingerprint
from app.oracle_runner import (
    OracleSession,
    format_db_error,
    request_cancel,
    run_script,
    test_connection,
)
from app.sample_script import SAMPLE_SCRIPT
from app.script_splitter import (
    find_risks,
    preview_sql,
    split_oracle_script,
    statement_label,
    summarize_statements,
)

_SCHEMA_RE = re.compile(r"[A-Za-z][A-Za-z0-9_$#]{0,127}")
_PAGE = (
    __import__("pathlib").Path(__file__).resolve().parent / "web" / "index.html"
)
_MAX_BODY = 5_000_000


class WebApp:
    def __init__(self, store: ConnectionStore | None = None) -> None:
        self.store = store or ConnectionStore()
        self.sessions: dict[str, OracleSession] = {}
        self.pending: set[str] = set()
        self.connect_gen: dict[str, int] = {}
        self.profiles: list[ConnectionProfile] = []
        self.script_store: ConnectionProfile | None = None
        self.active_id: str | None = None
        self.selected_ids: set[str] = set()
        self.running_ids: set[str] = set()
        self.warning = ""
        self._state_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.running = False
        self.reload_profiles()

    def reload_profiles(self) -> None:
        data = self.store.load()
        with self._state_lock:
            self.profiles = data.connections
            self.script_store = data.script_store
            self.warning = data.warning
            if data.last_active_id and any(item.id == data.last_active_id for item in self.profiles):
                self.active_id = data.last_active_id
            elif self.active_id and any(item.id == self.active_id for item in self.profiles):
                pass
            elif self.profiles:
                self.active_id = self.profiles[0].id
            else:
                self.active_id = None
            known = {item.id for item in self.profiles}
            self.selected_ids = {item for item in self.selected_ids if item in known}

    def autoconnect(self) -> None:
        for profile in list(self.profiles):
            self.start_connect(profile.id)
        if self.script_store is not None:
            self.start_connect(self.script_store.id)

    def start_connect(self, profile_id: str) -> None:
        profile = self._profile(profile_id)
        if profile is None:
            return
        with self._state_lock:
            if profile_id in self.running_ids:
                return
            self.connect_gen[profile_id] = self.connect_gen.get(profile_id, 0) + 1
            generation = self.connect_gen[profile_id]
            self.pending.add(profile_id)
            snapshot = profile.snapshot()
        threading.Thread(
            target=self._connect_worker,
            args=(snapshot, generation),
            daemon=True,
            name=f"connect-{profile_id[:8]}",
        ).start()

    def _connect_worker(self, profile: ConnectionProfile, generation: int) -> None:
        session = OracleSession(profile)
        if profile.password_error or not profile.password:
            session.error = "저장된 비밀번호를 열 수 없습니다. 연결을 다시 저장하세요."
        else:
            try:
                session.connect()
            except Exception as exc:
                session.error = format_db_error(exc)
                session.close()
        with self._state_lock:
            if generation != self.connect_gen.get(profile.id):
                session.close()
                return
            self.pending.discard(profile.id)
            known = any(item.id == profile.id for item in self.profiles)
            if self.script_store is not None and self.script_store.id == profile.id:
                known = True
            if not known:
                session.close()
                self.sessions.pop(profile.id, None)
                return
            if profile.id in self.running_ids:
                session.close()
                return
            old = self.sessions.get(profile.id)
            if old is not None and old is not session:
                old.close()
            self.sessions[profile.id] = session

    def snapshot(self) -> dict:
        with self._state_lock:
            profiles = list(self.profiles)
            script_store = self.script_store.snapshot() if self.script_store else None
            active_id = self.active_id
            selected_ids = set(self.selected_ids)
            pending = set(self.pending)
            sessions = dict(self.sessions)
            warning = self.warning
            running = self.running
        cards = [
            _public_card(profile, active_id, selected_ids, pending, sessions, selectable=True)
            for profile in profiles
        ]
        return {
            "connections": cards,
            "script_store": (
                _public_card(script_store, active_id, selected_ids, pending, sessions, selectable=False)
                if script_store is not None
                else None
            ),
            "active_id": active_id,
            "selected_ids": [item.id for item in profiles if item.id in selected_ids],
            "running": running,
            "warning": warning,
        }

    def select_many(self, profile_ids: list[str]) -> None:
        chosen: list[str] = []
        for profile_id in profile_ids:
            if profile_id in chosen:
                continue
            self._reject_script_store(profile_id)
            profile = self._target_profile(profile_id)
            if profile is None:
                raise ValueError("연결을 찾을 수 없습니다.")
            session = self.sessions.get(profile_id)
            if session is None or not session.connected:
                raise ValueError(
                    f"{profile.name}: 아직 연결되지 않았습니다. 연결된 데이터베이스만 선택할 수 있습니다."
                )
            chosen.append(profile_id)
        with self._state_lock:
            self.selected_ids = set(chosen)
            if chosen:
                self.active_id = chosen[-1]

    def save_profile(self, payload: dict) -> ConnectionProfile:
        if not isinstance(payload, dict):
            raise ValueError("연결 정보가 올바르지 않습니다.")
        raw_id = payload.get("id")
        existing = self._target_profile(raw_id) if isinstance(raw_id, str) and raw_id else None
        profile = _profile_from_payload(payload, existing, self.profiles)
        if profile.id in self.running_ids:
            raise ValueError("이 연결로 스크립트를 실행하는 중입니다. 끝난 뒤에 저장하세요.")
        previous = connection_fingerprint(existing) if existing else None
        self.store.upsert(profile)
        self.reload_profiles()
        with self._state_lock:
            self.active_id = profile.id
        self.store.update_last_active(profile.id)
        if previous != connection_fingerprint(profile):
            self.start_connect(profile.id)
        return profile

    def delete_profile(self, profile_id: str) -> None:
        if profile_id in self.running_ids:
            raise ValueError("이 연결로 스크립트를 실행하는 중입니다. 끝난 뒤에 삭제하세요.")
        self.store.delete(profile_id)
        with self._state_lock:
            self.connect_gen[profile_id] = self.connect_gen.get(profile_id, 0) + 1
            self.pending.discard(profile_id)
            self.selected_ids.discard(profile_id)
            session = self.sessions.pop(profile_id, None)
            if self.active_id == profile_id:
                self.active_id = None
        if session is not None:
            session.close()
        self.reload_profiles()

    def save_script_store(self, payload: dict) -> ConnectionProfile:
        if not isinstance(payload, dict):
            raise ValueError("연결 정보가 올바르지 않습니다.")
        existing = self.script_store
        raw_id = payload.get("id")
        if existing is not None and isinstance(raw_id, str) and raw_id and raw_id != existing.id:
            raise ValueError("스크립트 저장 데이터베이스는 하나만 등록할 수 있습니다.")
        profile = _profile_from_payload(payload, existing, [])
        if profile.id in self.running_ids:
            raise ValueError("스크립트 저장 데이터베이스에 작업 중입니다. 끝난 뒤에 저장하세요.")
        previous = connection_fingerprint(existing) if existing else None
        replaced_id = existing.id if existing is not None and existing.id != profile.id else None
        self.store.save_script_store(profile)
        self.reload_profiles()
        if replaced_id:
            self._drop_session(replaced_id)
        if previous != connection_fingerprint(profile):
            self.start_connect(profile.id)
        return profile

    def delete_script_store(self) -> None:
        current = self.script_store
        if current is None:
            return
        if current.id in self.running_ids:
            raise ValueError("스크립트 저장 데이터베이스에 작업 중입니다. 끝난 뒤에 삭제하세요.")
        self.store.clear_script_store()
        self._drop_session(current.id)
        self.reload_profiles()

    def test_profile(self, payload: dict) -> str:
        existing = None
        raw_id = payload.get("id") if isinstance(payload, dict) else None
        if isinstance(raw_id, str) and raw_id:
            existing = self._profile(raw_id)
        profile = _profile_from_payload(payload, existing, self.profiles, for_test=True)
        info = test_connection(profile)
        return "연결되었습니다. " + info.summary()

    def inspect_script(self, script: str) -> dict:
        statements = split_oracle_script(script)
        items = []
        for index, statement in enumerate(statements, 1):
            if statement.mode == "skip":
                items.append(
                    {
                        "index": index,
                        "label": "건너뜀",
                        "mode": "skip",
                        "preview": statement.sql,
                        "note": statement.note,
                    }
                )
                continue
            items.append(
                {
                    "index": index,
                    "label": statement_label(statement.sql, statement.mode),
                    "mode": statement.mode,
                    "preview": preview_sql(statement.sql),
                    "note": statement.note,
                }
            )
        return {
            "summary": summarize_statements(statements),
            "statements": items,
            "risks": find_risks(statements),
            "empty": not any(item.mode != "skip" for item in statements),
        }

    def execute(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("요청 형식이 올바르지 않습니다.")
        script = payload.get("script")
        if not isinstance(script, str) or not script.strip():
            raise ValueError("실행할 스크립트를 입력하세요.")
        targets = self._connected_targets(self._target_ids(payload))
        statements = split_oracle_script(script)
        if not any(item.mode != "skip" for item in statements):
            raise ValueError("실행할 SQL 문장이 없습니다.")
        risks = find_risks(statements)
        if risks and not payload.get("confirm_risks"):
            return {
                "need_confirm": True,
                "risks": risks,
                "summary": summarize_statements(statements),
            }
        if not self._run_lock.acquire(blocking=False):
            raise ValueError("이미 스크립트를 실행 중입니다.")
        self.cancel_event.clear()
        with self._state_lock:
            self.running = True
            self.running_ids = {profile.id for profile, _session in targets}
        buckets: list[dict | None] = [None] * len(targets)

        def run_one(index: int, profile: ConnectionProfile, session: OracleSession) -> None:
            logs: list[dict] = []
            results: list[dict] = []
            stats: dict = {}

            def report(*event) -> None:
                kind = event[0]
                if kind == "log":
                    logs.append({"tag": event[1], "message": f"[{profile.name}] {event[2]}"})
                elif kind == "result":
                    results.append(
                        {
                            "title": f"{profile.name} · {event[1]}",
                            "columns": event[2],
                            "rows": event[3],
                            "truncated": event[4],
                        }
                    )
                elif kind == "finished":
                    stats.update(event[1])

            try:
                run_script(
                    session,
                    statements,
                    commit=bool(payload.get("commit", True)),
                    stop_on_error=bool(payload.get("stop_on_error", True)),
                    cancel_event=self.cancel_event,
                    report=report,
                )
            except Exception as exc:
                logs.append({"tag": "err", "message": f"[{profile.name}] {format_db_error(exc)}"})
                stats["fail"] = int(stats.get("fail") or 0) + 1
            buckets[index] = {
                "id": profile.id,
                "name": profile.name,
                "logs": logs,
                "results": results,
                "stats": stats,
            }

        threads = [
            threading.Thread(target=run_one, args=(index, profile, session), daemon=True)
            for index, (profile, session) in enumerate(targets)
        ]
        for thread in threads:
            thread.start()
        try:
            for thread in threads:
                thread.join()
        finally:
            with self._state_lock:
                self.running = False
                self.running_ids = set()
            self._run_lock.release()

        logs = []
        results = []
        stats = {"ok": 0, "fail": 0, "skip": 0, "cancelled": False, "targets": len(targets), "elapsed_ms": 0}
        for item in buckets:
            if item is None:
                continue
            logs.append({"tag": "info", "message": f"■ {item['name']}"})
            logs.extend(item["logs"])
            results.extend(item["results"])
            piece = item["stats"]
            stats["ok"] += int(piece.get("ok") or 0)
            stats["fail"] += int(piece.get("fail") or 0)
            stats["skip"] += int(piece.get("skip") or 0)
            stats["cancelled"] = bool(stats["cancelled"] or piece.get("cancelled"))
            stats["elapsed_ms"] = max(int(stats["elapsed_ms"]), int(piece.get("elapsed_ms") or 0))
            if piece.get("reconnect"):
                self.start_connect(item["id"])
        return {
            "need_confirm": False,
            "summary": summarize_statements(statements),
            "logs": logs,
            "results": results,
            "stats": stats,
        }

    def cancel(self) -> None:
        self.cancel_event.set()
        with self._state_lock:
            running_ids = list(self.running_ids)
        for profile_id in running_ids:
            session = self.sessions.get(profile_id)
            if session is not None:
                request_cancel(session)

    def commit(self, payload: dict | None) -> str:
        return self._transaction(self._target_ids(payload), "commit")

    def rollback(self, payload: dict | None) -> str:
        return self._transaction(self._target_ids(payload), "rollback")

    def _target_ids(self, payload: dict | None) -> list[str]:
        raw = None
        if isinstance(payload, dict):
            if "connection_ids" in payload:
                raw = payload.get("connection_ids")
            elif isinstance(payload.get("connection_id"), str) and payload.get("connection_id"):
                raw = [payload["connection_id"]]
        if raw is None:
            with self._state_lock:
                raw = list(self.selected_ids)
        if not isinstance(raw, list):
            raise ValueError("선택한 연결 정보가 올바르지 않습니다.")
        ids: list[str] = []
        for item in raw:
            if isinstance(item, str) and item and item not in ids:
                ids.append(item)
        return ids

    def _connected_targets(self, profile_ids: list[str]) -> list[tuple[ConnectionProfile, OracleSession]]:
        if not profile_ids:
            raise ValueError("연결된 데이터베이스를 선택하세요.")
        targets: list[tuple[ConnectionProfile, OracleSession]] = []
        for profile_id in profile_ids:
            self._reject_script_store(profile_id)
            profile = self._target_profile(profile_id)
            session = self.sessions.get(profile_id)
            if profile is None or session is None or not session.connected:
                name = profile.name if profile is not None else "선택한 연결"
                raise ValueError(f"{name}: 연결되지 않았습니다. 연결된 데이터베이스만 선택하세요.")
            targets.append((profile, session))
        return targets

    def _transaction(self, profile_ids: list[str], action: str) -> str:
        if self.running:
            raise ValueError("스크립트 실행이 끝난 뒤에 다시 시도하세요.")
        targets = self._connected_targets(profile_ids)
        errors: list[str] = []

        def work(profile: ConnectionProfile, session: OracleSession) -> None:
            try:
                with session.lock:
                    if session.conn is None:
                        raise ValueError("연결이 없습니다.")
                    if action == "commit":
                        session.conn.commit()
                    else:
                        session.conn.rollback()
            except Exception as exc:
                errors.append(f"{profile.name}: {format_db_error(exc)}")

        threads = [threading.Thread(target=work, args=item, daemon=True) for item in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if errors:
            raise ValueError("\n".join(errors))
        verb = "커밋" if action == "commit" else "롤백"
        names = ", ".join(profile.name for profile, _session in targets)
        return f"선택한 {len(targets)}개 연결({names})에 {verb}했습니다."

    def _profile(self, profile_id: str | None) -> ConnectionProfile | None:
        if not profile_id:
            return None
        target = self._target_profile(profile_id)
        if target is not None:
            return target
        if self.script_store is not None and self.script_store.id == profile_id:
            return self.script_store
        return None

    def _target_profile(self, profile_id: str | None) -> ConnectionProfile | None:
        if not profile_id:
            return None
        for profile in self.profiles:
            if profile.id == profile_id:
                return profile
        return None

    def _reject_script_store(self, profile_id: str) -> None:
        if self.script_store is not None and self.script_store.id == profile_id:
            raise ValueError("스크립트 저장 데이터베이스는 변경 스크립트 대상이 아닙니다.")

    def _drop_session(self, profile_id: str) -> None:
        with self._state_lock:
            self.connect_gen[profile_id] = self.connect_gen.get(profile_id, 0) + 1
            self.pending.discard(profile_id)
            self.selected_ids.discard(profile_id)
            session = self.sessions.pop(profile_id, None)
        if session is not None:
            session.close()

    def close(self) -> None:
        for session in list(self.sessions.values()):
            session.close()


def _public_card(
    profile: ConnectionProfile,
    active_id: str | None,
    selected_ids: set[str],
    pending: set[str],
    sessions: dict[str, OracleSession],
    *,
    selectable: bool,
) -> dict:
    status, message, info = _status_of(profile.id, pending, sessions)
    return {
        "id": profile.id,
        "name": profile.name,
        "host": profile.host,
        "port": profile.port,
        "identifier_type": profile.identifier_type,
        "identifier": profile.identifier,
        "username": profile.username,
        "current_schema": profile.current_schema,
        "dsn": profile.dsn,
        "target": profile.target_text(),
        "active": selectable and profile.id == active_id,
        "selected": selectable and profile.id in selected_ids and status == "connected",
        "status": status,
        "message": message,
        "info": info,
    }


def _status_of(profile_id: str, pending: set[str], sessions: dict[str, OracleSession]) -> tuple[str, str, str]:
    if profile_id in pending:
        return "connecting", "연결하는 중...", ""
    session = sessions.get(profile_id)
    if session and session.connected and session.info is not None:
        return "connected", "", session.info.summary()
    if session and session.error:
        return "failed", session.error, ""
    return "disconnected", "", ""


def _profile_from_payload(
    payload: dict,
    existing: ConnectionProfile | None,
    profiles: list[ConnectionProfile],
    *,
    for_test: bool = False,
) -> ConnectionProfile:
    if not isinstance(payload, dict):
        raise ValueError("연결 정보가 올바르지 않습니다.")
    name = _clean(payload.get("name"))
    host = _clean(payload.get("host"))
    identifier = _clean(payload.get("identifier"))
    username = _clean(payload.get("username"))
    schema = _clean(payload.get("current_schema"))
    dsn = _clean(payload.get("dsn"))
    if not name:
        raise ValueError("연결 이름을 입력하세요.")
    port = _port(payload.get("port"))
    if not dsn:
        if not host:
            raise ValueError("호스트를 입력하세요. DSN을 쓰는 경우에는 DSN만 입력해도 됩니다.")
        if not identifier:
            raise ValueError("서비스명 또는 SID를 입력하세요.")
    if not username:
        raise ValueError("사용자 이름을 입력하세요.")
    if schema and not _SCHEMA_RE.fullmatch(schema):
        raise ValueError("현재 스키마는 영문으로 시작하고 영문, 숫자, _, $, # 만 사용할 수 있습니다.")
    typed = payload.get("password")
    if isinstance(typed, str) and typed:
        password = typed
    elif existing and existing.password and not existing.password_error:
        password = existing.password
    else:
        raise ValueError("비밀번호를 입력하세요.")
    profile_id = existing.id if existing else str(uuid.uuid4())
    if not for_test:
        folded = name.casefold()
        for profile in profiles:
            if profile.id != profile_id and profile.name.casefold() == folded:
                raise ValueError("같은 이름의 연결이 이미 있습니다.")
    identifier_type = payload.get("identifier_type")
    if identifier_type not in {"service_name", "sid"}:
        identifier_type = "service_name"
    return ConnectionProfile(
        id=profile_id,
        name=name,
        host=host,
        port=port,
        identifier_type=identifier_type,
        identifier=identifier,
        username=username,
        password=password,
        current_schema=schema,
        dsn=dsn,
    )


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _port(value: object) -> int:
    if value is None or value == "":
        return 1521
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("포트는 숫자여야 합니다.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("포트는 1부터 65535 사이여야 합니다.")
    return port


class AppHandler(BaseHTTPRequestHandler):
    app: WebApp
    page: bytes

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/", "/index.html"}:
            self._bytes(200, self.page, "text/html; charset=utf-8")
            return
        if path == "/api/state":
            self._json(200, self.app.snapshot())
            return
        if path == "/api/sample":
            self._json(200, {"script": SAMPLE_SCRIPT})
            return
        self._json(404, {"error": "주소를 찾을 수 없습니다."})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/script-store":
                profile = self.app.save_script_store(payload)
                self._json(200, {"ok": True, "id": profile.id})
                return
            if path == "/api/connections":
                profile = self.app.save_profile(payload)
                self._json(200, {"ok": True, "id": profile.id})
                return
            if path == "/api/connections/test":
                message = self.app.test_profile(payload)
                self._json(200, {"ok": True, "message": message})
                return
            if path == "/api/connections/select":
                raw_ids = payload.get("ids")
                if raw_ids is None:
                    raw_id = payload.get("id")
                    raw_ids = [raw_id] if isinstance(raw_id, str) and raw_id else []
                if not isinstance(raw_ids, list):
                    raise ValueError("선택한 연결 정보가 올바르지 않습니다.")
                self.app.select_many([item for item in raw_ids if isinstance(item, str)])
                self._json(200, {"ok": True})
                return
            if path == "/api/reconnect":
                raw_ids = payload.get("ids")
                if isinstance(raw_ids, list):
                    for profile_id in raw_ids:
                        if isinstance(profile_id, str) and profile_id:
                            self.app.start_connect(profile_id)
                else:
                    profile_id = payload.get("id")
                    if isinstance(profile_id, str) and profile_id:
                        self.app.start_connect(profile_id)
                    else:
                        self.app.autoconnect()
                self._json(200, {"ok": True})
                return
            if path == "/api/script/inspect":
                script = payload.get("script")
                if not isinstance(script, str):
                    raise ValueError("스크립트를 입력하세요.")
                self._json(200, self.app.inspect_script(script))
                return
            if path == "/api/script/execute":
                result = self.app.execute(payload)
                status = 409 if result.get("need_confirm") else 200
                self._json(status, result)
                return
            if path == "/api/script/cancel":
                self.app.cancel()
                self._json(200, {"ok": True})
                return
            if path == "/api/script/commit":
                self._json(200, {"ok": True, "message": self.app.commit(payload)})
                return
            if path == "/api/script/rollback":
                self._json(200, {"ok": True, "message": self.app.rollback(payload)})
                return
            self._json(404, {"error": "주소를 찾을 수 없습니다."})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": format_db_error(exc)})

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/script-store":
            try:
                self.app.delete_script_store()
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, {"ok": True})
            return
        prefix = "/api/connections/"
        if not path.startswith(prefix) or path == prefix:
            self._json(404, {"error": "주소를 찾을 수 없습니다."})
            return
        try:
            self.app.delete_profile(unquote(path[len(prefix) :]))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, {"ok": True})

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > _MAX_BODY:
            raise ValueError("요청이 너무 큽니다.")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("요청 형식이 올바르지 않습니다.") from exc
        if not isinstance(payload, dict):
            raise ValueError("요청 형식이 올바르지 않습니다.")
        return payload

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._bytes(status, body, "application/json; charset=utf-8")

    def _bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))


def load_page() -> bytes:
    return _PAGE.read_bytes()


def serve(host: str = "127.0.0.1", port: int = 8765, *, open_browser: bool = True) -> None:
    app = WebApp()
    if app.warning:
        print(app.warning, file=sys.stderr)
    page = load_page()
    handler = type("BoundHandler", (AppHandler,), {"app": app, "page": page})
    httpd = _bind_server(host, port, handler)
    chosen_host, chosen_port = httpd.server_address[:2]
    url = f"http://{chosen_host}:{chosen_port}/"
    threading.Thread(target=app.autoconnect, daemon=True).start()
    print(f"오라클 오브젝트 변경 관리: {url}")
    print("이 창을 닫거나 Ctrl+C 를 누르면 서버가 멈춥니다.")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")
    finally:
        httpd.server_close()
        app.close()


def _bind_server(host: str, port: int, handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    last_error: OSError | None = None
    for candidate in range(port, port + 10):
        try:
            return ThreadingHTTPServer((host, candidate), handler)
        except OSError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error
