import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import oracle_client
from app.oracle_client import client_major, find_client_dir, usable_client_dir
from app.oracle_runner import open_connection, read_plsql_errors
from app.models import ConnectionProfile


def _write_pe(path: Path, machine: int = 0x8664) -> None:
    data = bytearray(0x80)
    data[0:2] = b"MZ"
    data[0x3C:0x40] = (0x40).to_bytes(4, "little")
    data[0x40:0x44] = b"PE\x00\x00"
    data[0x44:0x46] = machine.to_bytes(2, "little")
    path.write_bytes(data)


class OracleClientTests(unittest.TestCase):
    def tearDown(self):
        oracle_client._thick_ready = False
        oracle_client._thick_error = ""
        oracle_client._thick_dir = None

    def test_skips_32bit_and_old_client(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            _write_pe(folder / "oci.dll", 0x014C)
            (folder / "oraociei11.dll").write_bytes(b"")
            self.assertFalse(usable_client_dir(folder))

    def test_accepts_64bit_client_19(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            _write_pe(folder / "oci.dll")
            (folder / "oraociei19.dll").write_bytes(b"")
            self.assertTrue(usable_client_dir(folder))
            self.assertEqual(client_major(folder), 19)

    def test_finds_nested_extracted_client(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "instantclient-basic-windows.x64-19.32.0.0.0dbru" / "instantclient_19_32"
            nested.mkdir(parents=True)
            _write_pe(nested / "oci.dll")
            (nested / "oraociei19.dll").write_bytes(b"")
            with patch.object(oracle_client, "client_search_roots", return_value=[nested.parent]):
                found = find_client_dir()
            self.assertEqual(found, nested)

    def test_finds_client_from_env(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            _write_pe(folder / "oci.dll")
            (folder / "oraociei19.dll").write_bytes(b"")
            with patch.dict("os.environ", {"OCM_ORACLE_CLIENT": str(folder)}, clear=False):
                self.assertEqual(find_client_dir(), folder.resolve())

    def test_open_connection_retries_in_thick_mode_after_dpy_3010(self):
        profile = ConnectionProfile(
            id="dev",
            name="개발",
            host="127.0.0.1",
            identifier="ORCL",
            username="scott",
            password="secret",
        )
        oracledb = type(
            "Oracle",
            (),
            {
                "defaults": type("Defaults", (), {"fetch_lobs": True})(),
                "makedsn": staticmethod(lambda host, port, **kwargs: f"{host}:{port}/{kwargs.get('service_name') or kwargs.get('sid')}"),
                "connect": Mock(
                    side_effect=[
                        Exception("DPY-3010: connections to this database server version are not supported"),
                        object(),
                    ]
                ),
            },
        )
        with (
            patch("app.oracle_runner._require_oracledb", return_value=oracledb),
            patch("app.oracle_runner.find_client_dir", return_value=None),
            patch("app.oracle_runner.ensure_thick_mode") as thick,
            patch("app.oracle_runner._apply_schema"),
        ):
            conn = open_connection(profile)
        self.assertIsNotNone(conn)
        thick.assert_called_once()
        self.assertEqual(oracledb.connect.call_count, 2)

    def test_read_plsql_errors_from_all_errors(self):
        class FakeCursor:
            def __init__(self, rows):
                self.rows = rows
                self.warning = None

            def execute(self, sql, params=None):
                self.params = params

            def fetchall(self):
                return self.rows

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class FakeConn:
            def cursor(self):
                return FakeCursor([(10, 5, "ERROR", "PLS-00201: identifier T must be declared")])

        sql = "CREATE OR REPLACE PROCEDURE OPENPOP.SP_FOO IS BEGIN NULL; END;"
        lines = read_plsql_errors(FakeConn(), sql, FakeCursor([]))
        self.assertEqual(lines[0], "OPENPOP.SP_FOO 컴파일 오류")
        self.assertIn("LINE 10, POS 5", lines[1])
        self.assertIn("PLS-00201", lines[1])


if __name__ == "__main__":
    unittest.main()
