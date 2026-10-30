"""Compare an ERwin physical model with an Oracle schema and build apply DDL."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.erwin_model import ErwinModel, ModelColumn, ModelTable
from app.gap_filter import GapFilter, default_gap_filter, subject_area_excluded, table_name_excluded

_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")
_TYPE_SPACE_RE = re.compile(r"\s+")
_TYPE_CHARSET_RE = re.compile(r"\s*\(\s*(\d+)\s*(?:BYTE|CHAR)?\s*\)", re.I)
_NUMBER_RE = re.compile(r"^NUMBER(?:\((\d+)(?:\s*,\s*(\d+))?\))?$")


@dataclass
class DbColumn:
    name: str
    datatype: str
    nullable: bool
    comment: str = ""


@dataclass
class DbTable:
    name: str
    columns: list[DbColumn] = field(default_factory=list)
    pk_columns: list[str] = field(default_factory=list)
    comment: str = ""


@dataclass
class GapItem:
    kind: str
    table: str
    column: str
    model: str
    database: str
    detail: str
    ddl: str = ""
    apply: bool = False

    def public(self) -> dict:
        category = CATEGORY_OF.get(self.kind, self.kind)
        return {
            "kind": self.kind,
            "category": category,
            "kind_label": CATEGORY_LABELS.get(category, self.kind),
            "table": self.table,
            "column": self.column,
            "model": self.model,
            "database": self.database,
            "detail": self.detail,
            "ddl": self.ddl,
            "db_ddl": db_apply_sql(self),
            "apply": self.apply,
        }


KIND_LABELS = {
    "missing_table": "테이블 추가",
    "missing_column": "컬럼 추가",
    "type_diff": "컬럼 변경",
    "null_diff": "컬럼 변경",
    "pk_diff": "컬럼 변경",
    "extra_table": "테이블 삭제",
    "extra_column": "컬럼 삭제",
}

CATEGORY_OF = {
    "missing_table": "table_add",
    "extra_table": "table_delete",
    "missing_column": "column_add",
    "extra_column": "column_delete",
    "type_diff": "column_change",
    "null_diff": "column_change",
    "pk_diff": "column_change",
}

CATEGORY_LABELS = {
    "table_add": "테이블 추가",
    "table_delete": "테이블 삭제",
    "column_add": "컬럼 추가",
    "column_delete": "컬럼 삭제",
    "column_change": "컬럼 변경",
}

FILTER_CATEGORIES = (
    "table_add",
    "table_delete",
    "column_add",
    "column_delete",
    "column_change",
)


def quote_ident(name: str) -> str:
    text = (name or "").strip()
    if not text:
        raise ValueError("이름이 비어 있습니다.")
    if _IDENT_RE.fullmatch(text):
        return text.upper()
    return '"' + text.replace('"', '""') + '"'


def object_key(name: str) -> str:
    text = (name or "").strip()
    if _IDENT_RE.fullmatch(text):
        return text.upper()
    return text


def table_match_keys(name: str) -> list[str]:
    text = (name or "").strip()
    keys: list[str] = []
    for raw in (text, text.rsplit(".", 1)[-1] if "." in text else ""):
        key = object_key(raw)
        if key and key not in keys:
            keys.append(key)
    return keys


def model_table_keys(table: ModelTable) -> list[str]:
    keys: list[str] = []
    for name in (table.key(), table.physical_name, table.logical_name):
        for key in table_match_keys(name):
            if key not in keys:
                keys.append(key)
    return keys


def _db_table_for(table: ModelTable, db_map: dict[str, DbTable]) -> DbTable | None:
    for key in model_table_keys(table):
        found = db_map.get(key)
        if found is not None:
            return found
    return None


def _unique_keys(names: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = object_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def normalize_datatype(value: str) -> str:
    text = _TYPE_SPACE_RE.sub(" ", (value or "").strip().upper())
    text = text.replace(" BYTE)", ")").replace(" CHAR)", ")")
    text = _TYPE_CHARSET_RE.sub(lambda match: f"({match.group(1)})", text)
    aliases = {
        "INT": "NUMBER",
        "INTEGER": "NUMBER",
        "SMALLINT": "NUMBER",
        "DECIMAL": "NUMBER",
        "NUMERIC": "NUMBER",
        "VARCHAR": "VARCHAR2",
        "CHARACTER VARYING": "VARCHAR2",
        "LONG RAW": "LONG RAW",
    }
    if text in aliases:
        return aliases[text]
    prefix = text.split("(", 1)[0].strip()
    if prefix in aliases and "(" in text:
        return aliases[prefix] + text[len(prefix) :]
    if text in {"NUMBER(38)", "NUMBER(38,0)"}:
        return "NUMBER"
    return text


def types_compatible(model_type: str, db_type: str) -> bool:
    left = normalize_datatype(model_type)
    right = normalize_datatype(db_type)
    if left == right:
        return True
    left_num = _NUMBER_RE.match(left)
    right_num = _NUMBER_RE.match(right)
    if left_num and right_num:
        if left == "NUMBER" and right in {"NUMBER", "NUMBER(38)", "NUMBER(38,0)"}:
            return True
        if right == "NUMBER" and left in {"NUMBER", "NUMBER(38)", "NUMBER(38,0)"}:
            return True
    return False


def oracle_data_type(data_type: str, data_length, data_precision, data_scale, char_length) -> str:
    kind = str(data_type or "").upper()
    if kind in {"VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR", "RAW"}:
        length = char_length or data_length
        return f"{kind}({int(length)})" if length else kind
    if kind == "NUMBER":
        if data_precision in (None, ""):
            return "NUMBER"
        precision = int(data_precision)
        scale = 0 if data_scale in (None, "") else int(data_scale)
        if scale:
            return f"NUMBER({precision},{scale})"
        if precision == 38:
            return "NUMBER"
        return f"NUMBER({precision})"
    if kind == "FLOAT" and data_precision not in (None, ""):
        return f"FLOAT({int(data_precision)})"
    return kind or "VARCHAR2(100)"


def compare_model_to_db(
    model: ErwinModel,
    tables: list[DbTable],
    settings: GapFilter | None = None,
) -> list[GapItem]:
    filt = settings or default_gap_filter()
    db_map: dict[str, DbTable] = {}
    for item in tables:
        for key in table_match_keys(item.name):
            db_map.setdefault(key, item)
    items: list[GapItem] = []
    seen_model: set[str] = set()
    matched_db: set[str] = set()
    for table in model.tables:
        ident = object_key(table.key())
        if not ident or ident in seen_model:
            continue
        seen_model.add(ident)
        db_table = _db_table_for(table, db_map)
        if db_table is None:
            items.append(
                GapItem(
                    kind="missing_table",
                    table=table.key(),
                    column="",
                    model=table.logical_name or table.key(),
                    database="",
                    detail="모델에 있는 테이블이 DB에 없습니다.",
                    ddl=create_table_sql(table),
                    apply=True,
                )
            )
            continue
        matched_db.update(table_match_keys(db_table.name))
        items.extend(_compare_table(table, db_table, filt))
    known_model = set(matched_db)
    for table in list(model.tables) + list(model.excluded_tables):
        known_model.update(model_table_keys(table))
    for db_table in tables:
        keys = table_match_keys(db_table.name)
        if any(key in known_model for key in keys):
            continue
        if any(table_name_excluded(name, filt) for name in (db_table.name, *keys)):
            continue
        items.append(
            GapItem(
                kind="extra_table",
                table=db_table.name,
                column="",
                model="",
                database=db_table.name,
                detail="DB에만 있는 테이블입니다. 삭제 DDL은 만들지 않습니다.",
            )
        )
    return items


def db_apply_sql(item: GapItem) -> str:
    if (item.ddl or "").strip():
        return item.ddl.strip() + ("\n" if not item.ddl.endswith("\n") else "")
    if item.kind == "extra_table" and item.table:
        return f"DROP TABLE {quote_ident(item.table)};\n"
    if item.kind == "extra_column" and item.table and item.column:
        return (
            f"ALTER TABLE {quote_ident(item.table)} DROP COLUMN {quote_ident(item.column)};\n"
        )
    return ""


def apply_item_to_model(model: ErwinModel, item: GapItem, db_tables: list[DbTable] | None = None) -> None:
    db_map = {object_key(table.name): table for table in db_tables or []}
    table_key = object_key(item.table)
    if not table_key:
        raise ValueError("테이블 이름이 없습니다.")
    if item.kind == "missing_table":
        kept = [table for table in model.tables if object_key(table.key()) != table_key]
        if len(kept) == len(model.tables):
            raise ValueError("모델에서 해당 테이블을 찾지 못했습니다.")
        model.tables = kept
        return
    if item.kind == "extra_table":
        db_table = db_map.get(table_key)
        if db_table is None:
            raise ValueError("DB 카탈로그에서 테이블을 찾지 못했습니다.")
        if any(object_key(table.key()) == table_key for table in model.tables):
            return
        model.tables.append(_model_table_from_db(db_table, _inherit_subject_areas(model)))
        return
    table = next((entry for entry in model.tables if object_key(entry.key()) == table_key), None)
    if table is None:
        raise ValueError("모델에서 해당 테이블을 찾지 못했습니다.")
    column_key = object_key(item.column)
    if item.kind == "missing_column":
        if not column_key:
            raise ValueError("컬럼 이름이 없습니다.")
        kept = [column for column in table.columns if object_key(column.key()) != column_key]
        if len(kept) == len(table.columns):
            raise ValueError("모델에서 해당 컬럼을 찾지 못했습니다.")
        table.columns = kept
        return
    db_table = db_map.get(table_key)
    if item.kind == "extra_column":
        db_col = None
        if db_table is not None:
            db_col = next((column for column in db_table.columns if object_key(column.name) == column_key), None)
        if db_col is None:
            db_col = _parse_db_column(item.column, item.database)
        if any(object_key(column.key()) == object_key(db_col.name) for column in table.columns):
            return
        table.columns.append(_model_column_from_db(db_col, db_table.pk_columns if db_table else []))
        return
    if item.kind in {"type_diff", "null_diff", "pk_diff"}:
        if item.kind == "pk_diff":
            pk = {object_key(name) for name in (db_table.pk_columns if db_table is not None else [])}
            for column in table.columns:
                column.is_pk = object_key(column.key()) in pk
            return
        column = next((entry for entry in table.columns if object_key(entry.key()) == column_key), None)
        if column is None:
            raise ValueError("모델에서 해당 컬럼을 찾지 못했습니다.")
        db_col = None
        if db_table is not None:
            db_col = next((entry for entry in db_table.columns if object_key(entry.name) == column_key), None)
        if db_col is None:
            db_col = _parse_db_column(item.column, item.database)
        column.datatype = db_col.datatype or column.datatype
        column.nullable = db_col.nullable
        return
    raise ValueError("이 항목은 모델에 반영할 수 없습니다.")


def model_apply_op(item: GapItem, model: ErwinModel, db_tables: list[DbTable] | None = None) -> dict:
    db_map = {object_key(table.name): table for table in db_tables or []}
    if item.kind == "missing_table":
        return {"action": "delete_table", "table": item.table}
    if item.kind == "missing_column":
        return {"action": "delete_column", "table": item.table, "column": item.column}
    if item.kind == "extra_table":
        db_table = db_map.get(object_key(item.table))
        if db_table is None:
            raise ValueError("DB 카탈로그에서 테이블을 찾지 못했습니다.")
        table = _model_table_from_db(db_table, _inherit_subject_areas(model))
        op = {
            "action": "add_table",
            "table": table.key(),
            "logical_name": table.logical_name,
            "physical_name": table.physical_name,
            "comment": table.comment,
            "col_count": str(len(table.columns)),
        }
        for index, column in enumerate(table.columns):
            prefix = f"c{index}_"
            op[prefix + "logical_name"] = column.logical_name
            op[prefix + "physical_name"] = column.physical_name
            op[prefix + "datatype"] = column.datatype
            op[prefix + "nullable"] = "1" if column.nullable else "0"
            op[prefix + "is_pk"] = "1" if column.is_pk else "0"
            op[prefix + "comment"] = column.comment
        return op
    if item.kind == "extra_column":
        db_table = db_map.get(object_key(item.table))
        db_col = None
        if db_table is not None:
            db_col = next(
                (column for column in db_table.columns if object_key(column.name) == object_key(item.column)),
                None,
            )
        if db_col is None:
            db_col = _parse_db_column(item.column, item.database)
        return {
            "action": "add_column",
            "table": item.table,
            "column": db_col.name,
            "logical_name": db_col.name,
            "physical_name": db_col.name,
            "datatype": db_col.datatype,
            "nullable": "1" if db_col.nullable else "0",
            "comment": db_col.comment,
        }
    if item.kind in {"type_diff", "null_diff"}:
        db_table = db_map.get(object_key(item.table))
        db_col = None
        if db_table is not None:
            db_col = next(
                (column for column in db_table.columns if object_key(column.name) == object_key(item.column)),
                None,
            )
        if db_col is None:
            db_col = _parse_db_column(item.column, item.database)
        return {
            "action": "modify_column",
            "table": item.table,
            "column": item.column,
            "datatype": db_col.datatype,
            "nullable": "1" if db_col.nullable else "0",
        }
    if item.kind == "pk_diff":
        return {"action": "skip"}
    raise ValueError("이 항목은 모델에 반영할 수 없습니다.")


def _inherit_subject_areas(model: ErwinModel, settings: GapFilter | None = None) -> list[str]:
    filt = settings or default_gap_filter()
    for table in model.tables:
        areas = [item for item in table.subject_areas if item.strip() and not subject_area_excluded(item, filt)]
        if areas:
            return areas
    return ["갭분석반영"]


def _model_table_from_db(db_table: DbTable, subject_areas: list[str]) -> ModelTable:
    return ModelTable(
        logical_name=db_table.name,
        physical_name=db_table.name,
        comment=db_table.comment,
        columns=[_model_column_from_db(column, db_table.pk_columns) for column in db_table.columns],
        subject_areas=list(subject_areas),
    )


def _model_column_from_db(column: DbColumn, pk_columns: list[str] | None = None) -> ModelColumn:
    pk = {object_key(name) for name in pk_columns or []}
    return ModelColumn(
        logical_name=column.name,
        physical_name=column.name,
        datatype=column.datatype,
        nullable=column.nullable,
        is_pk=object_key(column.name) in pk,
        comment=column.comment,
    )


def _parse_db_column(name: str, text: str) -> DbColumn:
    raw = (text or "").strip()
    nullable = True
    datatype = raw
    upper = raw.upper()
    if upper.endswith(" NOT NULL"):
        nullable = False
        datatype = raw[: -len(" NOT NULL")].strip()
    elif upper.endswith(" NULL"):
        datatype = raw[: -len(" NULL")].strip()
    if not name:
        raise ValueError("컬럼 이름이 없습니다.")
    return DbColumn(name=name, datatype=datatype or "VARCHAR2(100)", nullable=nullable)


def build_apply_ddl(items: list[GapItem], categories: list[str] | set[str] | None = None) -> str:
    allowed = None if categories is None else {str(item) for item in categories}
    parts: list[str] = []
    for item in items:
        category = CATEGORY_OF.get(item.kind, item.kind)
        if allowed is not None and category not in allowed:
            continue
        if allowed is None:
            sql = item.ddl.strip() if item.apply else ""
        else:
            sql = db_apply_sql(item).strip()
        if sql:
            parts.append(sql)
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n"


def summarize_gaps(items: list[GapItem]) -> dict:
    counts = {kind: 0 for kind in KIND_LABELS}
    categories = {key: 0 for key in CATEGORY_LABELS}
    for item in items:
        counts[item.kind] = counts.get(item.kind, 0) + 1
        category = CATEGORY_OF.get(item.kind)
        if category:
            categories[category] += 1
    apply_count = sum(1 for item in items if item.apply)
    return {
        "total": len(items),
        "apply": apply_count,
        "missing_table": counts["missing_table"],
        "missing_column": counts["missing_column"],
        "type_diff": counts["type_diff"],
        "null_diff": counts["null_diff"],
        "pk_diff": counts["pk_diff"],
        "extra_table": counts["extra_table"],
        "extra_column": counts["extra_column"],
        "categories": categories,
    }


def create_table_sql(table: ModelTable) -> str:
    name = quote_ident(table.key())
    lines = []
    pk: list[str] = []
    for column in table.columns:
        col_name = quote_ident(column.key())
        datatype = column.datatype.strip() or "VARCHAR2(100)"
        nulls = "" if column.nullable else " NOT NULL"
        lines.append(f"  {col_name} {datatype}{nulls}")
        if column.is_pk:
            pk.append(column.key())
    body = ",\n".join(lines) if lines else "  DUMMY NUMBER"
    parts = [f"CREATE TABLE {name} (\n{body}\n);"]
    if pk:
        parts.append("")
        parts.append(pk_create_sql(table.key(), pk))
    parts.extend(_comment_sql(table, table.columns, new_table=True))
    return "\n".join(parts)


def _compare_table(table: ModelTable, db_table: DbTable, settings: GapFilter | None = None) -> list[GapItem]:
    items: list[GapItem] = []
    db_cols = {object_key(item.name): item for item in db_table.columns}
    seen = set()
    for column in table.columns:
        key = object_key(column.key())
        if not key or key in seen:
            continue
        seen.add(key)
        db_col = db_cols.get(key)
        if db_col is None:
            items.append(
                GapItem(
                    kind="missing_column",
                    table=table.key(),
                    column=column.key(),
                    model=_column_model_text(column),
                    database="",
                    detail="모델에 있는 컬럼이 DB에 없습니다.",
                    ddl=add_column_sql(table, column),
                    apply=True,
                )
            )
            continue
        type_changed = bool(column.datatype) and not types_compatible(column.datatype, db_col.datatype)
        null_changed = (not column.nullable) != (not db_col.nullable)
        if type_changed or null_changed:
            model_null = "NULL" if column.nullable else "NOT NULL"
            db_null = "NULL" if db_col.nullable else "NOT NULL"
            kind = "type_diff" if type_changed else "null_diff"
            detail = "데이터 타입과 NULL 허용 여부가 다릅니다." if type_changed and null_changed else (
                "데이터 타입이 다릅니다." if type_changed else "NULL 허용 여부가 다릅니다."
            )
            items.append(
                GapItem(
                    kind=kind,
                    table=table.key(),
                    column=column.key(),
                    model=f"{column.datatype} {model_null}".strip(),
                    database=f"{db_col.datatype} {db_null}".strip(),
                    detail=detail,
                    ddl=modify_column_sql(
                        table,
                        column,
                        type_changed=type_changed,
                        null_changed=null_changed,
                    ),
                    apply=True,
                )
            )
    for db_col in db_table.columns:
        if object_key(db_col.name) not in seen:
            items.append(
                GapItem(
                    kind="extra_column",
                    table=table.key(),
                    column=db_col.name,
                    model="",
                    database=_db_column_text(db_col),
                    detail="DB에만 있는 컬럼입니다. 삭제 DDL은 만들지 않습니다.",
                )
            )
    items.extend(_pk_items(table, db_table, settings))
    return items


def add_column_sql(table: ModelTable, column: ModelColumn) -> str:
    datatype = column.datatype.strip() or "VARCHAR2(100)"
    nulls = "" if column.nullable else " NOT NULL"
    sql = (
        f"ALTER TABLE {quote_ident(table.key())} ADD "
        f"{quote_ident(column.key())} {datatype}{nulls};"
    )
    comments = _comment_sql(table, [column], new_table=False)
    return "\n".join([sql, *comments])


def modify_column_sql(
    table: ModelTable,
    column: ModelColumn,
    *,
    type_changed: bool = True,
    null_changed: bool = True,
) -> str:
    pieces: list[str] = []
    if type_changed:
        pieces.append(column.datatype.strip() or "VARCHAR2(100)")
    if null_changed:
        pieces.append("NULL" if column.nullable else "NOT NULL")
    if not pieces:
        pieces.append(column.datatype.strip() or "VARCHAR2(100)")
    return (
        f"ALTER TABLE {quote_ident(table.key())} MODIFY "
        f"{quote_ident(column.key())} {' '.join(pieces)};"
    )


def pk_create_sql(table_name: str, columns: list[str]) -> str:
    names = [quote_ident(name) for name in columns if str(name).strip()]
    if not names:
        raise ValueError("PK 컬럼이 없습니다.")
    table = quote_ident(table_name)
    constraint = quote_ident(_pk_name(table_name))
    index_cols = ",".join(f"{name}  ASC" for name in names)
    pk_cols = ",".join(names)
    return (
        f"CREATE UNIQUE INDEX {constraint} ON {table}\n"
        f"({index_cols});\n"
        f"\n"
        f"ALTER TABLE {table}\n"
        f"\tADD CONSTRAINT  {constraint} PRIMARY KEY ({pk_cols});"
    )


def _pk_items(table: ModelTable, db_table: DbTable, settings: GapFilter | None = None) -> list[GapItem]:
    model_pk = _unique_keys(item.key() for item in table.columns if item.is_pk)
    db_pk = _unique_keys(db_table.pk_columns)
    if model_pk == db_pk:
        return []
    order_only = bool(model_pk) and bool(db_pk) and set(model_pk) == set(db_pk)
    filt = settings or default_gap_filter()
    if order_only and filt.exclude_pk_order:
        return []
    model_text = ", ".join(model_pk) or "(없음)"
    db_text = ", ".join(db_pk) or "(없음)"
    ddl = ""
    apply = False
    if model_pk and not db_pk:
        ddl = pk_create_sql(table.key(), model_pk)
        apply = True
        detail = "모델 PK가 DB에 없습니다."
    elif order_only:
        detail = "PK 컬럼 순서가 다릅니다. 기존 PK를 자동으로 바꾸지 않습니다."
    else:
        detail = "PK 구성이 다릅니다. 기존 PK를 자동으로 바꾸지 않습니다."
    return [
        GapItem(
            kind="pk_diff",
            table=table.key(),
            column="",
            model=model_text,
            database=db_text,
            detail=detail,
            ddl=ddl,
            apply=apply,
        )
    ]


def _comment_sql(table: ModelTable, columns: list[ModelColumn], *, new_table: bool) -> list[str]:
    parts: list[str] = []
    table_name = quote_ident(table.key())
    table_comment = (table.comment or table.logical_name or "").strip()
    if new_table and table_comment and table_comment != table.key():
        parts.append(f"COMMENT ON TABLE {table_name} IS '{_sql_str(table_comment)}';")
    for column in columns:
        comment = (column.comment or column.logical_name or "").strip()
        if not comment or comment == column.key():
            continue
        parts.append(
            f"COMMENT ON COLUMN {table_name}.{quote_ident(column.key())} IS '{_sql_str(comment)}';"
        )
    return parts


def _pk_name(table_name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", table_name)
    name = f"PK_{base}"
    return name[:30] if _IDENT_RE.fullmatch(base) else name


def _sql_str(value: str) -> str:
    return value.replace("'", "''")


def _column_model_text(column: ModelColumn) -> str:
    nulls = "NULL" if column.nullable else "NOT NULL"
    return f"{column.datatype} {nulls}".strip()


def _db_column_text(column: DbColumn) -> str:
    nulls = "NULL" if column.nullable else "NOT NULL"
    return f"{column.datatype} {nulls}".strip()
