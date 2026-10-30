import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from app.change_history import HistoryPaths
from app.connection_store import ConnectionStore
from app.mail_inbox import MailInbox
from app.mail_store import MailStore
from app.erwin_model import DEFAULT_ERWIN_MODEL
from app.gap_analysis import DbColumn, DbTable
from app.oracle_runner import OracleSession, SessionInfo
from app.sample_script import SAMPLE_SCRIPT
from app.web_server import AppHandler, ThreadingHTTPServer, WebApp, load_page
from tests.test_mail import FakeMailClient, _rfc822


class WebServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        store = ConnectionStore(Path(self.temporary.name) / "connections.json")
        history_dir = Path(self.temporary.name) / "history"
        history_dir.mkdir()
        self.history_paths = HistoryPaths.from_dir(history_dir)
        self.app = WebApp(store, history_paths=self.history_paths)
        handler = type("BoundHandler", (AppHandler,), {"app": self.app, "page": load_page()})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address[:2]
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.close()
        self.temporary.cleanup()

    def request(self, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as response:
                body = response.read().decode("utf-8")
                return response.status, json.loads(body) if path.startswith("/api/") else body
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            parsed = json.loads(body) if body.startswith("{") else body
            return exc.code, parsed

    def test_page_and_sample_and_inspect(self):
        status, page = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("OMC연구소", page)
        self.assertIn("오라클 변경 관리", page)
        self.assertIn("오브젝트 변경 관리", page)
        self.assertIn("모델 & DB 갭분석", page)
        self.assertIn("모델을 읽는 중", page)
        self.assertIn("테이블 추가", page)
        self.assertIn("gapDdlForFilters", page)
        self.assertIn("선택한 구분", page)
        self.assertIn("DB에 반영", page)
        self.assertIn("모델에 반영", page)
        self.assertIn("추출 제외 설정", page)
        self.assertIn("주제영역", page)
        self.assertIn("OMC-SPOP_v1.76.erwin", page)
        self.assertIn("Main Subject Area", page)
        self.assertIn("_PRE", page)
        self.assertIn("querymedic.", page)
        self.assertIn("시작+끝", page)
        self.assertIn("PK 컬럼 순서", page)
        self.assertIn("prefix_suffix", page)
        self.assertIn("/favicon.svg", page)
        status, icon = self.request("/favicon.svg")
        self.assertEqual(status, 200)
        self.assertIn("<svg", icon)
        self.assertIn("메일 정리", page)
        self.assertIn("data-view=\"change\"", page)
        self.assertIn("data-view=\"gap\"", page)
        self.assertIn("data-view=\"mail\"", page)
        self.assertIn("data-view=\"sites\"", page)
        self.assertIn("사이트 관리", page)
        self.assertIn("사이트 URL", page)
        self.assertIn("그룹 추가", page)
        self.assertIn("site-group-edit-id", page)
        self.assertIn("site-card-head", page)
        self.assertIn("site-group-block", page)
        self.assertIn("예: 개발", page)
        self.assertIn("드래그하면 순서를", page)
        self.assertIn("/api/site-groups/reorder", page)
        self.assertIn("그룹 순서를", page)
        self.assertIn("openmade.co.kr", page)
        self.assertIn("mgjeon@openmade.co.kr", page)
        self.assertIn("nmail.nanuminet.com", page)
        self.assertIn("POP3", page)
        self.assertNotIn("확인메일함", page)
        self.assertIn("읽음", page)
        self.assertIn("연결 설정", page)
        self.assertIn("동시에 반영", page)
        self.assertIn("스크립트 저장", page)
        self.assertIn("변경 사유", page)
        self.assertIn("변경이력 저장", page)
        self.assertIn("Master 변경이력 기본 DB", page)
        self.assertIn("지우기", page)
        self.assertIn("스크립트 지우기", page)
        self.assertIn('id="notice"', page)
        self.assertIn("showAlert", page)
        self.assertIn("showConfirm", page)
        status, sample = self.request("/api/sample")
        self.assertEqual(status, 200)
        self.assertIn("CREATE TABLE", sample["script"])
        status, inspected = self.request("/api/script/inspect", "POST", {"script": SAMPLE_SCRIPT})
        self.assertEqual(status, 200)
        labels = [item["label"] for item in inspected["statements"]]
        self.assertEqual(labels[0], "테이블 생성")
        self.assertIn("함수", labels)
        self.assertIn("패키지 바디", labels)
        self.assertEqual(inspected["risks"], [])

    def test_connection_is_saved_without_returning_password(self):
        status, created = self.request(
            "/api/connections",
            "POST",
            {
                "name": "개발",
                "host": "127.0.0.1",
                "port": 1521,
                "identifier_type": "service_name",
                "identifier": "ORCL",
                "username": "scott",
                "password": "Secret비밀번호!",
            },
        )
        self.assertEqual(status, 200)
        status, state = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["connections"][0]["name"], "개발")
        self.assertEqual(state["active_id"], created["id"])
        self.assertNotIn("password", state["connections"][0])
        raw = (Path(self.temporary.name) / "connections.json").read_text(encoding="utf-8")
        self.assertNotIn("Secret비밀번호!", raw)

        status, missing = self.request("/api/connections", "POST", {"name": "다른"})
        self.assertEqual(status, 400)
        self.assertIn("호스트", missing["error"])

        status, _deleted = self.request("/api/connections/" + created["id"], "DELETE")
        self.assertEqual(status, 200)
        _status, state = self.request("/api/state")
        self.assertEqual(state["connections"], [])

    def test_script_store_is_not_an_execution_target(self):
        status, created = self.request(
            "/api/script-store",
            "POST",
            {
                "name": "스크립트 저장",
                "host": "127.0.0.1",
                "port": 1521,
                "identifier_type": "service_name",
                "identifier": "REPO",
                "username": "archive",
                "password": "archive-secret",
            },
        )
        self.assertEqual(status, 200)
        _status, state = self.request("/api/state")
        self.assertEqual(state["connections"], [])
        self.assertEqual(state["script_store"]["name"], "스크립트 저장")
        self.assertNotIn("password", state["script_store"])
        raw = (Path(self.temporary.name) / "connections.json").read_text(encoding="utf-8")
        self.assertNotIn("archive-secret", raw)

        status, payload = self.request(
            "/api/script/execute",
            "POST",
            {"script": "SELECT 1 FROM DUAL;", "confirm_risks": True, "connection_ids": [created["id"]]},
        )
        self.assertEqual(status, 400)
        self.assertIn("스크립트 저장", payload["error"])

        status, _deleted = self.request("/api/script-store", "DELETE")
        self.assertEqual(status, 200)
        _status, state = self.request("/api/state")
        self.assertIsNone(state["script_store"])

    def test_execute_requires_a_connection(self):
        status, payload = self.request(
            "/api/script/execute",
            "POST",
            {"script": "SELECT 1 FROM DUAL;", "confirm_risks": True},
        )
        self.assertEqual(status, 400)
        self.assertIn("연결", payload["error"])

    def test_select_requires_a_live_connection(self):
        status, created = self._save("개발")
        self.assertEqual(status, 200)
        status, payload = self.request("/api/connections/select", "POST", {"ids": [created["id"]]})
        self.assertEqual(status, 400)
        self.assertIn("연결", payload["error"])

    def test_execute_runs_only_selected_connections_together(self):
        with patch("app.oracle_runner.open_connection", side_effect=OSError("no database")):
            created = []
            for name in ("개발", "운영", "제외"):
                status, payload = self._save(name)
                self.assertEqual(status, 200)
                created.append(payload["id"])
            self._wait_until_idle()

        selected = created[:2]
        with self.app._state_lock:
            for profile_id in created:
                self.app.connect_gen[profile_id] = self.app.connect_gen.get(profile_id, 0) + 1
                self.app.pending.discard(profile_id)
                session = OracleSession(self.app._profile(profile_id))
                session.conn = object()
                session.info = SessionInfo("scott", "SCOTT", "ORCL", "ORCL", "localhost")
                self.app.sessions[profile_id] = session

        status, _selected = self.request("/api/connections/select", "POST", {"ids": selected})
        self.assertEqual(status, 200)
        _status, state = self.request("/api/state")
        self.assertEqual([item["id"] for item in state["connections"] if item["selected"]], selected)

        started = []
        entered = threading.Event()
        lock = threading.Lock()

        def fake_run(session, _statements, **kwargs):
            with lock:
                started.append(session.profile.name)
                if len(started) >= 2:
                    entered.set()
            if not entered.wait(1.5):
                kwargs["report"]("log", "err", "not concurrent")
                kwargs["report"]("finished", {"ok": 0, "fail": 1, "skip": 0, "cancelled": False, "elapsed_ms": 1})
                return
            kwargs["report"]("log", "ok", "done")
            kwargs["report"]("finished", {"ok": 1, "fail": 0, "skip": 0, "cancelled": False, "elapsed_ms": 5})

        with patch("app.web_server.run_script", side_effect=fake_run):
            status, payload = self.request(
                "/api/script/execute",
                "POST",
                {
                    "script": "SELECT 1 FROM DUAL;",
                    "reason": "조회 점검",
                    "confirm_risks": True,
                    "connection_ids": selected,
                },
            )
        self.assertEqual(status, 200, payload)
        self.assertEqual(sorted(started), ["개발", "운영"])
        self.assertEqual(payload["stats"]["ok"], 2)
        self.assertEqual(payload["stats"]["targets"], 2)
        self.assertEqual(payload["reason"], "조회 점검")
        messages = [line["message"] for line in payload["logs"]]
        self.assertEqual(messages[0], "변경 사유: 조회 점검")
        self.assertTrue(any(message.startswith("■ 개발") for message in messages))
        self.assertFalse(any("제외" in message for message in messages))
        self.assertFalse(self.history_paths.objects.exists())
        self.assertFalse(self.history_paths.data.exists())
        self.assertFalse(self.history_paths.procedures.exists())

    def test_execute_writes_change_history_by_statement_kind(self):
        created = self._connected("개발", record_history=True)
        script = (
            "ALTER TABLE AI_WORK ADD NOTE VARCHAR2(100);\n"
            "INSERT INTO CD(GRP_CD_ID, CD) VALUES('1', '1');\n"
            "CREATE OR REPLACE FUNCTION demo_fn RETURN NUMBER IS BEGIN RETURN 1; END;\n/\n"
        )

        def fake_run(_session, _statements, **kwargs):
            kwargs["report"]("log", "ok", "done")
            kwargs["report"]("finished", {"ok": 3, "fail": 0, "skip": 0, "cancelled": False, "elapsed_ms": 5})

        with patch("app.web_server.run_script", side_effect=fake_run):
            status, payload = self.request(
                "/api/script/execute",
                "POST",
                {
                    "script": script,
                    "reason": "기능 점검",
                    "confirm_risks": True,
                    "save_history": True,
                    "connection_ids": [created],
                },
            )
        self.assertEqual(status, 200, payload)
        objects = self.history_paths.objects.read_text(encoding="utf-8")
        data = self.history_paths.data.read_text(encoding="utf-8")
        procedures = self.history_paths.procedures.read_text(encoding="utf-8")
        self.assertIn("ALTER TABLE AI_WORK ADD NOTE VARCHAR2(100);", objects)
        self.assertIn("INSERT INTO CD(GRP_CD_ID, CD) VALUES('1', '1');", data)
        self.assertIn("DEMO_FN", procedures)
        self.assertNotIn("CREATE OR REPLACE", procedures)
        self.assertIn("/* 기능 점검 */", objects)
        self.assertIn("오픈팝 데모 반영 (전만기)", objects)
        self.assertIn("오픈팝 배포 반영 (전만기)", data)
        messages = [line["message"] for line in payload["logs"]]
        self.assertTrue(any("Master Object 변경이력.sql에 기록했습니다." in message for message in messages))

    def test_execute_skips_history_unless_checked(self):
        created = self._connected("개발")

        def fake_run(_session, _statements, **kwargs):
            kwargs["report"]("log", "ok", "done")
            kwargs["report"]("finished", {"ok": 1, "fail": 0, "skip": 0, "cancelled": False, "elapsed_ms": 5})

        with patch("app.web_server.run_script", side_effect=fake_run):
            status, payload = self.request(
                "/api/script/execute",
                "POST",
                {
                    "script": "ALTER TABLE AI_WORK ADD NOTE VARCHAR2(100);",
                    "reason": "저장 안 함",
                    "confirm_risks": True,
                    "connection_ids": [created],
                },
            )
        self.assertEqual(status, 200, payload)
        self.assertFalse(self.history_paths.objects.exists())
        self.assertFalse(any("변경이력" in line["message"] for line in payload["logs"]))

    def test_execute_history_requires_designated_db(self):
        other = self._connected("개발")
        master = self._connected("운영", record_history=True)
        script = "ALTER TABLE AI_WORK ADD NOTE VARCHAR2(100);"

        def fake_run(_session, _statements, **kwargs):
            kwargs["report"]("log", "ok", "done")
            kwargs["report"]("finished", {"ok": 1, "fail": 0, "skip": 0, "cancelled": False, "elapsed_ms": 5})

        with patch("app.web_server.run_script", side_effect=fake_run):
            status, skipped = self.request(
                "/api/script/execute",
                "POST",
                {
                    "script": script,
                    "reason": "개발만",
                    "confirm_risks": True,
                    "save_history": True,
                    "connection_ids": [other],
                },
            )
        self.assertEqual(status, 200, skipped)
        self.assertFalse(self.history_paths.objects.exists())
        self.assertTrue(any("기본 DB가 실행 대상이 아니라" in line["message"] for line in skipped["logs"]))
        with patch("app.web_server.run_script", side_effect=fake_run):
            status, written = self.request(
                "/api/script/execute",
                "POST",
                {
                    "script": script,
                    "reason": "운영 반영",
                    "confirm_risks": True,
                    "save_history": True,
                    "connection_ids": [other, master],
                },
            )
        self.assertEqual(status, 200, written)
        objects = self.history_paths.objects.read_text(encoding="utf-8")
        self.assertIn("ALTER TABLE AI_WORK ADD NOTE VARCHAR2(100);", objects)
        self.assertTrue(any("Master Object 변경이력.sql에 기록했습니다." in line["message"] for line in written["logs"]))

    def test_gap_analyze_one_connection_from_json_model(self):
        created = self._connected("개발")
        other = self._connected("운영")
        model_path = Path(self.temporary.name) / "model.json"
        model_path.write_text(
            json.dumps(
                {
                    "name": "SQL",
                    "tables": [
                        {
                            "logical_name": "작업",
                            "physical_name": "AI_WORK",
                            "columns": [
                                {
                                    "logical_name": "작업ID",
                                    "physical_name": "AI_WORK_ID",
                                    "datatype": "NUMBER",
                                    "null_option": "NotNull",
                                    "is_pk": True,
                                },
                                {
                                    "logical_name": "비고",
                                    "physical_name": "NOTE",
                                    "datatype": "VARCHAR2(100)",
                                    "null_option": "Null",
                                    "is_pk": False,
                                },
                            ],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        status, loaded = self.request("/api/gap/load", "POST", {"path": str(model_path)})
        self.assertEqual(status, 200, loaded)
        self.assertEqual(loaded["table_count"], 1)
        self.assertTrue(loaded["loaded"])

        status, selected = self.request("/api/gap/select", "POST", {"connection_id": created})
        self.assertEqual(status, 200, selected)
        self.assertEqual(selected["connection_id"], created)

        db_tables = [
            DbTable(
                name="AI_WORK",
                columns=[DbColumn("AI_WORK_ID", "NUMBER", False)],
                pk_columns=["AI_WORK_ID"],
            )
        ]
        with patch("app.web_server.fetch_db_tables", return_value=("SCOTT", db_tables)):
            status, report = self.request("/api/gap/analyze", "POST", {"connection_id": created})
        self.assertEqual(status, 200, report)
        self.assertEqual(report["connection_id"], created)
        self.assertNotEqual(report["connection_id"], other)
        self.assertIn("ALTER TABLE AI_WORK ADD NOTE VARCHAR2(100);", report["ddl"])
        kinds = {item["kind"] for item in report["items"]}
        self.assertIn("missing_column", kinds)

        _status, state = self.request("/api/state")
        self.assertEqual(state["gap"]["connection_id"], created)
        self.assertEqual(state["gap"]["table_count"], 1)

        missing = next(item for item in report["items"] if item["kind"] == "missing_column")
        with patch("app.web_server.fetch_db_tables", return_value=("SCOTT", db_tables)):
            status, applied = self.request(
                "/api/gap/apply-model",
                "POST",
                {"index": missing["index"], "kind": missing["kind"], "table": missing["table"], "column": missing["column"]},
            )
        self.assertEqual(status, 200, applied)
        kinds_after = {item["kind"] for item in applied["report"]["items"]}
        self.assertNotIn("missing_column", kinds_after)
        saved = json.loads(model_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["tables"][0]["columns"][0]["physical_name"], "AI_WORK_ID")
        self.assertEqual(len(saved["tables"][0]["columns"]), 1)

    def test_gap_analyze_requires_model_and_one_db(self):
        status, payload = self.request("/api/gap/analyze", "POST", {})
        self.assertEqual(status, 400)
        self.assertIn("모델", payload["error"])

    def test_gap_default_model_path(self):
        self.assertEqual(self.app.gap_path, str(DEFAULT_ERWIN_MODEL))
        self.assertTrue(self.app.gap_path.endswith("OMC-SPOP_v1.76.erwin"))
        saved = Path(self.temporary.name) / "picked.erwin"
        saved.write_text("x", encoding="utf-8")
        self.app._save_gap_path(str(saved))
        self.app._restore_gap_state()
        self.assertEqual(self.app.gap_path, str(DEFAULT_ERWIN_MODEL))
        self.assertEqual(self.app.gap_filter.subject_areas, ["Main Subject Area"])
        self.assertEqual(
            [(item.match, item.value) for item in self.app.gap_filter.table_patterns],
            [("prefix", "_"), ("suffix", "_PRE"), ("prefix", "querymedic.")],
        )
        self.assertTrue(self.app.gap_filter.exclude_pk_order)
        status, saved_filter = self.request(
            "/api/gap/settings",
            "POST",
            {
                "subject_areas": ["Main Subject Area", "09_오픈팝AI(OAI)"],
                "table_patterns": [{"match": "suffix", "value": "_BAK"}],
            },
        )
        self.assertEqual(status, 200, saved_filter)
        self.assertIn("09_오픈팝AI(OAI)", saved_filter["filter"]["subject_areas"])
        self.assertEqual(saved_filter["filter"]["table_patterns"][0]["value"], "_BAK")
        status, both = self.request(
            "/api/gap/settings",
            "POST",
            {
                "subject_areas": ["Main Subject Area"],
                "table_patterns": [{"match": "prefix_suffix", "value": "VSQL", "end": "PRE"}],
            },
        )
        self.assertEqual(status, 200, both)
        self.assertEqual(both["filter"]["table_patterns"][0]["match"], "prefix_suffix")
        self.assertEqual(both["filter"]["table_patterns"][0]["value"], "VSQL")
        self.assertEqual(both["filter"]["table_patterns"][0]["end"], "PRE")
        status, pk_order = self.request(
            "/api/gap/settings",
            "POST",
            {
                "subject_areas": ["Main Subject Area"],
                "table_patterns": [{"match": "suffix", "value": "_PRE"}],
                "exclude_pk_order": False,
            },
        )
        self.assertEqual(status, 200, pk_order)
        self.assertFalse(pk_order["filter"]["exclude_pk_order"])
        status, reset = self.request("/api/gap/settings", "POST", {"reset": True})
        self.assertEqual(status, 200, reset)
        self.assertEqual(reset["filter"]["subject_areas"], ["Main Subject Area"])
        self.assertTrue(any(item["value"] == "querymedic." for item in reset["filter"]["table_patterns"]))
        self.assertTrue(reset["filter"]["exclude_pk_order"])

    def _connected(self, name, record_history=False):
        with patch("app.oracle_runner.open_connection", side_effect=OSError("no database")):
            status, payload = self._save(name, record_history=record_history)
            self.assertEqual(status, 200)
            self._wait_until_idle()
        profile_id = payload["id"]
        with self.app._state_lock:
            self.app.connect_gen[profile_id] = self.app.connect_gen.get(profile_id, 0) + 1
            self.app.pending.discard(profile_id)
            session = OracleSession(self.app._profile(profile_id))
            session.conn = object()
            session.info = SessionInfo("scott", "SCOTT", "ORCL", "ORCL", "localhost")
            self.app.sessions[profile_id] = session
        return profile_id

    def _save(self, name, record_history=False):
        return self.request(
            "/api/connections",
            "POST",
            {
                "name": name,
                "host": "127.0.0.1",
                "port": 1521,
                "identifier_type": "service_name",
                "identifier": "ORCL",
                "username": "scott",
                "password": "secret",
                "record_history": record_history,
            },
        )

    def _wait_until_idle(self):
        deadline = time.time() + 3
        while time.time() < deadline:
            _status, state = self.request("/api/state")
            if state["connections"] and all(item["status"] != "connecting" for item in state["connections"]):
                return
            time.sleep(0.05)
        self.fail("연결 시도가 끝나지 않았습니다.")

    def test_sites_are_saved_and_opened_from_state(self):
        _status, state = self.request("/api/state")
        self.assertEqual([item["name"] for item in state["site_groups"]], ["개발", "테스트", "운영"])
        development = next(item for item in state["site_groups"] if item["name"] == "개발")

        status, created = self.request(
            "/api/sites",
            "POST",
            {"name": "OpenPOP", "url": "openpop.local/app", "group_id": development["id"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(created["name"], "OpenPOP")
        self.assertEqual(created["url"], "https://openpop.local/app")
        self.assertEqual(created["group_id"], development["id"])
        _status, state = self.request("/api/state")
        self.assertEqual(state["sites"][0]["name"], "OpenPOP")
        self.assertEqual(state["sites"][0]["url"], "https://openpop.local/app")
        self.assertEqual(state["sites"][0]["group_id"], development["id"])

        status, extra = self.request("/api/site-groups", "POST", {"name": "스테이징"})
        self.assertEqual(status, 200)
        self.assertEqual(extra["name"], "스테이징")
        status, renamed = self.request(
            "/api/site-groups",
            "POST",
            {"id": extra["id"], "name": "검증"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(renamed["id"], extra["id"])
        self.assertEqual(renamed["name"], "검증")
        extra = renamed
        status, duplicate = self.request("/api/site-groups", "POST", {"name": "개발"})
        self.assertEqual(status, 400)

        status, updated = self.request(
            "/api/sites",
            "POST",
            {"id": created["id"], "name": "OpenPOP 운영", "url": "https://openpop.local", "group_id": extra["id"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["id"], created["id"])
        self.assertEqual(updated["name"], "OpenPOP 운영")
        self.assertEqual(updated["group_id"], extra["id"])

        status, rejected = self.request("/api/sites", "POST", {"name": "악성", "url": "javascript:alert(1)"})
        self.assertEqual(status, 400)
        self.assertIn("http", rejected["error"])

        status, _group_deleted = self.request("/api/site-groups/" + extra["id"], "DELETE")
        self.assertEqual(status, 200)
        _status, state = self.request("/api/state")
        self.assertEqual(state["sites"][0]["group_id"], "")
        self.assertNotIn("검증", [item["name"] for item in state["site_groups"]])

        status, other = self.request(
            "/api/sites",
            "POST",
            {"name": "QueryMedic", "url": "https://querymedic.example"},
        )
        self.assertEqual(status, 200)
        status, reordered = self.request(
            "/api/sites/reorder",
            "POST",
            {"ids": [other["id"], created["id"]], "moved_id": other["id"], "group_id": development["id"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["name"] for item in reordered["sites"]], ["QueryMedic", "OpenPOP 운영"])
        self.assertEqual(reordered["sites"][0]["group_id"], development["id"])

        group_ids = [item["id"] for item in state["site_groups"]]
        status, groups_reordered = self.request(
            "/api/site-groups/reorder",
            "POST",
            {"ids": list(reversed(group_ids))},
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [item["id"] for item in groups_reordered["site_groups"]],
            list(reversed(group_ids)),
        )
        status, bad_groups = self.request("/api/site-groups/reorder", "POST", {"ids": [group_ids[0]]})
        self.assertEqual(status, 400)

        status, _deleted = self.request("/api/sites/" + created["id"], "DELETE")
        self.assertEqual(status, 200)
        status, _other_deleted = self.request("/api/sites/" + other["id"], "DELETE")
        self.assertEqual(status, 200)
        _status, state = self.request("/api/state")
        self.assertEqual(state["sites"], [])


class MailApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        store = ConnectionStore(Path(self.temporary.name) / "connections.json")
        history_dir = Path(self.temporary.name) / "history"
        history_dir.mkdir()
        self.client = FakeMailClient(
            [_rfc822("홍길동", "hong@openmade.co.kr", "주간 배포", "배포를 내일로 조정합니다.", "21")]
        )
        mail = MailInbox(
            MailStore(Path(self.temporary.name) / "mail.json"),
            self.client,
            notify=lambda *_args: None,
        )
        self.app = WebApp(store, history_paths=HistoryPaths.from_dir(history_dir), mail_inbox=mail)
        handler = type("BoundHandler", (AppHandler,), {"app": self.app, "page": load_page()})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.httpd.server_address[:2]
        self.base = f"http://{host}:{port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.close()
        self.temporary.cleanup()

    def request(self, path, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body)

    def test_mail_settings_and_unread_summary(self):
        status, state = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["mail"]["unread"], 0)
        self.assertEqual(state["mail"]["email"], "mgjeon@openmade.co.kr")

        status, saved = self.request(
            "/api/mail/settings",
            "POST",
            {
                "email": "mgjeon@openmade.co.kr",
                "password": "ok-pass",
                "pop_host": "nmail.nanuminet.com",
                "pop_port": 995,
                "filter_domain": "openmade.co.kr",
                "poll_seconds": 30,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["unread"], 1)
        self.assertIn("홍길동", saved["digest"])
        self.assertTrue(saved["has_password"])
        raw = (Path(self.temporary.name) / "mail.json").read_text(encoding="utf-8")
        self.assertNotIn("ok-pass", raw)

        status, inbox = self.request("/api/mail")
        self.assertEqual(status, 200)
        self.assertEqual(inbox["messages"][0]["subject"], "주간 배포")
        self.assertEqual(inbox["groups"][0]["count"], 1)

        status, tested = self.request("/api/mail/test", "POST", {})
        self.assertEqual(status, 200)
        self.assertIn("mgjeon@openmade.co.kr", tested["message"])
        self.assertEqual(inbox["pop_host"], "nmail.nanuminet.com")
        self.assertEqual(inbox["pop_port"], 995)
        self.assertTrue(inbox["messages"][0]["body"])

        status, read = self.request("/api/mail/read", "POST", {"uid": inbox["messages"][0]["uid"]})
        self.assertEqual(status, 200)
        self.assertEqual(read["unread"], 0)
        self.assertEqual(read["messages"], [])


if __name__ == "__main__":
    unittest.main()
