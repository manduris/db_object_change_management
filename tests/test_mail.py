import json
import tempfile
import unittest
from email.header import Header
from pathlib import Path

from app.dpapi import protect_text
from app.mail_client import MailMessage, parse_rfc822, parse_uidl_lines, sender_domain_matches
from app.mail_inbox import MailInbox
from app.mail_store import MailSettings, MailStore
from app.mail_summary import notice_text, summarize_unread


def _rfc822(sender: str, address: str, subject: str, body: str, uid: str = "1") -> MailMessage:
    header = Header(subject, "utf-8").encode()
    raw = (
        f"From: {sender} <{address}>\r\n"
        f"To: mgjeon@openmade.co.kr\r\n"
        f"Subject: {header}\r\n"
        "Date: Wed, 28 Oct 2026 10:15:00 +0900\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "Content-Transfer-Encoding: 8bit\r\n"
        "\r\n"
        f"{body}\r\n"
    ).encode("utf-8")
    return parse_rfc822(uid, raw)


class MailClientTests(unittest.TestCase):
    def test_parse_keeps_korean_and_strips_quotes(self):
        message = _rfc822(
            sender="홍길동",
            address="hong@openmade.co.kr",
            subject="주간 배포",
            body="배포를 내일 오후 3시로 미룹니다.\n확인 부탁드립니다.\n\n-----Original Message-----\n예전 내용",
        )
        self.assertEqual(message.sender, "홍길동")
        self.assertEqual(message.sender_email, "hong@openmade.co.kr")
        self.assertEqual(message.subject, "주간 배포")
        self.assertIn("내일 오후 3시", message.body)
        self.assertNotIn("예전 내용", message.body)

    def test_domain_filter(self):
        self.assertTrue(sender_domain_matches("hong@openmade.co.kr", "홍길동 <hong@openmade.co.kr>", "openmade.co.kr"))
        self.assertTrue(sender_domain_matches("bot@mail.openmade.co.kr", "", "openmade.co.kr"))
        self.assertFalse(sender_domain_matches("other@gmail.com", "Gmail <other@gmail.com>", "openmade.co.kr"))
        self.assertFalse(sender_domain_matches("evil@openmade.co.kr.example", "", "openmade.co.kr"))

    def test_uidl_lines(self):
        items = parse_uidl_lines([b"1 abcdef", b"2 uid-22", b"+OK ignored"])
        self.assertEqual(items, [(1, "abcdef"), (2, "uid-22")])


class MailSummaryTests(unittest.TestCase):
    def test_groups_and_digest(self):
        messages = [
            _rfc822("홍길동", "hong@openmade.co.kr", "주간 배포", "배포를 내일로 조정합니다. 점검을 진행합니다.", "1"),
            _rfc822("홍길동", "hong@openmade.co.kr", "체크리스트", "점검 항목을 공유합니다.", "2"),
            _rfc822("김철수", "kim@openmade.co.kr", "회의", "오전에 짧게 회의합시다.", "3"),
        ]
        digest = summarize_unread(messages)
        self.assertEqual(digest["unread"], 3)
        self.assertEqual(digest["headline"], "읽지 않은 메일 3건")
        self.assertEqual(len(digest["groups"]), 2)
        self.assertEqual(digest["groups"][0]["sender"], "홍길동")
        self.assertEqual(digest["groups"][0]["count"], 2)
        self.assertIn("홍길동 2건", digest["digest"])
        self.assertIn("김철수 1건", digest["digest"])
        self.assertTrue(digest["messages"][0]["summary"])
        self.assertTrue(digest["messages"][0]["body"])
        self.assertIn("새 메일 3건", notice_text(messages))


class MailStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "mail.json"
        self.store = MailStore(self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_password_is_encrypted(self):
        settings = MailSettings(
            email="mgjeon@openmade.co.kr",
            password="webmail-secret",
            pop_host="nmail.nanuminet.com",
            pop_port=995,
            filter_domain="",
        )
        self.store.save(settings)
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("webmail-secret", raw)
        self.assertIn("nmail.nanuminet.com", raw)
        loaded = self.store.load()
        self.assertEqual(loaded.email, "mgjeon@openmade.co.kr")
        self.assertEqual(loaded.password, "webmail-secret")
        self.assertEqual(loaded.pop_host, "nmail.nanuminet.com")
        self.assertEqual(loaded.pop_port, 995)
        self.assertEqual(loaded.filter_domain, "")
        self.assertTrue(loaded.configured)

    def test_old_gmail_settings_are_replaced(self):
        self.path.write_text(
            json.dumps(
                {
                    "email": "manduris@openmade.info",
                    "imap_host": "imap.gmail.com",
                    "imap_port": 993,
                    "filter_domain": "openmade.co.kr",
                    "password_protected": protect_text("old-app-pass"),
                }
            ),
            encoding="utf-8",
        )
        loaded = self.store.load()
        self.assertEqual(loaded.email, "mgjeon@openmade.co.kr")
        self.assertEqual(loaded.pop_host, "nmail.nanuminet.com")
        self.assertEqual(loaded.pop_port, 995)
        self.assertEqual(loaded.filter_domain, "")
        self.assertEqual(loaded.password, "")


class FakeMailClient:
    def __init__(self, messages=None, password="ok-pass"):
        self.messages = list(messages or [])
        self.password = password
        self.ping_calls = 0
        self.fetch_calls = 0
        self.read_calls: list[str] = []

    def ping(self, settings):
        self.ping_calls += 1
        if settings.password != self.password:
            raise ValueError("로그인에 실패했습니다.")

    def fetch_unread(self, settings):
        self.fetch_calls += 1
        if settings.password != self.password:
            raise ValueError("로그인에 실패했습니다.")
        return list(self.messages)

    def mark_read(self, settings, uid):
        if settings.password != self.password:
            raise ValueError("로그인에 실패했습니다.")
        self.read_calls.append(uid)
        self.messages = [item for item in self.messages if item.uid != uid]


class MailInboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = MailStore(Path(self.temporary.name) / "mail.json")
        self.notices: list[tuple[str, str]] = []
        self.client = FakeMailClient()
        self.inbox = MailInbox(
            self.store,
            self.client,
            notify=lambda title, body: self.notices.append((title, body)),
        )

    def tearDown(self):
        self.inbox.stop()
        self.temporary.cleanup()

    def test_first_fetch_does_not_notify_then_new_mail_does(self):
        first = _rfc822("홍길동", "hong@openmade.co.kr", "주간 배포", "배포를 내일로 조정합니다.", "11")
        self.client.messages = [first]
        saved = self.inbox.save_settings(
            {
                "email": "mgjeon@openmade.co.kr",
                "password": "ok-pass",
                "pop_host": "nmail.nanuminet.com",
                "pop_port": 995,
                "filter_domain": "openmade.co.kr",
            }
        )
        self.assertEqual(saved["unread"], 1)
        self.assertEqual(self.notices, [])
        self.assertIn("홍길동", saved["digest"])

        second = _rfc822("김철수", "kim@openmade.co.kr", "회의", "오전에 회의합시다.", "12")
        self.client.messages = [first, second]
        again = self.inbox.refresh()
        self.assertEqual(again["unread"], 2)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("김철수", self.notices[0][1])
        self.assertGreater(again["notice_id"], 0)

    def test_missing_password_skips_network(self):
        data = self.inbox.refresh()
        self.assertEqual(self.client.fetch_calls, 0)
        self.assertFalse(data["configured"])
        self.assertEqual(data["unread"], 0)

    def test_mark_read_hides_mail_and_skips_notify(self):
        first = _rfc822("홍길동", "hong@openmade.co.kr", "주간 배포", "배포를 내일로 조정합니다.", "11")
        self.client.messages = [first]
        self.inbox.save_settings(
            {
                "email": "mgjeon@openmade.co.kr",
                "password": "ok-pass",
                "pop_host": "nmail.nanuminet.com",
                "pop_port": 995,
                "filter_domain": "openmade.co.kr",
            }
        )
        self.notices.clear()
        moved = self.inbox.mark_read("11")
        self.assertEqual(self.client.read_calls, ["11"])
        self.assertEqual(moved["unread"], 0)
        self.assertNotIn("confirmed", moved)
        again = self.inbox.refresh()
        self.assertEqual(again["unread"], 0)
        self.assertEqual(self.notices, [])
        self.assertEqual(again["messages"], [])
