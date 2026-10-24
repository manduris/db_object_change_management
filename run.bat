@echo off
setlocal
cd /d "%~dp0"

if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
  set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
) else if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
  set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
) else if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
  set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
) else (
  set "PY=python"
)

if not exist ".venv\Scripts\python.exe" (
  "%PY%" -m venv .venv
  if errorlevel 1 (
    echo Failed to create a virtual environment.
    echo Install Python 3.12 and run this file again.
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Failed to install required packages.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" "%~dp0main.py"
