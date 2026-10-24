import os
import tempfile
import unittest
from pathlib import Path


class SmokeUiTests(unittest.TestCase):
    def test_window_builds(self):
        try:
            import customtkinter  # noqa: F401
        except ImportError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OCM_STORE_PATH"] = str(Path(tmp) / "connections.json")
            window = None
            try:
                import tkinter as tk

                from app.ui_main import MainWindow

                try:
                    window = MainWindow()
                except tk.TclError as exc:
                    self.skipTest(str(exc))
                from app.models import ConnectionProfile
                from app.sample_script import SAMPLE_SCRIPT

                window.profiles = [
                    ConnectionProfile(
                        id="dev",
                        name="개발",
                        host="127.0.0.1",
                        port=1521,
                        identifier="ORCL",
                        username="scott",
                        password="secret",
                    )
                ]
                window.active_id = "dev"
                window._refresh_cards()
                window._refresh_header()
                window._set_editor(SAMPLE_SCRIPT)
                window.inspect_script()
                window.update()
                log_text = window.log.get("1.0", "end")
                self.assertIn("함수", log_text)
                self.assertIn("패키지 바디", log_text)
                self.assertEqual(window.title(), "오라클 오브젝트 변경 관리")
                window.open_manager()
                window.update()
                self.assertIsNotNone(window._manager)
                self.assertTrue(window._manager.winfo_exists())
                window._manager.destroy()
            finally:
                if window is not None:
                    window.destroy()
                os.environ.pop("OCM_STORE_PATH", None)


if __name__ == "__main__":
    unittest.main()
