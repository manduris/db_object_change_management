"""Read Oracle table/column/PK metadata for the current schema."""

from __future__ import annotations

from app.gap_analysis import DbColumn, DbTable, oracle_data_type
from app.oracle_runner import OracleSession, format_db_error

_TABLE_SQL = """
SELECT table_name
  FROM all_tables
 WHERE owner = :owner
   AND temporary = 'N'
   AND nested = 'NO'
 ORDER BY table_name
"""

_COLUMN_SQL = """
SELECT table_name, column_name, data_type, data_length, data_precision, data_scale,
       nullable, char_length
  FROM all_tab_columns
 WHERE owner = :owner
 ORDER BY table_name, column_id
"""

_PK_SQL = """
SELECT acc.table_name, acc.column_name, acc.position
  FROM all_constraints ac
  JOIN all_cons_columns acc
    ON ac.owner = acc.owner
   AND ac.constraint_name = acc.constraint_name
 WHERE ac.owner = :owner
   AND ac.constraint_type = 'P'
 ORDER BY acc.table_name, acc.position
"""

_TABLE_COMMENT_SQL = """
SELECT table_name, comments
  FROM all_tab_comments
 WHERE owner = :owner
   AND table_type = 'TABLE'
"""

_COLUMN_COMMENT_SQL = """
SELECT table_name, column_name, comments
  FROM all_col_comments
 WHERE owner = :owner
"""


def fetch_db_tables(session: OracleSession) -> tuple[str, list[DbTable]]:
    if session.conn is None or not session.connected:
        raise ValueError("데이터베이스에 연결되어 있지 않습니다.")
    owner = (session.info.schema if session.info is not None else "") or ""
    owner = owner.strip().upper()
    if not owner:
        raise ValueError("현재 스키마를 확인할 수 없습니다.")
    try:
        with session.lock:
            conn = session.conn
            if conn is None:
                raise ValueError("데이터베이스에 연결되어 있지 않습니다.")
            tables = _query_tables(conn, owner)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("DB 카탈로그를 읽지 못했습니다. " + format_db_error(exc)) from exc
    return owner, tables


def _query_tables(conn, owner: str) -> list[DbTable]:
    names = [str(row[0]) for row in _fetch(conn, _TABLE_SQL, owner)]
    tables = {name: DbTable(name=name) for name in names}
    comments = {str(row[0]): str(row[1] or "") for row in _fetch(conn, _TABLE_COMMENT_SQL, owner)}
    col_comments = {
        (str(row[0]), str(row[1])): str(row[2] or "")
        for row in _fetch(conn, _COLUMN_COMMENT_SQL, owner)
    }
    for row in _fetch(conn, _COLUMN_SQL, owner):
        table_name = str(row[0])
        table = tables.get(table_name)
        if table is None:
            continue
        column_name = str(row[1])
        table.columns.append(
            DbColumn(
                name=column_name,
                datatype=oracle_data_type(str(row[2] or ""), row[3], row[4], row[5], row[7]),
                nullable=str(row[6] or "Y").upper() != "N",
                comment=col_comments.get((table_name, column_name), ""),
            )
        )
    for row in _fetch(conn, _PK_SQL, owner):
        table = tables.get(str(row[0]))
        if table is None:
            continue
        table.pk_columns.append(str(row[1]))
    for name, table in tables.items():
        table.comment = comments.get(name, "")
    return [tables[name] for name in names]


def _fetch(conn, sql: str, owner: str) -> list[tuple]:
    with conn.cursor() as cursor:
        cursor.execute(sql, {"owner": owner})
        return list(cursor.fetchall() or [])
