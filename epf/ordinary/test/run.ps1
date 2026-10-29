# Автотест обработок эмулятора ЧЗ на обычных формах в тестовой базе (толстый клиент, обычное приложение).
# Нужно: запущенный эмулятор, база настроена под него (сертификат, правка "без подписи"), собранные ..\..\ЭЧЗ_*_ОФ.epf.
# Платформа спросит разрешение открыть внешние обработки ("Предупреждение безопасности") - ответить "Да".
#
#   powershell -ExecutionPolicy Bypass -File run.ps1 -Base "D:\DataBases\file_upp" [-Subdivision "Первый этаж"] [-Build]
param(
    [Parameter(Mandatory = $true)][string]$Base,
    [string]$Subdivision = "Первый этаж",
    [string]$Platform    = "",
    [int]   $TimeoutSec  = 600,
    [switch]$Build
)
$ErrorActionPreference = "Stop"
$here = $PSScriptRoot
$epfDir = (Resolve-Path (Join-Path $here "..\..")).Path
if (-not $Platform) {
    $Platform = Get-ChildItem "C:\Program Files\1cv8" -Directory | Where-Object { $_.Name -match '^\d+(\.\d+){3}$' } |
        Sort-Object { [version]$_.Name } -Descending | Select-Object -First 1 -ExpandProperty Name
}
$exe = "C:\Program Files\1cv8\$Platform\bin\1cv8.exe"
$testEpf = Join-Path $here "build\ЭЧЗ_АвтотестОФ.epf"
New-Item -ItemType Directory -Force (Join-Path $here "build") | Out-Null

if ($Build -or -not (Test-Path $testEpf)) {
    $log = Join-Path $here "build\build.log"
    $p = Start-Process $exe -ArgumentList "DESIGNER /F`"$Base`" /DisableStartupDialogs /LoadExternalDataProcessorOrReportFromFiles `"$here\src\ЭЧЗ_АвтотестОФ.xml`" `"$testEpf`" /Out `"$log`"" -Wait -PassThru
    if ($p.ExitCode -ne 0 -or -not (Test-Path $testEpf)) { Get-Content $log; throw "Не собрана тестовая обработка" }
}

$result = Join-Path $here "build\result.txt"
if (Test-Path $result) { Remove-Item $result -Force }
$param = "$result|$epfDir|$Subdivision"
$p = Start-Process $exe -ArgumentList "ENTERPRISE /F`"$Base`" /RunModeOrdinaryApplication /Execute`"$testEpf`" /C`"$param`" /DisableStartupMessages" -PassThru
if (-not $p.WaitForExit($TimeoutSec * 1000)) { Stop-Process -Id $p.Id -Force; throw "1С не завершилась за $TimeoutSec с" }
if (-not (Test-Path $result)) { throw "Файл результата не создан" }
Get-Content $result -Encoding UTF8
if ((Get-Content $result -Encoding UTF8 -Raw) -match "ошибок 0") { exit 0 } else { exit 1 }
