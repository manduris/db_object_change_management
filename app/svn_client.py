"""Run svn update/commit against a working copy without prompting."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_TORTOISE = (
    Path(r"C:\Program Files\TortoiseSVN\bin\svn.exe"),
    Path(r"C:\Program Files (x86)\TortoiseSVN\bin\svn.exe"),
)


@dataclass
class SvnResult:
    ok: bool
    action: str
    output: str = ""
    error: str = ""

    def log_lines(self) -> list[str]:
        title = f"SVN {self.action} {'성공' if self.ok else '실패'}"
        body = (self.output or self.error).strip()
        if not body:
            return [title]
        lines = [title]
        lines.extend(line.rstrip() for line in body.splitlines() if line.strip())
        return lines


def is_svn_working_copy(path: Path) -> bool:
    current = path.resolve()
    if current.is_file():
        current = current.parent
    for folder in [current, *current.parents]:
        if (folder / ".svn").is_dir():
            return True
    return False


def svn_update(path: Path) -> SvnResult | None:
    return _svn(["update", ".", "--non-interactive"], path, action="업데이트")


def svn_commit(path: Path, message: str) -> SvnResult | None:
    if not is_svn_working_copy(path):
        return None
    text = (message or "").strip() or "변경이력"
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".txt")
    try:
        handle.write(text)
        handle.close()
        return _svn(
            ["commit", ".", "-F", handle.name, "--encoding", "UTF-8", "--non-interactive"],
            path,
            action="커밋",
        )
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def _svn(args: list[str], path: Path, *, action: str) -> SvnResult | None:
    folder = path.resolve()
    if folder.is_file():
        folder = folder.parent
    folder.mkdir(parents=True, exist_ok=True)
    if not is_svn_working_copy(folder):
        return None
    exe = _svn_exe()
    if not exe:
        return SvnResult(
            ok=False,
            action=action,
            error="SVN 명령을 찾을 수 없습니다. TortoiseSVN 명령줄 클라이언트(svn.exe)를 설치하세요.",
        )
    try:
        completed = subprocess.run(
            [exe, *args],
            cwd=str(folder),
            capture_output=True,
            timeout=180,
            **_no_window(),
        )
    except subprocess.TimeoutExpired:
        return SvnResult(ok=False, action=action, error=f"SVN {action} 시간이 초과되었습니다.")
    except OSError as exc:
        return SvnResult(ok=False, action=action, error=f"SVN {action}을 실행하지 못했습니다. {exc}")
    output = "\n".join(part for part in (_decode(completed.stdout), _decode(completed.stderr)) if part).strip()
    if completed.returncode == 0:
        return SvnResult(ok=True, action=action, output=output or "완료했습니다.")
    detail = output or f"종료 코드 {completed.returncode}"
    return SvnResult(
        ok=False,
        action=action,
        output=output,
        error=f"SVN {action}에 실패했습니다. {detail}",
    )


def _svn_exe() -> str | None:
    found = shutil.which("svn")
    if found:
        return found
    for candidate in _TORTOISE:
        if candidate.is_file():
            return str(candidate)
    return None


def _decode(raw: bytes) -> str:
    if not raw:
        return ""
    for encoding in ("utf-8", "cp949"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _no_window() -> dict:
    if os.name != "nt":
        return {}
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return {"creationflags": flag}
