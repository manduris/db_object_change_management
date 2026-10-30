"""Load table/column metadata from CA ERwin 7.x .erwin models."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.gap_filter import GapFilter, default_gap_filter, is_main_subject_area, table_in_scope

_VBS = Path(__file__).resolve().with_name("erwin_extract.vbs")
_APPLY_VBS = Path(__file__).resolve().with_name("erwin_apply.vbs")
_CSCRIPT32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "SysWOW64" / "cscript.exe"
_spawn_lock = threading.Lock()
DEFAULT_ERWIN_MODEL = Path(r"D:\__SVN_D__\100.db\01.데이터모델\OMC-SPOP_v1.76.erwin")


@dataclass
class ModelColumn:
    logical_name: str
    physical_name: str
    datatype: str
    nullable: bool = True
    is_pk: bool = False
    domain: str = ""
    comment: str = ""

    def key(self) -> str:
        return (self.physical_name or self.logical_name).strip()


@dataclass
class ModelTable:
    logical_name: str
    physical_name: str
    comment: str = ""
    columns: list[ModelColumn] = field(default_factory=list)
    subject_areas: list[str] = field(default_factory=list)

    def key(self) -> str:
        return (self.physical_name or self.logical_name).strip()


@dataclass
class ErwinModel:
    name: str
    path: str
    tables: list[ModelTable] = field(default_factory=list)
    excluded_tables: list[ModelTable] = field(default_factory=list)

    def public(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "table_count": len(self.tables),
            "column_count": sum(len(item.columns) for item in self.tables),
            "tables": [asdict(item) for item in self.tables],
        }


def save_model_json(model: ErwinModel) -> None:
    path = Path(model.path)
    payload = {
        "name": model.name,
        "tables": [asdict(item) for item in model.tables],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_erwin_change(path: str | Path, op: dict) -> None:
    if (op.get("action") or "") == "skip":
        return
    source = Path(path)
    if source.suffix.lower() not in {".erwin", ".xml"}:
        raise ValueError("ERwin 모델 파일만 직접 수정할 수 있습니다.")
    if not _APPLY_VBS.exists():
        raise ValueError("ERwin 반영 스크립트를 찾을 수 없습니다.")
    cscript = _cscript32()
    with tempfile.TemporaryDirectory(prefix="erwin-apply-") as raw_tmp:
        tmp = Path(raw_tmp)
        working = tmp / source.name
        op_file = tmp / "op.txt"
        _copy_shared(source, working)
        op_file.write_text(_op_text(op), encoding="utf-8")
        try:
            completed = _run_cscript([str(cscript), "//nologo", str(_APPLY_VBS), str(working), str(op_file)])
        except subprocess.TimeoutExpired as exc:
            raise ValueError("ERwin 모델을 수정하는 데 시간이 너무 오래 걸렸습니다.") from exc
        except OSError as exc:
            raise ValueError("ERwin 모델 수정을 실행하지 못했습니다. " + str(exc)) from exc
        detail = (completed.stderr or completed.stdout or b"").decode("cp949", errors="replace").strip()
        if completed.returncode != 0:
            raise ValueError("ERwin 모델을 수정하지 못했습니다. " + (detail or f"종료 코드 {completed.returncode}"))
        _replace_shared(working, source)


def _op_text(op: dict) -> str:
    lines = []
    for key, value in op.items():
        text = "" if value is None else str(value).replace("\r", " ").replace("\n", " ")
        lines.append(f"{key}={text}")
    return "\n".join(lines) + "\n"


def _replace_shared(source: Path, dest: Path) -> None:
    try:
        shutil.copyfile(source, dest)
        return
    except OSError:
        pass
    data = source.read_bytes()
    handle = _open_shared_write(dest)
    try:
        _write_handle(handle, data)
    finally:
        _close_handle(handle)


def _open_shared_write(dest: Path) -> int:
    import ctypes

    generic_write = 0x40000000
    share = 0x1 | 0x2 | 0x4
    open_existing = 3
    handle = ctypes.windll.kernel32.CreateFileW(
        str(dest),
        generic_write,
        share,
        None,
        open_existing,
        0x80,
        None,
    )
    if handle == -1 or handle == 0xFFFFFFFF:
        raise ValueError(f"모델 파일을 저장할 수 없습니다. ERwin에서 닫은 뒤 다시 시도하세요. {dest}")
    return int(handle)


def _write_handle(handle: int, data: bytes) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.SetFilePointer(handle, 0, None, 0)
    written = wintypes.DWORD()
    ok = kernel32.WriteFile(handle, data, len(data), ctypes.byref(written), None)
    if not ok:
        raise ValueError("모델 파일을 저장하지 못했습니다.")
    kernel32.SetEndOfFile(handle)


def load_erwin_model(path: str | Path, settings: GapFilter | None = None) -> ErwinModel:
    source = Path(path)
    if not source.exists():
        raise ValueError(f"모델 파일을 찾을 수 없습니다. {source}")
    suffix = source.suffix.lower()
    filt = settings or default_gap_filter()
    if suffix in {".json"}:
        return _from_json(source, source.read_text(encoding="utf-8"), filt)
    if suffix not in {".erwin", ".xml"}:
        raise ValueError("ERwin 모델(.erwin) 또는 추출 JSON 파일을 선택하세요.")
    return _from_erwin(source, filt)


def fix_erwin_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    if not text:
        return ""
    try:
        return text.encode("latin-1").decode("cp949")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _from_json(source: Path, text: str, settings: GapFilter | None = None) -> ErwinModel:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("모델 JSON을 읽지 못했습니다.") from exc
    payload = _fix_tree(payload)
    if not isinstance(payload, dict):
        raise ValueError("모델 JSON 형식이 올바르지 않습니다.")
    tables = [_table_from_dict(item) for item in payload.get("tables") or [] if isinstance(item, dict)]
    kept, skipped = partition_model_tables([item for item in tables if item.key()], settings)
    name = str(payload.get("name") or source.stem)
    return ErwinModel(name=name, path=str(source), tables=kept, excluded_tables=skipped)


def partition_model_tables(
    tables: list[ModelTable],
    settings: GapFilter | None = None,
) -> tuple[list[ModelTable], list[ModelTable]]:
    filt = settings or default_gap_filter()
    kept: list[ModelTable] = []
    skipped: list[ModelTable] = []
    for item in tables:
        if _table_in_scope(item, filt):
            kept.append(item)
        else:
            skipped.append(item)
    return kept, skipped


def filter_model_tables(tables: list[ModelTable], settings: GapFilter | None = None) -> list[ModelTable]:
    kept, _skipped = partition_model_tables(tables, settings)
    return kept


def _table_in_scope(table: ModelTable, settings: GapFilter | None = None) -> bool:
    names = [table.key(), table.physical_name, table.logical_name]
    areas = [str(item).strip() for item in table.subject_areas if str(item).strip()]
    return table_in_scope(names, areas, settings)


def _from_erwin(source: Path, settings: GapFilter | None = None) -> ErwinModel:
    cscript = _cscript32()
    if not _VBS.exists():
        raise ValueError("ERwin 추출 스크립트를 찾을 수 없습니다.")
    with tempfile.TemporaryDirectory(prefix="erwin-gap-") as raw_tmp:
        tmp = Path(raw_tmp)
        working = tmp / source.name
        _copy_shared(source, working)
        out = tmp / "model.json"
        try:
            completed = _run_cscript([str(cscript), "//nologo", str(_VBS), str(working), str(out)])
        except subprocess.TimeoutExpired as exc:
            raise ValueError("ERwin 모델을 읽는 데 시간이 너무 오래 걸렸습니다.") from exc
        except OSError as exc:
            raise ValueError("ERwin 추출을 실행하지 못했습니다. " + str(exc)) from exc
        if completed.returncode != 0 or not out.exists():
            detail = (completed.stderr or completed.stdout or b"").decode("cp949", errors="replace").strip()
            hint = detail or f"종료 코드 {completed.returncode}"
            raise ValueError(
                "ERwin 모델을 읽지 못했습니다. AllFusion ERwin 7.x가 설치되어 있는지 확인하세요. "
                + hint
            )
        text = out.read_text(encoding="utf-16")
    model = _from_json(source, text, settings)
    model.path = str(source)
    if not model.tables:
        raise ValueError("모델에서 테이블을 찾지 못했습니다.")
    return model


def _run_cscript(command: list[str]) -> subprocess.CompletedProcess:
    """32-bit ERwin COM fails if this process set a 64-bit Instant Client DLL directory."""
    import ctypes

    from app.oracle_client import dll_directory_lock, find_client_dir

    client = find_client_dir()
    kernel32 = ctypes.windll.kernel32
    env = os.environ.copy()
    if client:
        needle = str(client).casefold()
        env["PATH"] = os.pathsep.join(
            item for item in env.get("PATH", "").split(os.pathsep) if needle not in item.casefold()
        )
    with dll_directory_lock:
        with _spawn_lock:
            kernel32.SetDllDirectoryW(None)
            try:
                return subprocess.run(
                    command,
                    capture_output=True,
                    timeout=180,
                    check=False,
                    env=env,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            finally:
                if client is not None:
                    kernel32.SetDllDirectoryW(str(client))


def _cscript32() -> Path:
    if _CSCRIPT32.exists():
        return _CSCRIPT32
    raise ValueError(
        "32비트 cscript(SysWOW64)를 찾을 수 없습니다. ERwin 7.x COM 추출에는 32비트 Windows 구성 요소가 필요합니다."
    )


def _copy_shared(source: Path, dest: Path) -> None:
    try:
        shutil.copyfile(source, dest)
        return
    except OSError:
        pass
    handle = _open_shared_read(source)
    try:
        dest.write_bytes(_read_handle(handle))
    finally:
        _close_handle(handle)


def _open_shared_read(source: Path) -> int:
    import ctypes

    generic_read = 0x80000000
    share = 0x1 | 0x2 | 0x4  # read/write/delete
    open_existing = 3
    handle = ctypes.windll.kernel32.CreateFileW(
        str(source),
        generic_read,
        share,
        None,
        open_existing,
        0x80,
        None,
    )
    if handle == -1 or handle == 0xFFFFFFFF:
        raise ValueError(f"모델 파일을 열 수 없습니다. 다른 프로그램에서 사용 중일 수 있습니다. {source}")
    return int(handle)


def _read_handle(handle: int) -> bytes:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    size_high = wintypes.DWORD()
    size_low = kernel32.GetFileSize(handle, ctypes.byref(size_high))
    size = (int(size_high.value) << 32) | int(size_low)
    if size < 0 or size > 200_000_000:
        raise ValueError("모델 파일이 너무 큽니다.")
    buf = ctypes.create_string_buffer(size)
    read = wintypes.DWORD()
    ok = kernel32.ReadFile(handle, buf, size, ctypes.byref(read), None)
    if not ok:
        raise ValueError("모델 파일을 읽지 못했습니다.")
    return buf.raw[: int(read.value)]


def _close_handle(handle: int) -> None:
    import ctypes

    ctypes.windll.kernel32.CloseHandle(handle)


def _fix_tree(value: object) -> object:
    if isinstance(value, str):
        return fix_erwin_text(value)
    if isinstance(value, list):
        return [_fix_tree(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _fix_tree(item) for key, item in value.items()}
    return value


def _table_from_dict(raw: dict) -> ModelTable:
    columns = []
    seen: dict[str, ModelColumn] = {}
    for item in raw.get("columns") or []:
        if not isinstance(item, dict):
            continue
        physical = str(item.get("physical_name") or item.get("logical_name") or "").strip()
        logical = str(item.get("logical_name") or physical).strip()
        if not physical and not logical:
            continue
        fold = (physical or logical).casefold()
        existing = seen.get(fold)
        if existing is not None:
            if bool(item.get("is_pk")):
                existing.is_pk = True
            continue
        column = ModelColumn(
            logical_name=logical,
            physical_name=physical or logical,
            datatype=str(item.get("datatype") or "").strip(),
            nullable=_nullable(item.get("null_option"), item.get("nullable")),
            is_pk=bool(item.get("is_pk")),
            domain=str(item.get("domain") or "").strip(),
            comment=str(item.get("comment") or "").strip(),
        )
        seen[fold] = column
        columns.append(column)
    physical = str(raw.get("physical_name") or raw.get("logical_name") or "").strip()
    logical = str(raw.get("logical_name") or physical).strip()
    areas = []
    for item in raw.get("subject_areas") or []:
        text = str(item).strip()
        if text and text not in areas:
            areas.append(text)
    return ModelTable(
        logical_name=logical,
        physical_name=physical or logical,
        comment=str(raw.get("comment") or "").strip(),
        columns=columns,
        subject_areas=areas,
    )


def _nullable(null_option: object, nullable: object) -> bool:
    if isinstance(nullable, bool):
        return nullable
    text = str(null_option or "").strip().upper().replace(" ", "")
    if text in {"NOTNULL", "NOTNULLOPTION", "IDENTITY"}:
        return False
    if text in {"NULL", "NULLOPTION", ""}:
        return True
    return True
