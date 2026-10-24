@echo off
chcp 65001 >nul
echo Starting VideoCut...

REM 优先使用本工程 .venv，否则用 PATH 里的 python
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

echo Using interpreter: %PY%
%PY% funclip/launch.py --home slirn %*
