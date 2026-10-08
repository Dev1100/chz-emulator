@echo off
rem CHZ emulator: proxy for 1C and web UI at http://127.0.0.1:3128/
cd /d "%~dp0"
start "" http://127.0.0.1:3128/
python chz_emulator.py --port 3128 %*
