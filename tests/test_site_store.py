import json
import tempfile
import unittest
from pathlib import Path

from app.site_store import SiteStore, normalize_site_url


class SiteStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "sites.json"
        self.store = SiteStore(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_normalize_adds_https_and_rejects_other_schemes(self):
        self.assertEqual(normalize_site_url("www.openmade.co.kr"), "https://www.openmade.co.kr")
        self.assertEqual(
            normalize_site_url("http://intranet.local/path?x=1"),
            "http://intranet.local/path?x=1",
        )
        with self.assertRaises(ValueError):
            normalize_site_url("javascript:alert(1)")
        with self.assertRaises(ValueError):
            normalize_site_url("   ")

    def test_roundtrip_update_and_delete(self):
        created = self.store.upsert("OpenPOP", "openpop.local")
        self.assertEqual(created.url, "https://openpop.local")
        self.assertEqual(self.store.list()[0].name, "OpenPOP")

        updated = self.store.upsert("OpenPOP 운영", "https://openpop.local/app", site_id=created.id)
        self.assertEqual(updated.id, created.id)
        self.assertEqual(self.store.list()[0].name, "OpenPOP 운영")
        self.assertEqual(self.store.list()[0].url, "https://openpop.local/app")

        self.store.delete(created.id)
        self.assertEqual(self.store.list(), [])
        with self.assertRaises(ValueError):
            self.store.delete(created.id)

    def test_default_groups_and_assignment(self):
        names = [item.name for item in self.store.groups()]
        self.assertEqual(names, ["개발", "테스트", "운영"])
        development = next(item for item in self.store.groups() if item.name == "개발")
        created = self.store.upsert("OpenPOP", "openpop.local", group_id=development.id)
        self.assertEqual(created.group_id, development.id)
        extra = self.store.add_group("스테이징")
        self.assertEqual(extra.name, "스테이징")
        renamed = self.store.rename_group(development.id, "개발계")
        self.assertEqual(renamed.id, development.id)
        self.assertEqual(renamed.name, "개발계")
        self.assertEqual(self.store.list()[0].group_id, development.id)
        with self.assertRaises(ValueError):
            self.store.rename_group(development.id, "테스트")
        with self.assertRaises(ValueError):
            self.store.add_group("개발계")
        self.store.delete_group(development.id)
        self.assertEqual(self.store.list()[0].group_id, "")
        self.assertNotIn("개발", [item.name for item in self.store.groups()])

    def test_legacy_file_gets_default_groups(self):
        self.path.write_text(
            json.dumps({
                "sites": [{"id": "s1", "name": "OpenPOP", "url": "https://openpop.local"}],
            }),
            encoding="utf-8",
        )
        names = [item.name for item in self.store.groups()]
        self.assertEqual(names, ["개발", "테스트", "운영"])
        self.assertEqual(self.store.list()[0].name, "OpenPOP")

    def test_reorder_changes_order_and_group(self):
        development = next(item for item in self.store.groups() if item.name == "개발")
        first = self.store.upsert("A", "a.example")
        second = self.store.upsert("B", "b.example")
        third = self.store.upsert("C", "c.example")
        self.store.reorder([third.id, first.id, second.id])
        self.assertEqual([item.name for item in self.store.list()], ["C", "A", "B"])
        self.store.reorder(
            [first.id, third.id, second.id],
            moved_id=first.id,
            group_id=development.id,
        )
        ordered = self.store.list()
        self.assertEqual([item.name for item in ordered], ["A", "C", "B"])
        self.assertEqual(ordered[0].group_id, development.id)
        with self.assertRaises(ValueError):
            self.store.reorder([first.id])

    def test_reorder_groups_changes_order(self):
        names = [item.name for item in self.store.groups()]
        self.assertEqual(names, ["개발", "테스트", "운영"])
        ids = [item.id for item in self.store.groups()]
        self.store.reorder_groups([ids[2], ids[0], ids[1]])
        self.assertEqual([item.name for item in self.store.groups()], ["운영", "개발", "테스트"])
        with self.assertRaises(ValueError):
            self.store.reorder_groups([ids[0]])
