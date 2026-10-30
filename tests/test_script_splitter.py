import unittest

from app.sample_script import SAMPLE_SCRIPT
from app.script_splitter import (
    find_risks,
    plsql_compile_target,
    split_oracle_script,
    statement_label,
    summarize_statements,
)


class ScriptSplitterTests(unittest.TestCase):
    def test_sample_script_labels(self):
        statements = [item for item in split_oracle_script(SAMPLE_SCRIPT) if item.mode != "skip"]
        labels = [statement_label(item.sql, item.mode) for item in statements]
        self.assertEqual(
            labels,
            [
                "테이블 생성",
                "인덱스 생성",
                "데이터 입력",
                "함수",
                "프로시저",
                "패키지",
                "패키지 바디",
                "데이터 수정",
                "PL/SQL",
            ],
        )
        function = next(item for item in statements if "FUNCTION demo_fn" in item.sql)
        self.assertEqual(function.mode, "plsql")
        self.assertNotIn("\n/", function.sql)
        self.assertIn("RETURN v_message;", function.sql)
        self.assertEqual(find_risks(statements), [])
        self.assertIn("함수 1", summarize_statements(statements))

    def test_semicolon_inside_string_and_quotes(self):
        script = "SELECT 'it''s;ok' FROM DUAL; SELECT q'[a;b]' FROM DUAL; SELECT \"WEIRD;NAME\" FROM DUAL;"
        parts = split_oracle_script(script)
        self.assertEqual(len(parts), 3)
        self.assertIn("it''s;ok", parts[0].sql)
        self.assertIn("a;b", parts[1].sql)
        self.assertIn('WEIRD;NAME', parts[2].sql)
        self.assertTrue(all(not item.sql.endswith(";") for item in parts))

    def test_comments_do_not_split_statements(self):
        script = """
        /*
        ;
        /
        */
        SELECT /* ; */ 1 FROM DUAL;
        -- only a comment
        """
        parts = [item for item in split_oracle_script(script) if item.mode != "skip"]
        self.assertEqual(len(parts), 1)
        self.assertIn("SELECT", parts[0].sql)
        self.assertEqual(split_oracle_script("-- nothing\n/* still nothing */"), [])

    def test_procedure_slash_keeps_following_insert_separate(self):
        script = """
        CREATE OR REPLACE PROCEDURE p IS
        BEGIN
          NULL;
        END;
        /
        INSERT INTO t VALUES (1);
        """
        parts = split_oracle_script(script.replace("\n", "\r\n"))
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0].mode, "plsql")
        self.assertIn("NULL;", parts[0].sql)
        self.assertNotIn("INSERT", parts[0].sql)
        self.assertEqual(parts[1].mode, "sql")
        self.assertTrue(parts[1].sql.upper().startswith("INSERT"))

    def test_quoted_schema_procedure_is_one_plsql_statement(self):
        script = """CREATE OR REPLACE PROCEDURE OPENPOP."SP_FOO"
(
    P_ID NUMBER
)
IS
BEGIN
  INSERT INTO T VALUES (P_ID);
  COMMIT;
END;
"""
        parts = split_oracle_script(script)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].mode, "plsql")
        self.assertEqual(
            plsql_compile_target(parts[0].sql),
            ("PROCEDURE", "SP_FOO", "OPENPOP"),
        )

    def test_editionable_procedure_and_eof_without_slash(self):
        script = """CREATE OR REPLACE EDITIONABLE PROCEDURE p IS
BEGIN
  NULL;
END;
"""
        parts = split_oracle_script(script)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].mode, "plsql")
        self.assertIn("END;", parts[0].sql)

    def test_sqlplus_and_exec(self):
        script = """
        SET SERVEROUTPUT ON
        PROMPT hello
        @other.sql
        EXEC demo_proc(1, 'x');
        SELECT 1 FROM DUAL
        /
        """
        parts = split_oracle_script(script)
        skips = [item for item in parts if item.mode == "skip"]
        self.assertEqual(len(skips), 3)
        executed = [item for item in parts if item.mode != "skip"]
        self.assertEqual(executed[0].mode, "plsql")
        self.assertIn("demo_proc(1, 'x')", executed[0].sql)
        self.assertIn("BEGIN", executed[0].sql)
        self.assertEqual(executed[1].mode, "sql")
        self.assertIn("SELECT 1 FROM DUAL", executed[1].sql)

    def test_risks(self):
        script = """
        DROP TABLE demo_change_log;
        TRUNCATE TABLE demo_change_log;
        DELETE FROM demo_change_log;
        UPDATE demo_change_log SET message = 'x';
        DELETE FROM demo_change_log WHERE id = 1;
        ALTER TABLE demo_change_log DROP COLUMN message;
        CREATE OR REPLACE PROCEDURE p IS
        BEGIN
          EXECUTE IMMEDIATE 'DROP TABLE demo_change_log';
          DELETE FROM demo_change_log;
        END;
        /
        """
        risks = find_risks(split_oracle_script(script))
        joined = "\n".join(risks)
        self.assertIn("오브젝트 삭제", joined)
        self.assertIn("테이블 비우기", joined)
        self.assertIn("WHERE 없는 DELETE", joined)
        self.assertIn("WHERE 없는 UPDATE", joined)
        self.assertIn("컬럼 또는 제약 삭제", joined)
        self.assertIn("PL/SQL 안의 삭제 또는 TRUNCATE", joined)
        self.assertNotIn("WHERE id = 1", joined)

    def test_two_statements_on_one_line(self):
        parts = split_oracle_script("INSERT INTO t VALUES (1); INSERT INTO t VALUES (2);")
        self.assertEqual(len(parts), 2)
        self.assertEqual(statement_label(parts[0].sql, parts[0].mode), "데이터 입력")


if __name__ == "__main__":
    unittest.main()
