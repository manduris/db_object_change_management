"""Local web UI served by the Python standard-library HTTP server."""

from __future__ import annotations

import json
import re
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from app.change_history import HistoryPaths, append_change_history, default_history_paths
from app.connection_store import ConnectionStore
from app.db_catalog import fetch_db_tables
from app.erwin_model import DEFAULT_ERWIN_MODEL, ErwinModel, apply_erwin_change, load_erwin_model, save_model_json
from app.gap_filter import (
    default_gap_filter,
    filter_from_payload,
    parse_gap_filter,
    read_gap_state,
    write_gap_state,
)
from app.file_dialog import browse_erwin_path
from app.gap_analysis import (
    GapItem,
    apply_item_to_model,
    build_apply_ddl,
    compare_model_to_db,
    db_apply_sql,
    model_apply_op,
    summarize_gaps,
)
from app.mail_inbox import MailInbox
from app.mail_store import MailStore
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
from app.site_store import Site, SiteGroup, SiteStore

_SCHEMA_RE = re.compile(r"[A-Za-z][A-Za-z0-9_$#]{0,127}")
_PAGE = Path(__file__).resolve().parent / "web" / "index.html"
_FAVICON = Path(__file__).resolve().parent / "web" / "favicon.svg"
_MAX_BODY = 8_000_000


