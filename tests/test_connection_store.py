import tempfile
import unittest
from pathlib import Path

from app.connection_store import ConnectionStore
from app.models import ConnectionProfile


class ConnectionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "connections.json"
        self.store = ConnectionStore(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_roundtrip_hides_password(self):
        profile = ConnectionProfile(
            id="dev-1",
            name="개발",
            host="db.example",
            port=1521,
            identifier_type="service_name",
            identifier="ORCL",
            username="scott",
            password="Secret비밀번호!",
            current_schema="APP",
        )
        self.store.upsert(profile)
        self.store.update_last_active(profile.id)
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("Secret비밀번호!", raw)
        self.assertIn("password_protected", raw)

        loaded = self.store.load()
        self.assertEqual(loaded.last_active_id, profile.id)
        self.assertEqual(len(loaded.connections), 1)
        saved = loaded.connections[0]
        self.assertEqual(saved.name, "개발")
        self.assertEqual(saved.password, "Secret비밀번호!")
        self.assertFalse(saved.password_error)
        self.assertEqual(saved.current_schema, "APP")
        self.assertFalse(saved.record_history)
        self.store.delete(profile.id)
        after_delete = self.store.load()
        self.assertEqual(after_delete.connections, [])
        self.assertIsNone(after_delete.last_active_id)

    def test_record_history_is_exclusive(self):
        first = ConnectionProfile(
            id="dev-1",
            name="개발",
            host="db.example",
            identifier="ORCL",
            username="scott",
            password="a",
            record_history=True,
        )
        second = ConnectionProfile(
            id="prd-1",
            name="운영",
            host="db.example",
            identifier="ORCL",
            username="scott",
            password="b",
            record_history=True,
        )
        self.store.upsert(first)
        self.store.upsert(second)
        loaded = self.store.load()
        flags = {item.name: item.record_history for item in loaded.connections}
        self.assertEqual(flags, {"개발": False, "운영": True})

    def test_corrupt_file_is_quarantined(self):
        self.path.write_text("{", encoding="utf-8")
        data = self.store.load()
        self.assertEqual(data.connections, [])
        self.assertTrue(data.warning)
        self.assertFalse(self.path.exists())
        broken = list(self.path.parent.glob("connections.broken*.json"))
        self.assertTrue(broken)

    def test_script_store_is_kept_apart_from_targets(self):
        target = ConnectionProfile(
            id="dev-1",
            name="개발",
            host="db.example",
            port=1521,
            identifier="ORCL",
            username="scott",
            password="target-secret",
        )
        archive = ConnectionProfile(
            id="archive-1",
            name="스크립트 저장",
            host="repo.example",
            port=1521,
            identifier="REPO",
            username="archive",
            password="archive-secret",
        )
        self.store.upsert(target)
        self.store.save_script_store(archive)
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("archive-secret", raw)
        loaded = self.store.load()
        self.assertEqual([item.name for item in loaded.connections], ["개발"])
        self.assertIsNotNone(loaded.script_store)
        self.assertEqual(loaded.script_store.name, "스크립트 저장")
        self.assertEqual(loaded.script_store.password, "archive-secret")

        self.store.upsert(target)
        self.assertEqual(self.store.load().script_store.id, "archive-1")
        self.store.clear_script_store()
        self.assertIsNone(self.store.load().script_store)
        self.assertEqual(len(self.store.load().connections), 1)

    def test_missing_file_is_empty(self):
        data = self.store.load()
        self.assertEqual(data.connections, [])
        self.assertEqual(data.warning, "")


if __name__ == "__main__":
    unittest.main()
