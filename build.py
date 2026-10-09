"""Сборка отдельного приложения: release/chz-emulator.exe + инструкция + расширение 1С + zip.
Нужно: python -m pip install --user pyinstaller cryptography.   Запуск: python build.py"""
import os, shutil, subprocess, sys, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
CFE = os.path.join('extension', 'ЧЗ_БезПодписи.cfe')
os.chdir(HERE)
subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--console', '--name', 'chz-emulator',
                '--distpath', 'build/dist', '--workpath', 'build/work', '--specpath', 'build',
                '--add-data', f'../ui.html{os.pathsep}.', '--add-data', f'../{CFE}{os.pathsep}extension',
                '--add-data', f'../epf/ЭЧЗ_НастройкаПодключения.epf{os.pathsep}epf',
                '--add-data', f'../epf/ЭЧЗ_ВыгрузкаВНК.epf{os.pathsep}epf',
                '--hidden-import', 'scenario', '--paths', '.', 'chz_emulator.py'], check=True)
os.makedirs('release', exist_ok=True)
files = {'chz-emulator.exe': 'build/dist/chz-emulator.exe', 'ИНСТРУКЦИЯ.md': 'ИНСТРУКЦИЯ.md', 'ИНСТРУКЦИЯ_НЕ_КА.md': 'ИНСТРУКЦИЯ_НЕ_КА.md',
         'ЧЗ_БезПодписи.cfe': CFE, 'CHZ_BezPodpisi.cfe': CFE,
         'ЭЧЗ_НастройкаПодключения.epf': 'epf/ЭЧЗ_НастройкаПодключения.epf', 'CHZ_Setup.epf': 'epf/ЭЧЗ_НастройкаПодключения.epf',
         'ЭЧЗ_ВыгрузкаВНК.epf': 'epf/ЭЧЗ_ВыгрузкаВНК.epf', 'CHZ_ExportNK.epf': 'epf/ЭЧЗ_ВыгрузкаВНК.epf'}   # латинская копия — для релиза GitHub
for name, src in files.items():
    shutil.copyfile(src, os.path.join('release', name))
with zipfile.ZipFile('release/chz-emulator.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    for name in ('chz-emulator.exe', 'ИНСТРУКЦИЯ.md', 'ИНСТРУКЦИЯ_НЕ_КА.md', 'ЧЗ_БезПодписи.cfe', 'ЭЧЗ_НастройкаПодключения.epf', 'ЭЧЗ_ВыгрузкаВНК.epf'):
        z.write(os.path.join('release', name), name)
print('Готово: release/')