class WebApp:
    def __init__(
        self,
        store: ConnectionStore | None = None,
        history_paths: HistoryPaths | None = None,
        mail_inbox: MailInbox | None = None,
        site_store: SiteStore | None = None,
    ) -> None:
        self.store = store or ConnectionStore()
        self.history_paths = history_paths or default_history_paths()
        self.mail = mail_inbox or MailInbox(MailStore(self.store.path.with_name("mail.json")))
        self.sites = site_store or SiteStore(self.store.path.with_name("sites.json"))
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
        self.gap_model: ErwinModel | None = None
        self.gap_path: str = ""
        self.gap_connection_id: str | None = None
        self.gap_items: list[GapItem] = []
        self.gap_db_tables = []
        self.gap_filter = default_gap_filter()
        self.reload_profiles()
        self._restore_gap_state()

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
            if self.gap_connection_id and self.gap_connection_id not in known:
                self.gap_connection_id = None

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
            "mail": self.mail.public_state(),
            "gap": self._gap_public(),
            "sites": [item.public() for item in self.sites.list()],
            "site_groups": [item.public() for item in self.sites.groups()],
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
        profile.record_history = False
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
        reason = _clean(payload.get("reason"))
        save_history = bool(payload.get("save_history"))
        targets = self._connected_targets(self._target_ids(payload))
        statements = split_oracle_script(script)
        if not any(item.mode != "skip" for item in statements):
            raise ValueError("실행할 SQL 문장이 없습니다.")
        risks = find_risks(statements)
        if risks and not payload.get("confirm_risks"):
            return {
                "need_confirm": True,
                "reason": reason,
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
                    logs.append({"tag": event[1], "message": event[2]})
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
                logs.append({"tag": "err", "message": format_db_error(exc)})
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
        if reason:
            logs.append({"tag": "info", "message": f"변경 사유: {reason}"})
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
        history = self._record_history(statements, reason, buckets) if save_history else []
        logs.extend({"tag": tag, "message": message} for tag, message in history)
        return {
            "need_confirm": False,
            "reason": reason,
            "summary": summarize_statements(statements),
            "logs": logs,
            "results": results,
            "stats": stats,
        }

    def _record_history(self, statements, reason: str, buckets: list[dict | None]) -> list[tuple[str, str]]:
        history_id = next((item.id for item in self.profiles if item.record_history), "")
        if not history_id:
            return [("warn", "Master 변경이력 기본 DB가 지정되지 않아 기록하지 않았습니다.")]
        bucket = next((item for item in buckets if item and item.get("id") == history_id), None)
        if bucket is None:
            return [("warn", "Master 변경이력 기본 DB가 실행 대상이 아니라 기록하지 않았습니다.")]
        piece = bucket.get("stats") or {}
        if piece.get("cancelled") or not piece.get("ok") or piece.get("fail"):
            return []
        try:
            written = append_change_history(
                statements,
                reason,
                paths=self.history_paths,
            )
        except Exception as exc:
            return [("warn", "변경이력 파일에 기록하지 못했습니다. " + format_db_error(exc))]
        notes = [("ok", message) for message in written.messages]
        notes.extend(("warn", message) for message in written.errors)
        return notes

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

    def browse_model(self) -> dict:
        current = self.gap_path or (self.gap_model.path if self.gap_model else self._saved_gap_path())
        path = browse_erwin_path(current)
        if not path:
            return {"cancelled": True, **self._gap_public()}
        self.gap_path = path
        self._save_gap_path(path)
        return {"cancelled": False, **self._gap_public(), "path": path}

    def load_model(self, path: str) -> dict:
        text = _clean(path)
        if not text:
            raise ValueError("모델 파일 경로를 입력하세요.")
        model = load_erwin_model(text, self.gap_filter)
        self.gap_model = model
        self.gap_path = model.path
        self.gap_items = []
        self.gap_db_tables = []
        self._save_gap_path(model.path)
        return {"cancelled": False, **self._gap_public(), "tables": model.public()["tables"]}

    def select_gap_connection(self, profile_id: str) -> None:
        self._reject_script_store(profile_id)
        profile = self._target_profile(profile_id)
        if profile is None:
            raise ValueError("연결을 찾을 수 없습니다.")
        session = self.sessions.get(profile_id)
        if session is None or not session.connected:
            raise ValueError(f"{profile.name}: 아직 연결되지 않았습니다. 연결된 데이터베이스만 선택할 수 있습니다.")
        with self._state_lock:
            self.gap_connection_id = profile_id

    def analyze_gap(self, payload: dict | None) -> dict:
        if self.gap_model is None or not self.gap_model.tables:
            raise ValueError("먼저 ERwin 모델 파일을 지정하고 읽어 주세요.")
        profile_id = ""
        if isinstance(payload, dict):
            raw = payload.get("connection_id")
            if isinstance(raw, str):
                profile_id = raw.strip()
        if not profile_id:
            profile_id = self.gap_connection_id or ""
        if not profile_id:
            raise ValueError("갭 분석할 데이터베이스를 하나 선택하세요.")
        self.select_gap_connection(profile_id)
        profile = self._target_profile(profile_id)
        session = self.sessions.get(profile_id)
        if profile is None or session is None:
            raise ValueError("연결을 찾을 수 없습니다.")
        owner, tables = fetch_db_tables(session)
        items = compare_model_to_db(self.gap_model, tables, self.gap_filter)
        self.gap_items = items
        self.gap_db_tables = tables
        ddl = build_apply_ddl(items)
        summary = summarize_gaps(items)
        public_items = []
        for index, item in enumerate(items):
            row = item.public()
            row["index"] = index
            public_items.append(row)
        return {
            "ok": True,
            "connection_id": profile_id,
            "connection_name": profile.name,
            "schema": owner,
            "model": {
                "name": self.gap_model.name,
                "path": self.gap_model.path,
                "table_count": len(self.gap_model.tables),
                "column_count": sum(len(item.columns) for item in self.gap_model.tables),
            },
            "db_table_count": len(tables),
            "summary": summary,
            "items": public_items,
            "ddl": ddl,
        }

    def apply_gap_db(self, payload: dict | None) -> dict:
        item = self._gap_item(payload)
        sql = db_apply_sql(item)
        if not sql.strip():
            raise ValueError("이 항목은 DB에 반영할 DDL이 없습니다.")
        if self.gap_connection_id is None:
            raise ValueError("갭 분석할 데이터베이스를 하나 선택하세요.")
        request = payload if isinstance(payload, dict) else {}
        result = self.execute(
            {
                "script": sql,
                "reason": _clean(request.get("reason")),
                "connection_ids": [self.gap_connection_id],
                "commit": request.get("commit", True),
                "stop_on_error": request.get("stop_on_error", True),
                "confirm_risks": bool(request.get("confirm_risks")),
                "save_history": bool(request.get("save_history")),
            }
        )
        if result.get("need_confirm") or (result.get("stats") or {}).get("fail"):
            return result
        result["report"] = self.analyze_gap({"connection_id": self.gap_connection_id})
        return result

    def apply_gap_model(self, payload: dict | None) -> dict:
        item = self._gap_item(payload)
        model = self.gap_model
        if model is None:
            raise ValueError("먼저 ERwin 모델 파일을 지정하고 읽어 주세요.")
        suffix = Path(model.path).suffix.lower()
        if item.kind == "pk_diff" and suffix != ".json":
            raise ValueError("PK 구성은 ERwin 키 그룹을 자동으로 바꾸지 않습니다.")
        if suffix == ".json":
            apply_item_to_model(model, item, self.gap_db_tables)
            save_model_json(model)
            self.gap_model = load_erwin_model(model.path, self.gap_filter)
        else:
            op = model_apply_op(item, model, self.gap_db_tables)
            apply_erwin_change(model.path, op)
            self.gap_model = load_erwin_model(model.path, self.gap_filter)
        self.gap_path = self.gap_model.path
        self._save_gap_path(self.gap_model.path)
        report = self.analyze_gap({"connection_id": self.gap_connection_id}) if self.gap_connection_id else None
        return {
            "ok": True,
            "message": "모델에 반영했습니다.",
            "report": report,
            **self._gap_public(),
        }

    def _gap_item(self, payload: dict | None) -> GapItem:
        if not self.gap_items:
            raise ValueError("먼저 갭 분석을 하세요.")
        if not isinstance(payload, dict):
            raise ValueError("반영할 항목을 선택하세요.")
        index = payload.get("index")
        try:
            position = int(index)
        except (TypeError, ValueError) as exc:
            raise ValueError("반영할 항목을 선택하세요.") from exc
        if position < 0 or position >= len(self.gap_items):
            raise ValueError("선택한 항목을 찾을 수 없습니다. 갭 분석을 다시 하세요.")
        item = self.gap_items[position]
        kind = _clean(payload.get("kind"))
        table = _clean(payload.get("table"))
        column = _clean(payload.get("column"))
        if kind and kind != item.kind:
            raise ValueError("선택한 항목이 바뀌었습니다. 갭 분석을 다시 하세요.")
        if table and table != item.table:
            raise ValueError("선택한 항목이 바뀌었습니다. 갭 분석을 다시 하세요.")
        if column and column != item.column:
            raise ValueError("선택한 항목이 바뀌었습니다. 갭 분석을 다시 하세요.")
        return item

    def _gap_public(self) -> dict:
        model = self.gap_model
        loaded = model is not None and bool(model.tables)
        return {
            "path": (model.path if model else "") or self.gap_path,
            "name": model.name if loaded and model else "",
            "loaded": loaded,
            "table_count": len(model.tables) if loaded and model else 0,
            "column_count": sum(len(item.columns) for item in model.tables) if loaded and model else 0,
            "connection_id": self.gap_connection_id,
            "filter": self.gap_filter.public(),
        }

    def save_gap_filter(self, payload: dict | None) -> dict:
        self.gap_filter = filter_from_payload(payload)
        self._save_gap_state()
        return {
            "ok": True,
            "message": "추출 제외 설정을 저장했습니다. 모델 읽기와 갭 분석을 다시 하면 적용됩니다.",
            **self._gap_public(),
        }

    def reset_gap_filter(self) -> dict:
        self.gap_filter = default_gap_filter()
        self._save_gap_state()
        return {
            "ok": True,
            "message": "기본 제외 대상으로 되돌렸습니다. 모델 읽기를 다시 하면 적용됩니다.",
            **self._gap_public(),
        }

    def _gap_state_path(self) -> Path:
        return self.store.path.with_name("gap.json")

    def _saved_gap_path(self) -> str:
        value = read_gap_state(self._gap_state_path()).get("model_path")
        return value.strip() if isinstance(value, str) else ""

    def _save_gap_path(self, model_path: str) -> None:
        self._save_gap_state(model_path=model_path)

    def _restore_gap_state(self) -> None:
        raw = read_gap_state(self._gap_state_path())
        self.gap_filter = parse_gap_filter(raw)
        self.gap_path = str(DEFAULT_ERWIN_MODEL)

    def _save_gap_state(self, **updates) -> None:
        payload = {
            "model_path": self.gap_path,
            "subject_areas": list(self.gap_filter.subject_areas),
            "table_patterns": [item.public() for item in self.gap_filter.table_patterns],
            "exclude_pk_order": self.gap_filter.exclude_pk_order,
        }
        payload.update(updates)
        try:
            write_gap_state(self._gap_state_path(), payload)
        except OSError:
            pass

    def save_site(self, payload: dict) -> Site:
        site_id = payload.get("id")
        if site_id is not None and not isinstance(site_id, str):
            raise ValueError("사이트 정보가 올바르지 않습니다.")
        name = payload.get("name")
        url = payload.get("url")
        if not isinstance(name, str):
            raise ValueError("사이트명을 입력하세요.")
        if not isinstance(url, str):
            raise ValueError("사이트 URL을 입력하세요.")
        group_id = payload.get("group_id")
        if group_id is None:
            group_id = ""
        if not isinstance(group_id, str):
            raise ValueError("그룹 정보가 올바르지 않습니다.")
        return self.sites.upsert(
            name,
            url,
            site_id.strip() if site_id else None,
            group_id=group_id,
        )

    def save_site_group(self, payload: dict) -> dict:
        name = payload.get("name")
        if not isinstance(name, str):
            raise ValueError("그룹명을 입력하세요.")
        group_id = payload.get("id")
        if group_id is not None and not isinstance(group_id, str):
            raise ValueError("그룹 정보가 올바르지 않습니다.")
        if isinstance(group_id, str) and group_id.strip():
            return self.sites.rename_group(group_id.strip(), name).public()
        return self.sites.add_group(name).public()

    def delete_site(self, site_id: str) -> None:
        self.sites.delete(site_id)

    def delete_site_group(self, group_id: str) -> None:
        self.sites.delete_group(group_id)

    def reorder_sites(self, payload: dict) -> list[Site]:
        ids = payload.get("ids")
        if not isinstance(ids, list):
            raise ValueError("사이트 순서가 올바르지 않습니다.")
        moved_id = payload.get("moved_id")
        if moved_id is not None and not isinstance(moved_id, str):
            raise ValueError("사이트 정보가 올바르지 않습니다.")
        return self.sites.reorder(ids, moved_id=moved_id, group_id=payload.get("group_id"))

    def reorder_site_groups(self, payload: dict) -> list[SiteGroup]:
        ids = payload.get("ids")
        if not isinstance(ids, list):
            raise ValueError("그룹 순서가 올바르지 않습니다.")
        return self.sites.reorder_groups(ids)

    def close(self) -> None:
        self.mail.stop()
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
        "record_history": bool(profile.record_history),
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
        record_history=bool(payload.get("record_history")),
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
        if path in {"/favicon.ico", "/favicon.svg"}:
            self._bytes(200, _FAVICON.read_bytes(), "image/svg+xml")
            return
        if path == "/api/state":
            self._json(200, self.app.snapshot())
            return
        if path == "/api/sample":
            self._json(200, {"script": SAMPLE_SCRIPT})
            return
        if path == "/api/mail":
            self._json(200, self.app.mail.snapshot())
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
            if path == "/api/mail/settings":
                self._json(200, self.app.mail.save_settings(payload))
                return
            if path == "/api/mail/test":
                message = self.app.mail.test_settings(payload)
                self._json(200, {"ok": True, "message": message})
                return
            if path == "/api/mail/refresh":
                self._json(200, self.app.mail.refresh())
                return
            if path == "/api/mail/read":
                uid = payload.get("uid")
                if not isinstance(uid, str):
                    raise ValueError("읽을 메일을 선택하세요.")
                self._json(200, self.app.mail.mark_read(uid))
                return
            if path == "/api/gap/browse":
                self._json(200, self.app.browse_model())
                return
            if path == "/api/gap/load":
                path_value = payload.get("path")
                if not isinstance(path_value, str):
                    raise ValueError("모델 파일 경로를 입력하세요.")
                self._json(200, self.app.load_model(path_value))
                return
            if path == "/api/gap/select":
                profile_id = payload.get("connection_id")
                if not isinstance(profile_id, str) or not profile_id.strip():
                    raise ValueError("갭 분석할 데이터베이스를 하나 선택하세요.")
                self.app.select_gap_connection(profile_id.strip())
                self._json(200, {"ok": True, **self.app._gap_public()})
                return
            if path == "/api/gap/analyze":
                self._json(200, self.app.analyze_gap(payload))
                return
            if path == "/api/gap/settings":
                if payload.get("reset"):
                    self._json(200, self.app.reset_gap_filter())
                else:
                    self._json(200, self.app.save_gap_filter(payload))
                return
            if path == "/api/gap/apply-db":
                result = self.app.apply_gap_db(payload)
                status = 409 if result.get("need_confirm") else 200
                self._json(status, result)
                return
            if path == "/api/gap/apply-model":
                self._json(200, self.app.apply_gap_model(payload))
                return
            if path == "/api/sites":
                site = self.app.save_site(payload)
                self._json(200, {"ok": True, **site.public()})
                return
            if path == "/api/sites/reorder":
                items = self.app.reorder_sites(payload)
                self._json(200, {"ok": True, "sites": [item.public() for item in items]})
                return
            if path == "/api/site-groups/reorder":
                groups = self.app.reorder_site_groups(payload)
                self._json(200, {"ok": True, "site_groups": [item.public() for item in groups]})
                return
            if path == "/api/site-groups":
                group = self.app.save_site_group(payload)
                self._json(200, {"ok": True, **group})
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
        group_prefix = "/api/site-groups/"
        if path.startswith(group_prefix) and path != group_prefix:
            try:
                self.app.delete_site_group(unquote(path[len(group_prefix) :]))
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, {"ok": True})
            return
        site_prefix = "/api/sites/"
        if path.startswith(site_prefix) and path != site_prefix:
            try:
                self.app.delete_site(unquote(path[len(site_prefix) :]))
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
    app.mail.start()
    print(f"오라클 변경 관리: {url}")
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
