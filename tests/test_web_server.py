import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from app.connection_store import ConnectionStore
from app.oracle_runner import OracleSession, SessionInfo
from app.sample_script import SAMPLE_SCRIPT
from app.web_server import AppHandler, ThreadingHTTPServer, WebApp, load_page


class WebServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        store = ConnectionStore(Path(self.temporary.name) / "connections.json")
        self.app = WebApp(store)
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
        self.assertIn("오라클 오브젝트 변경 관리", page)
        self.assertIn("동시에 반영", page)
        self.assertIn("스크립트 저장", page)
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
                    "confirm_risks": True,
                    "connection_ids": selected,
                },
            )
        self.assertEqual(status, 200, payload)
        self.assertEqual(sorted(started), ["개발", "운영"])
        self.assertEqual(payload["stats"]["ok"], 2)
        self.assertEqual(payload["stats"]["targets"], 2)
        messages = [line["message"] for line in payload["logs"]]
        self.assertTrue(any(message.startswith("■ 개발") for message in messages))
        self.assertFalse(any("제외" in message for message in messages))

    def _save(self, name):
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


if __name__ == "__main__":
    unittest.main()
