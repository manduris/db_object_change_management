"""Oracle object and data change runner."""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from pathlib import Path


def _log_path() -> Path:
    appdata = os.environ.get("APPDATA")
    root = Path(appdata) if appdata else Path.home()
    return root / "OracleObjectChangeManager" / "error.log"


def _write_error(exc_type, exc, tb) -> None:
    path = _log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n" + time.strftime("%Y-%m-%d %H:%M:%S") + "\n")
            traceback.print_exception(exc_type, exc, tb, file=handle)
    except OSError:
        pass


def _show_error(message: str) -> None:
    print(message, file=sys.stderr)


def _install_excepthook() -> None:
    def hook(exc_type, exc, tb):
        _write_error(exc_type, exc, tb)
        _show_error(f"예기치 않은 오류가 발생했습니다.\n{exc}\n\n자세한 내용: {_log_path()}")

    sys.excepthook = hook

    def thread_hook(args):
        _write_error(args.exc_type, args.exc_value, args.exc_traceback)

    threading.excepthook = thread_hook


def main() -> None:
    root = Path(__file__).resolve().parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    _install_excepthook()
    try:
        import oracledb  # noqa: F401
    except ImportError:
        _show_error("필요한 패키지가 없습니다.\nrun.bat 을 실행해 주세요.")
        return
    from app.web_server import serve

    serve()


if __name__ == "__main__":
    main()
