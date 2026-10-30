"""Native Windows file dialog for local desktop use."""

from __future__ import annotations

import subprocess
from pathlib import Path


def browse_erwin_path(initial: str = "") -> str | None:
    start = _initial_dir(initial).replace("'", "''")
    file_name = _initial_file(initial).replace("'", "''")
    script = f"""
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = 'ERwin 모델 선택'
$dialog.Filter = 'ERwin 모델 (*.erwin)|*.erwin|추출 JSON (*.json)|*.json|모든 파일 (*.*)|*.*'
$dialog.CheckFileExists = $true
$dialog.Multiselect = $false
$start = '{start}'
if ($start -and (Test-Path -LiteralPath $start)) {{ $dialog.InitialDirectory = $start }}
$file = '{file_name}'
if ($file) {{ $dialog.FileName = $file }}
$dialog.ShowHelp = $false
[System.Windows.Forms.Application]::EnableVisualStyles()
$result = $dialog.ShowDialog()
if ($result -ne [System.Windows.Forms.DialogResult]::OK) {{ exit 1 }}
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
[Console]::Out.Write($dialog.FileName)
"""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", script],
            capture_output=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("파일 찾기 창을 열지 못했습니다. 경로를 직접 입력하세요.") from exc
    if completed.returncode != 0:
        return None
    path = completed.stdout.decode("utf-8", errors="replace").strip()
    return path or None


def _initial_dir(initial: str) -> str:
    if not initial:
        return r"D:\__SVN_D__\100.db\01.데이터모델"
    path = Path(initial)
    if path.is_dir():
        return str(path)
    if path.parent.exists():
        return str(path.parent)
    return r"D:\__SVN_D__\100.db\01.데이터모델"


def _initial_file(initial: str) -> str:
    if not initial:
        return ""
    path = Path(initial)
    if path.is_dir():
        return ""
    return path.name
