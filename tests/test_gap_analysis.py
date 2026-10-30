import json
import tempfile
import unittest
from pathlib import Path

from app.erwin_model import (
    ErwinModel,
    ModelColumn,
    ModelTable,
    filter_model_tables,
    fix_erwin_text,
    is_main_subject_area,
    load_erwin_model,
)
from app.gap_filter import GapFilter, TablePattern, default_gap_filter, parse_gap_filter, table_name_excluded
from app.gap_analysis import (
    DbColumn,
    DbTable,
    GapItem,
    apply_item_to_model,
    build_apply_ddl,
    compare_model_to_db,
    db_apply_sql,
    normalize_datatype,
    quote_ident,
    summarize_gaps,
    types_compatible,
)


def _model(*tables: ModelTable) -> ErwinModel:
    return ErwinModel(name="demo", path="demo.json", tables=list(tables))


class GapAnalysisTests(unittest.TestCase):
    def test_quote_and_normalize(self):
        self.assertEqual(quote_ident("ai_work"), "AI_WORK")
        self.assertEqual(quote_ident("요청SQL"), '"요청SQL"')
        self.assertEqual(normalize_datatype("varchar2(100 CHAR)"), "VARCHAR2(100)")
        self.assertTrue(types_compatible("INTEGER", "NUMBER"))
        self.assertTrue(types_compatible("NUMBER", "NUMBER(38)"))
        self.assertFalse(types_compatible("VARCHAR2(20)", "VARCHAR2(100)"))

    def test_fix_erwin_cp949_bytes_in_unicode(self):
        self.assertEqual(fix_erwin_text("AI_SQL"), "AI_SQL")
        encoded = "표준품질".encode("cp949")
        as_latin = encoded.decode("latin-1")
        self.assertEqual(fix_erwin_text(as_latin), "표준품질")

    def test_missing_table_and_column_ddl(self):
        model = _model(
            ModelTable(
                logical_name="작업",
                physical_name="AI_WORK",
                comment="AI작업",
                columns=[
                    ModelColumn("작업ID", "AI_WORK_ID", "NUMBER", nullable=False, is_pk=True),
                    ModelColumn("요청SQL", "요청SQL", "CLOB", nullable=True),
                    ModelColumn("비고", "NOTE", "VARCHAR2(100)", nullable=True),
                ],
            )
        )
        db = [
            DbTable(
                name="AI_WORK",
                columns=[
                    DbColumn("AI_WORK_ID", "NUMBER", False),
                    DbColumn("요청SQL", "CLOB", True),
                    DbColumn("LEGACY_COL", "VARCHAR2(10)", True),
                ],
                pk_columns=["AI_WORK_ID"],
            ),
            DbTable(name="ONLY_IN_DB", columns=[DbColumn("ID", "NUMBER", False)], pk_columns=["ID"]),
        ]
        items = compare_model_to_db(model, db)
        kinds = {item.kind for item in items}
        self.assertIn("missing_column", kinds)
        self.assertIn("extra_column", kinds)
        self.assertIn("extra_table", kinds)
        public = [item.public() for item in items]
        self.assertTrue(any(item["category"] == "column_add" and item["kind_label"] == "컬럼 추가" for item in public))
        self.assertTrue(any(item["category"] == "table_delete" and item["table"] == "ONLY_IN_DB" for item in public))
        ddl = build_apply_ddl(items)
        self.assertIn("ALTER TABLE AI_WORK ADD NOTE VARCHAR2(100);", ddl)
        self.assertIn("COMMENT ON COLUMN AI_WORK.NOTE IS '비고';", ddl)
        self.assertNotIn("DROP", ddl)
        self.assertNotIn("ONLY_IN_DB", ddl)
        self.assertNotIn("LEGACY_COL", ddl)
        add_only = build_apply_ddl(items, categories=["column_add"])
        self.assertIn("ALTER TABLE AI_WORK ADD NOTE", add_only)
        self.assertNotIn("DROP", add_only)
        delete_only = build_apply_ddl(items, categories=["table_delete", "column_delete"])
        self.assertIn("DROP TABLE ONLY_IN_DB", delete_only)
        self.assertIn("DROP COLUMN LEGACY_COL", delete_only)
        self.assertNotIn("ADD NOTE", delete_only)
        summary = summarize_gaps(items)
        self.assertEqual(summary["categories"]["column_add"], 1)
        self.assertEqual(summary["categories"]["column_delete"], 1)
        self.assertEqual(summary["categories"]["table_delete"], 1)

    def test_create_table_for_missing_model_table(self):
        model = _model(
            ModelTable(
                logical_name="가이드",
                physical_name="AI_SQL_WRT_GUIDE",
                columns=[
                    ModelColumn("ID", "SQL_WRT_GUIDE_ID", "NUMBER", nullable=False, is_pk=True),
                    ModelColumn("이름", "SQL_WRT_GUIDE_NM", "VARCHAR2(100)", nullable=True),
                ],
            )
        )
        items = compare_model_to_db(model, [])
        self.assertEqual(items[0].kind, "missing_table")
        ddl = items[0].ddl
        self.assertIn("CREATE TABLE AI_SQL_WRT_GUIDE", ddl)
        self.assertIn("SQL_WRT_GUIDE_ID NUMBER NOT NULL", ddl)
        self.assertIn("CREATE UNIQUE INDEX PK_AI_SQL_WRT_GUIDE ON AI_SQL_WRT_GUIDE", ddl)
        self.assertIn("(SQL_WRT_GUIDE_ID  ASC);", ddl)
        self.assertIn("ADD CONSTRAINT  PK_AI_SQL_WRT_GUIDE PRIMARY KEY (SQL_WRT_GUIDE_ID)", ddl)
        self.assertNotIn("PRIMARY KEY", ddl.split("CREATE UNIQUE INDEX", 1)[0])
        self.assertIn("COMMENT ON TABLE AI_SQL_WRT_GUIDE IS '가이드';", ddl)

    def test_pk_and_type_diff(self):
        model = _model(
            ModelTable(
                logical_name="T",
                physical_name="T1",
                columns=[
                    ModelColumn("ID", "ID", "NUMBER", nullable=False, is_pk=True),
                    ModelColumn("NM", "NM", "VARCHAR2(200)", nullable=False),
                ],
            )
        )
        db = [
            DbTable(
                name="T1",
                columns=[
                    DbColumn("ID", "NUMBER", False),
                    DbColumn("NM", "VARCHAR2(50)", True),
                ],
                pk_columns=[],
            )
        ]
        items = compare_model_to_db(model, db)
        ddl = build_apply_ddl(items)
        self.assertIn("MODIFY NM VARCHAR2(200) NOT NULL", ddl)
        self.assertIn("CREATE UNIQUE INDEX PK_T1 ON T1", ddl)
        self.assertIn("PRIMARY KEY (ID)", ddl)

    def test_modify_omits_unchanged_nullability(self):
        model = _model(
            ModelTable(
                logical_name="T",
                physical_name="T1",
                columns=[
                    ModelColumn("ID", "ID", "NUMBER", nullable=False, is_pk=True),
                    ModelColumn("NM", "NM", "VARCHAR2(200)", nullable=True),
                ],
            )
        )
        db = [
            DbTable(
                name="T1",
                columns=[
                    DbColumn("ID", "NUMBER", False),
                    DbColumn("NM", "VARCHAR2(50)", True),
                ],
                pk_columns=["ID"],
            )
        ]
        ddl = build_apply_ddl(compare_model_to_db(model, db))
        self.assertIn("MODIFY NM VARCHAR2(200);", ddl)
        self.assertNotIn("MODIFY NM VARCHAR2(200) NULL", ddl)
        self.assertNotIn("MODIFY NM VARCHAR2(200) NOT NULL", ddl)

    def test_pk_column_order_can_be_included_or_excluded(self):
        model = _model(
            ModelTable(
                logical_name="T",
                physical_name="T1",
                columns=[
                    ModelColumn("A", "A", "NUMBER", nullable=False, is_pk=True),
                    ModelColumn("B", "B", "NUMBER", nullable=False, is_pk=True),
                ],
            )
        )
        db = [
            DbTable(
                name="T1",
                columns=[
                    DbColumn("A", "NUMBER", False),
                    DbColumn("B", "NUMBER", False),
                ],
                pk_columns=["B", "A"],
            )
        ]
        hidden = compare_model_to_db(model, db)
        self.assertFalse(any(item.kind == "pk_diff" for item in hidden))
        shown = compare_model_to_db(
            model,
            db,
            GapFilter(subject_areas=[], table_patterns=[], exclude_pk_order=False),
        )
        pk_items = [item for item in shown if item.kind == "pk_diff"]
        self.assertEqual(len(pk_items), 1)
        self.assertIn("순서", pk_items[0].detail)
        membership = compare_model_to_db(
            model,
            [
                DbTable(
                    name="T1",
                    columns=[
                        DbColumn("A", "NUMBER", False),
                        DbColumn("B", "NUMBER", False),
                    ],
                    pk_columns=["A"],
                )
            ],
        )
        self.assertTrue(any(item.kind == "pk_diff" for item in membership))
        parsed = parse_gap_filter({"exclude_pk_order": False, "subject_areas": [], "table_patterns": []})
        self.assertFalse(parsed.exclude_pk_order)
        self.assertTrue(default_gap_filter().exclude_pk_order)

    def test_duplicate_erwin_pk_attributes_are_unique(self):
        payload = {
            "name": "SQL",
            "tables": [
                {
                    "logical_name": "SQL_AUTO_DATA_CHK_ERROR",
                    "physical_name": "SQL_AUTO_DATA_CHK_ERROR",
                    "columns": [
                        {"physical_name": "PROJECT_ID", "datatype": "NUMBER", "is_pk": True},
                        {"physical_name": "SQL_AUTO_PERF_CHECK_ID", "datatype": "NUMBER", "is_pk": True},
                        {"physical_name": "SQL_ID", "datatype": "VARCHAR2(13)", "is_pk": True},
                        {"physical_name": "PROJECT_ID", "datatype": "NUMBER", "is_pk": True},
                        {"physical_name": "SQL_AUTO_PERF_CHECK_ID", "datatype": "NUMBER", "is_pk": True},
                        {"physical_name": "ERR_CODE", "datatype": "VARCHAR2(500)", "is_pk": False},
                        {"physical_name": "DATABASE_KINDS_CD", "datatype": "VARCHAR2(20)", "is_pk": False},
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "model.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            model = load_erwin_model(path)
        names = [item.physical_name for item in model.tables[0].columns]
        self.assertEqual(
            names,
            ["PROJECT_ID", "SQL_AUTO_PERF_CHECK_ID", "SQL_ID", "ERR_CODE", "DATABASE_KINDS_CD"],
        )
        same = [
            DbTable(
                name="SQL_AUTO_DATA_CHK_ERROR",
                columns=[
                    DbColumn("PROJECT_ID", "NUMBER", False),
                    DbColumn("SQL_AUTO_PERF_CHECK_ID", "NUMBER", False),
                    DbColumn("SQL_ID", "VARCHAR2(13)", False),
                    DbColumn("ERR_CODE", "VARCHAR2(500)", False),
                    DbColumn("DATABASE_KINDS_CD", "VARCHAR2(20)", False),
                ],
                pk_columns=["PROJECT_ID", "SQL_AUTO_PERF_CHECK_ID", "SQL_ID"],
            )
        ]
        self.assertFalse(any(item.kind == "pk_diff" for item in compare_model_to_db(model, same)))
        extra = [
            DbTable(
                name="SQL_AUTO_DATA_CHK_ERROR",
                columns=same[0].columns,
                pk_columns=["PROJECT_ID", "SQL_AUTO_PERF_CHECK_ID", "SQL_ID", "DATABASE_KINDS_CD"],
            )
        ]
        pk_item = next(item for item in compare_model_to_db(model, extra) if item.kind == "pk_diff")
        self.assertEqual(pk_item.model, "PROJECT_ID, SQL_AUTO_PERF_CHECK_ID, SQL_ID")
        self.assertEqual(
            pk_item.database,
            "PROJECT_ID, SQL_AUTO_PERF_CHECK_ID, SQL_ID, DATABASE_KINDS_CD",
        )

    def test_load_extracted_json(self):
        payload = {
            "name": "SQL",
            "tables": [
                {
                    "logical_name": "AI작업",
                    "physical_name": "AI_WORK",
                    "columns": [
                        {
                            "logical_name": "AI작업ID",
                            "physical_name": "AI_WORK_ID",
                            "datatype": "NUMBER",
                            "null_option": "NotNull",
                            "is_pk": True,
                        }
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "model.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            model = load_erwin_model(path)
        self.assertEqual(model.tables[0].physical_name, "AI_WORK")
        self.assertFalse(model.tables[0].columns[0].nullable)
        self.assertTrue(model.tables[0].columns[0].is_pk)

    def test_filter_main_subject_area_and_underscore_tables(self):
        self.assertTrue(is_main_subject_area("<Main Subject Area>"))
        self.assertTrue(is_main_subject_area("Main subjectarea"))
        self.assertFalse(is_main_subject_area("09_오픈팝AI(OAI)"))
        payload = {
            "name": "SQL",
            "tables": [
                {
                    "logical_name": "메인전용",
                    "physical_name": "ONLY_MAIN",
                    "subject_areas": ["<Main Subject Area>"],
                    "columns": [{"logical_name": "ID", "physical_name": "ID", "datatype": "NUMBER"}],
                },
                {
                    "logical_name": "작업",
                    "physical_name": "AI_WORK",
                    "subject_areas": ["Main Subject Area", "09_오픈팝AI(OAI)"],
                    "columns": [{"logical_name": "ID", "physical_name": "ID", "datatype": "NUMBER"}],
                },
                {
                    "logical_name": "임시",
                    "physical_name": "_TMP",
                    "subject_areas": ["09_오픈팝AI(OAI)"],
                    "columns": [{"logical_name": "ID", "physical_name": "ID", "datatype": "NUMBER"}],
                },
                {
                    "logical_name": "픽스처",
                    "physical_name": "NO_SA",
                    "columns": [{"logical_name": "ID", "physical_name": "ID", "datatype": "NUMBER"}],
                },
                {
                    "logical_name": "예측",
                    "physical_name": "CPU_LIMIT_PRE",
                    "subject_areas": ["09_오픈팝AI(OAI)"],
                    "columns": [{"logical_name": "ID", "physical_name": "ID", "datatype": "NUMBER"}],
                },
                {
                    "logical_name": "쿼리메딕",
                    "physical_name": "querymedic.SQL_STATS",
                    "subject_areas": ["09_오픈팝AI(OAI)"],
                    "columns": [{"logical_name": "ID", "physical_name": "ID", "datatype": "NUMBER"}],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "model.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            model = load_erwin_model(path)
        names = {item.physical_name for item in model.tables}
        self.assertEqual(names, {"AI_WORK", "NO_SA"})
        kept = filter_model_tables(
            [
                ModelTable("메인", "ONLY_MAIN", subject_areas=["<Main Subject Area>"]),
                ModelTable("작업", "AI_WORK", subject_areas=["09_오픈팝AI(OAI)"]),
                ModelTable("예측", "T1_PRE", subject_areas=["09_오픈팝AI(OAI)"]),
                ModelTable("쿼리메딕", "QUERYMEDIC.T1", subject_areas=["09_오픈팝AI(OAI)"]),
            ]
        )
        self.assertEqual([item.physical_name for item in kept], ["AI_WORK"])

    def test_model_tables_are_not_reported_as_db_deletes(self):
        model = ErwinModel(
            name="demo",
            path="demo.json",
            tables=[
                ModelTable("TB_OPENQ_PDM210", "TB_OPENQ_PDM210", subject_areas=["09_오픈팝"]),
                ModelTable("논리211", "TB_OPENQ_PDM211", subject_areas=["09_오픈팝"]),
                ModelTable("TB_OPENQ_PDM212", "OPENQ.TB_OPENQ_PDM212", subject_areas=["09_오픈팝"]),
            ],
            excluded_tables=[
                ModelTable("TB_OPENQ_PDM213", "TB_OPENQ_PDM213", subject_areas=["Main Subject Area"]),
            ],
        )
        db = [
            DbTable(name="TB_OPENQ_PDM210", columns=[DbColumn("ID", "NUMBER", False)]),
            DbTable(name="TB_OPENQ_PDM211", columns=[DbColumn("ID", "NUMBER", False)]),
            DbTable(name="TB_OPENQ_PDM212", columns=[DbColumn("ID", "NUMBER", False)]),
            DbTable(name="TB_OPENQ_PDM213", columns=[DbColumn("ID", "NUMBER", False)]),
            DbTable(name="ONLY_IN_DB", columns=[DbColumn("ID", "NUMBER", False)]),
            DbTable(name="TMP_PRE", columns=[DbColumn("ID", "NUMBER", False)]),
        ]
        items = compare_model_to_db(model, db)
        extras = [item.table for item in items if item.kind == "extra_table"]
        self.assertEqual(extras, ["ONLY_IN_DB"])
        self.assertFalse(any(item.kind == "missing_table" for item in items))

    def test_custom_gap_filter_can_add_and_clear_rules(self):
        defaults = default_gap_filter()
        self.assertEqual(defaults.subject_areas, ["Main Subject Area"])
        self.assertEqual(
            [(item.match, item.value) for item in defaults.table_patterns],
            [("prefix", "_"), ("suffix", "_PRE"), ("prefix", "querymedic.")],
        )
        tables = [
            ModelTable("작업", "AI_WORK", subject_areas=["09_오픈팝AI(OAI)"]),
            ModelTable("임시", "_TMP", subject_areas=["09_오픈팝AI(OAI)"]),
        ]
        none = GapFilter(subject_areas=[], table_patterns=[])
        names = [item.physical_name for item in filter_model_tables(tables, none)]
        self.assertEqual(names, ["AI_WORK", "_TMP"])
        custom = GapFilter(subject_areas=["09_오픈팝AI(OAI)"], table_patterns=[TablePattern("exact", "AI_WORK")])
        self.assertEqual(filter_model_tables(tables, custom), [])
        loaded = parse_gap_filter({"subject_areas": ["Main Subject Area"], "table_patterns": []})
        self.assertEqual(loaded.table_patterns, [])

    def test_prefix_suffix_table_pattern(self):
        settings = GapFilter(
            subject_areas=[],
            table_patterns=[TablePattern("prefix_suffix", "VSQL", "PRE")],
        )
        self.assertTrue(table_name_excluded("VSQL_REPORT_PRE", settings))
        self.assertTrue(table_name_excluded("vsql_x_pre", settings))
        self.assertFalse(table_name_excluded("VSQL_REPORT", settings))
        self.assertFalse(table_name_excluded("REPORT_PRE", settings))
        self.assertFalse(table_name_excluded("AI_WORK", settings))
        parsed = parse_gap_filter(
            {
                "subject_areas": [],
                "table_patterns": [{"match": "prefix_suffix", "value": "VSQL", "end": "PRE"}],
            }
        )
        self.assertEqual(parsed.table_patterns[0].match, "prefix_suffix")
        self.assertEqual(parsed.table_patterns[0].end, "PRE")
        skipped = parse_gap_filter(
            {
                "subject_areas": [],
                "table_patterns": [{"match": "prefix_suffix", "value": "VSQL"}],
            }
        )
        self.assertEqual(skipped.table_patterns, [])

    def test_apply_item_to_model_and_db_sql(self):
        model = _model(
            ModelTable(
                logical_name="작업",
                physical_name="AI_WORK",
                subject_areas=["09_오픈팝AI(OAI)"],
                columns=[
                    ModelColumn("ID", "AI_WORK_ID", "NUMBER", nullable=False, is_pk=True),
                    ModelColumn("비고", "NOTE", "VARCHAR2(10)", nullable=True),
                ],
            )
        )
        db = [
            DbTable(
                name="AI_WORK",
                columns=[
                    DbColumn("AI_WORK_ID", "NUMBER", False),
                    DbColumn("NOTE", "VARCHAR2(100)", False),
                    DbColumn("EXTRA_COL", "DATE", True),
                ],
                pk_columns=["AI_WORK_ID"],
            ),
            DbTable(
                name="ONLY_IN_DB",
                columns=[DbColumn("ID", "NUMBER", False)],
                pk_columns=["ID"],
            ),
        ]
        extra = GapItem("extra_table", "ONLY_IN_DB", "", "", "ONLY_IN_DB", "DB에만")
        self.assertIn("DROP TABLE ONLY_IN_DB", db_apply_sql(extra))
        apply_item_to_model(model, extra, db)
        self.assertTrue(any(item.physical_name == "ONLY_IN_DB" for item in model.tables))
        change = GapItem(
            "type_diff",
            "AI_WORK",
            "NOTE",
            "VARCHAR2(10) NULL",
            "VARCHAR2(100) NOT NULL",
            "타입이 다릅니다.",
        )
        apply_item_to_model(model, change, db)
        note = next(item for item in model.tables[0].columns if item.physical_name == "NOTE")
        self.assertEqual(note.datatype, "VARCHAR2(100)")
        self.assertFalse(note.nullable)
        missing = GapItem("missing_column", "AI_WORK", "NOTE", "", "", "모델에만")
        apply_item_to_model(model, missing, db)
        self.assertFalse(any(item.physical_name == "NOTE" for item in model.tables[0].columns))


if __name__ == "__main__":
    unittest.main()
