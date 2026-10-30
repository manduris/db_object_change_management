"""Locate a 64-bit Oracle Client 19+ and enable python-oracledb thick mode."""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

_DLL_VERSION_RE = re.compile(
    r"^ora(?:ociei|ociee|clientic|client)(\d+)",
    re.IGNORECASE,
)
_lock = threading.Lock()
dll_directory_lock = _lock
_thick_ready = False
_thick_error = ""
_thick_dir: Path | None = None


def client_search_roots() -> list[Path]:
    roots: list[Path] = []
    for key in ("OCM_ORACLE_CLIENT", "ORACLE_CLIENT_LIB"):
        raw = os.environ.get(key)
        if raw:
            roots.append(Path(raw))
    app_root = Path(__file__).resolve().parent.parent
    roots.append(app_root / "instantclient")
    roots.extend(sorted(app_root.glob("instantclient*")))
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "OracleObjectChangeManager" / "instantclient")
    for pattern in (
        r"C:\oracle\instantclient*",
        r"D:\oracle\instantclient*",
        r"C:\instantclient*",
        r"D:\instantclient*",
    ):
        roots.extend(Path(pattern).parent.glob(Path(pattern).name))
    home = os.environ.get("ORACLE_HOME")
    if home:
        roots.append(Path(home))
        roots.append(Path(home) / "bin")
    for item in os.environ.get("PATH", "").split(os.pathsep):
        if item:
            roots.append(Path(item))
    return roots


def find_client_dir() -> Path | None:
    seen: set[Path] = set()
    for raw in client_search_roots():
        for candidate in _expand_candidate(raw):
            resolved = candidate.resolve() if candidate.exists() else candidate
            if resolved in seen:
                continue
            seen.add(resolved)
            if usable_client_dir(candidate):
                return _lib_dir(candidate)
    return None


def usable_client_dir(directory: Path) -> bool:
    oci = _oci_dll(directory)
    if oci is None:
        return False
    if not _is_pe64(oci):
        return False
    major = client_major(directory)
    return major is None or major >= 19


def client_major(directory: Path) -> int | None:
    folder = _lib_dir(directory)
    if not folder.is_dir():
        return None
    found: list[int] = []
    for item in folder.iterdir():
        matched = _DLL_VERSION_RE.match(item.name)
        if matched:
            found.append(int(matched.group(1)))
    if not found:
        return None
    return max(found)


def ensure_thick_mode() -> Path:
    """Load Oracle Client libraries. Raises RuntimeError if none can be used."""
    global _thick_ready, _thick_error, _thick_dir
    with _lock:
        if _thick_ready:
            return _thick_dir or Path()
        if _thick_error:
            raise RuntimeError(_thick_error)
        oracledb = _require_oracledb()
        if not oracledb.is_thin_mode():
            _thick_ready = True
            _thick_dir = find_client_dir()
            return _thick_dir or Path()
        found = find_client_dir()
        if found is None:
            _thick_error = missing_client_message()
            raise RuntimeError(_thick_error)
        _apply_nls_lang()
        try:
            oracledb.init_oracle_client(lib_dir=str(found))
        except Exception as exc:
            _thick_error = (
                "64비트 Oracle Instant Client 19 이상을 읽지 못했습니다. "
                f"{found} 의 클라이언트를 확인하세요.\n{exc}"
            )
            raise RuntimeError(_thick_error) from exc
        _thick_ready = True
        _thick_dir = found
        return found


def _apply_nls_lang() -> None:
    os.environ["NLS_LANG"] = os.environ.get("OCM_NLS_LANG") or "AMERICAN_AMERICA.AL32UTF8"


def missing_client_message() -> str:
    return (
        "이 데이터베이스는 추가 Oracle Client가 있어야 접속할 수 있습니다. "
        "지금 PC에는 32비트 Oracle 11.2 클라이언트만 있어 64비트 Python에서 사용할 수 없습니다. "
        "Oracle Instant Client 19 이상(Windows 64-bit Basic 또는 Basic Light)을 받은 뒤 "
        "OCM_ORACLE_CLIENT 환경 변수에 폴더를 지정하거나, "
        "프로그램 폴더의 instantclient 또는 "
        "%APPDATA%\\OracleObjectChangeManager\\instantclient 에 압축을 푸세요."
    )


def _expand_candidate(path: Path) -> list[Path]:
    if path.exists():
        return [path]
    if "*" in path.name:
        return list(path.parent.glob(path.name))
    return []


def _lib_dir(directory: Path) -> Path:
    if (directory / "oci.dll").is_file():
        return directory
    nested = directory / "bin"
    if (nested / "oci.dll").is_file():
        return nested
    if directory.is_dir():
        for child in sorted(directory.iterdir()):
            if child.is_dir() and (child / "oci.dll").is_file():
                return child
    return directory


def _oci_dll(directory: Path) -> Path | None:
    folder = _lib_dir(directory)
    oci = folder / "oci.dll"
    return oci if oci.is_file() else None


def _is_pe64(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            if handle.read(2) != b"MZ":
                return False
            handle.seek(0x3C)
            pe_offset = int.from_bytes(handle.read(4), "little")
            handle.seek(pe_offset)
            if handle.read(4) != b"PE\x00\x00":
                return False
            machine = int.from_bytes(handle.read(2), "little")
    except OSError:
        return False
    return machine == 0x8664


def _require_oracledb():
    try:
        import oracledb
    except ImportError as exc:
        raise RuntimeError(
            "oracledb 패키지가 없습니다. run.bat 으로 프로그램을 실행해 주세요."
        ) from exc
    return oracledb
