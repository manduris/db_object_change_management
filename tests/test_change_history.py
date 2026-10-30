import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app.change_history import (
    HistoryPaths,
    append_change_history,
    format_history_block,
    history_commit_message,
    history_kind,
    named_ddl,
)
from app.svn_client import SvnResult
from app.sample_script import SAMPLE_SCRIPT
from app.script_splitter import split_oracle_script


class ChangeHistoryTests(unittest.TestCase):
    def test_classifies_object_data_and_procedure(self):
        statements = [item for item in split_oracle_script(SAMPLE_SCRIPT) if item.mode != "skip"]
        kinds = [history_kind(item.sql) for item in statements]
        self.assertEqual(
            kinds,
            [
                "object",
                "object",
                "data",
                "procedure",
                "procedure",
                "procedure",
                "procedure",
                "data",
                None,
            ],
        )
        self.assertEqual(named_ddl(statements[3].sql), ("FUNCTION", "DEMO_FN"))
        self.assertEqual(named_ddl(statements[4].sql), ("PROCEDURE", "DEMO_PROC"))
        self.assertEqual(named_ddl(statements[6].sql), ("PACKAGE BODY", "DEMO_PKG"))

    def test_formats_object_block_with_reason_and_openpop_markers(self):
        statements = split_oracle_script(
            "ALTER TABLE AI_WORK ADD NOTE VARCHAR2(100);\n"
            "CREATE INDEX IX_AI_WORK_NOTE ON AI_WORK(NOTE);"
        )
        block = format_history_block("object", statements, "인덱스 추가", date(2026, 10, 28))
        self.assertTrue(block.startswith("-- 2026-10-28\n"))
        self.assertIn("/* 인덱스 추가 */", block)
        self.assertIn("ALTER TABLE AI_WORK ADD NOTE VARCHAR2(100);", block)
        self.assertIn("CREATE INDEX IX_AI_WORK_NOTE ON AI_WORK(NOTE);", block)
        self.assertIn(
            "------------------------------------------------  2026-10-28 오픈팝 데모 반영 (전만기) ---------------------------------------------------------",
            block,
        )
        self.assertIn(
            "------------------------------------------------  2026-10-28 오픈팝 배포 반영 (전만기) ---------------------------------------------------------",
            block,
        )

    def test_procedure_history_writes_object_names_only(self):
        statements = split_oracle_script(SAMPLE_SCRIPT)
        procedures = [item for item in statements if history_kind(item.sql) == "procedure"]
        block = format_history_block("procedure", procedures, "함수와 패키지 수정", date(2026, 10, 28))
        self.assertIn("/* 함수와 패키지 수정 */", block)
        self.assertIn("DEMO_FN", block)
        self.assertIn("DEMO_PROC", block)
        self.assertIn("DEMO_PKG", block)
        self.assertEqual(block.count("DEMO_PKG"), 1)
        self.assertNotIn("CREATE OR REPLACE", block)

    def test_appends_to_matching_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = HistoryPaths.from_dir(Path(tmp))
            statements = split_oracle_script(SAMPLE_SCRIPT)
            written = append_change_history(
                statements,
                "샘플 변경",
                paths=paths,
                when=date(2026, 10, 28),
            )
            self.assertEqual([item.kind for item in written.records], ["object", "data", "procedure"])
            objects = paths.objects.read_text(encoding="utf-8")
            data = paths.data.read_text(encoding="utf-8")
            procedures = paths.procedures.read_text(encoding="utf-8")
            self.assertIn("CREATE TABLE demo_change_log", objects)
            self.assertIn("INSERT INTO demo_change_log", data)
            self.assertIn("DEMO_FN", procedures)
            self.assertNotIn("BEGIN", procedures)
            self.assertTrue(all("샘플 변경" in item.text for item in written.records))

    def test_appends_cp949_history_without_rewriting_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Master Object 변경이력.sql"
            existing = (
                "------------------------------------------------  2026-10-27 오픈팝 배포 반영 (전만기) "
                "---------------------------------------------------------\r\n"
            )
            path.write_bytes(existing.encode("cp949"))
            paths = HistoryPaths(
                objects=path,
                data=Path(tmp) / "Master Data 변경이력.sql",
                procedures=Path(tmp) / "Master Procedure 변경이력.sql",
            )
            statements = split_oracle_script("ALTER TABLE AI_WORK ADD NOTE VARCHAR2(100);")
            append_change_history(statements, "컬럼 추가", paths=paths, when=date(2026, 10, 28))
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(existing.encode("cp949")))
            text = raw.decode("cp949")
            self.assertIn("\r\n-- 2026-10-28\r\n", text)
            self.assertIn("/* 컬럼 추가 */", text)
            self.assertIn("오픈팝 데모 반영 (전만기)", text)

    def test_skips_comment_on_table_and_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = HistoryPaths.from_dir(Path(tmp))
            statements = split_oracle_script(
                "ALTER TABLE AI_WORK ADD NOTE VARCHAR2(100);\n"
                "COMMENT ON TABLE AI_WORK IS '작업';\n"
                "COMMENT ON COLUMN AI_WORK.NOTE IS '비고';"
            )
            written = append_change_history(statements, "컬럼 추가", paths=paths, when=date(2026, 10, 29))
            self.assertEqual([item.kind for item in written.records], ["object"])
            objects = paths.objects.read_text(encoding="utf-8")
            self.assertIn("ALTER TABLE AI_WORK ADD NOTE VARCHAR2(100);", objects)
            self.assertNotIn("COMMENT ON TABLE", objects)
            self.assertNotIn("COMMENT ON COLUMN", objects)

    def test_same_date_writes_date_header_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = HistoryPaths.from_dir(Path(tmp))
            first = split_oracle_script("ALTER TABLE AI_WORK ADD NOTE VARCHAR2(100);")
            second = split_oracle_script("ALTER TABLE AI_WORK MODIFY NOTE VARCHAR2(200);")
            append_change_history(first, "컬럼 추가", paths=paths, when=date(2026, 10, 29))
            append_change_history(second, "길이 변경", paths=paths, when=date(2026, 10, 29))
            objects = paths.objects.read_text(encoding="utf-8")
            self.assertEqual(objects.count("-- 2026-10-29"), 1)
            self.assertEqual(objects.count("오픈팝 데모 반영"), 1)
            self.assertEqual(objects.count("오픈팝 배포 반영"), 1)
            self.assertIn("ADD NOTE VARCHAR2(100);", objects)
            self.assertIn("MODIFY NOTE VARCHAR2(200);", objects)
            self.assertTrue(objects.index("ADD NOTE") < objects.index("MODIFY NOTE"))
            self.assertTrue(objects.index("MODIFY NOTE") < objects.index("오픈팝 데모 반영"))

    def test_history_commit_message_uses_author_and_reason(self):
        self.assertEqual(history_commit_message("인덱스 추가"), "(전만기)\n인덱스 추가")
        self.assertEqual(history_commit_message("  "), "(전만기)")

    def test_svn_update_runs_before_write_and_commit_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".svn").mkdir()
            paths = HistoryPaths.from_dir(root)
            statements = split_oracle_script("ALTER TABLE AI_WORK ADD NOTE VARCHAR2(100);")
            with (
                patch(
                    "app.change_history.svn_update",
                    return_value=SvnResult(ok=True, action="업데이트", output="Updating '.':\nAt revision 18421."),
                ) as update,
                patch(
                    "app.change_history.svn_commit",
                    return_value=SvnResult(
                        ok=True,
                        action="커밋",
                        output="Sending        Master Object 변경이력.sql\nCommitted revision 18422.",
                    ),
                ) as commit,
            ):
                written = append_change_history(statements, "컬럼 추가", paths=paths, when=date(2026, 10, 29))
            update.assert_called_once_with(root)
            commit.assert_called_once_with(root, "(전만기)\n컬럼 추가")
            self.assertTrue(paths.objects.exists())
            self.assertIn("SVN 업데이트 성공", written.messages)
            self.assertIn("At revision 18421.", written.messages)
            self.assertIn("SVN 커밋 성공", written.messages)
            self.assertIn("Committed revision 18422.", written.messages)

    def test_svn_update_failure_skips_history_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".svn").mkdir()
            paths = HistoryPaths.from_dir(root)
            statements = split_oracle_script("ALTER TABLE AI_WORK ADD NOTE VARCHAR2(100);")
            with (
                patch(
                    "app.change_history.svn_update",
                    return_value=SvnResult(ok=False, action="업데이트", error="conflict", output="Conflicted"),
                ),
                patch("app.change_history.svn_commit") as commit,
            ):
                written = append_change_history(statements, "컬럼 추가", paths=paths, when=date(2026, 10, 29))
            commit.assert_not_called()
            self.assertFalse(paths.objects.exists())
            self.assertTrue(any("업데이트에 실패" in item for item in written.errors))


if __name__ == "__main__":
    unittest.main()
