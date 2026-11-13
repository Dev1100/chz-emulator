@echo off
rem CHZ emulator for 1C clients in the local network: listens on all interfaces, port 3128.
rem Windows Firewall must allow inbound TCP 3128 (run once as administrator):
rem   netsh advfirewall firewall add rule name="chz-emulator 3128" dir=in action=allow protocol=TCP localport=3128
cd /d "%~dp0"
start "" http://127.0.0.1:3128/
python chz_emulator.py --port 3128 --bind 0.0.0.0 --no-browser %*
