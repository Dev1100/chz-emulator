# Автотест ЭЧЗ_АвтоПодключение_ОФ в клиент-серверной базе (толстый клиент, обычное приложение).
# Нужно: эмулятор запущен через start_lan.bat (--bind 0.0.0.0), сертификат доверен, правка "без подписи" внесена,
# учет ИС МП и тестовый контур уже включены, собран ..\..\ЭЧЗ_АвтоПодключение_ОФ.epf.
# Платформа спросит разрешение открыть внешние обработки ("Предупреждение безопасности") - ответить "Да".
#
#   powershell -ExecutionPolicy Bypass -File run_auto.ps1 -Server hogwarts -Ref trade_10_f -User "Администратор" [-Build]
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [Parameter(Mandatory = $true)][string]$Ref,
    [string]$User       = "",
    [string]$Password   = "",
    [string]$Platform   = "",
    [int]   $TimeoutSec = 420,
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
$ib = "/S`"$Server/$Ref`""
if ($User) { $ib += " /N`"$User`"" }
if ($Password) { $ib += " /P`"$Password`"" }
$testEpf = Join-Path $here "build\ЭЧЗ_АвтотестПодключенияОФ.epf"
New-Item -ItemType Directory -Force (Join-Path $here "build") | Out-Null

if ($Build -or -not (Test-Path $testEpf)) {
    $log = Join-Path $here "build\build_auto.log"
    $p = Start-Process $exe -ArgumentList "DESIGNER $ib /DisableStartupDialogs /LoadExternalDataProcessorOrReportFromFiles `"$here\src\ЭЧЗ_АвтотестПодключенияОФ.xml`" `"$testEpf`" /Out `"$log`"" -Wait -PassThru
    if ($p.ExitCode -ne 0 -or -not (Test-Path $testEpf)) { Get-Content $log; throw "Не собрана тестовая обработка" }
}

# Переменная HTTP_PROXY (VPN) перехватывает и прямые запросы 1С к эмулятору: компьютер эмулятора - в исключения.
$env:NO_PROXY = (@($env:NO_PROXY, "localhost,127.0.0.1,::1,$Server") | Where-Object { $_ }) -join ","

$result = Join-Path $here "build\result_auto.txt"
if (Test-Path $result) { Remove-Item $result -Force }
$param = "$result|$epfDir|$Server"
$p = Start-Process $exe -ArgumentList "ENTERPRISE $ib /RunModeOrdinaryApplication /Execute`"$testEpf`" /C`"$param`" /DisableStartupMessages /DisableStartupDialogs" -PassThru
if (-not $p.WaitForExit($TimeoutSec * 1000)) { Stop-Process -Id $p.Id -Force; throw "1С не завершилась за $TimeoutSec с (окно подтверждения на экране?)" }
if (-not (Test-Path $result)) { throw "Файл результата не создан" }
Get-Content $result -Encoding UTF8
if ((Get-Content $result -Encoding UTF8 -Raw) -match "ошибок 0") { exit 0 } else { exit 1 }
