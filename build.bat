@echo off
rem Build standalone app: release\chz-emulator.exe + instruction + 1C extension (needs: python -m pip install --user pyinstaller cryptography)
cd /d "%~dp0"
python -m PyInstaller --noconfirm --onefile --console --name chz-emulator --distpath build\dist --workpath build\work --specpath build ^
  --add-data "../ui.html;." --add-data "../extension/ЧЗ_БезПодписи.cfe;extension" --hidden-import scenario --paths . chz_emulator.py || exit /b 1
if not exist release mkdir release
copy /y build\dist\chz-emulator.exe release\ >nul
copy /y "ИНСТРУКЦИЯ.md" release\ >nul
copy /y "extension\ЧЗ_БезПодписи.cfe" release\ >nul
powershell -NoProfile -Command "Compress-Archive -Force -Path release\* -DestinationPath release\chz-emulator.zip"
echo Done: release\
