<#
.SYNOPSIS
    Запуск claude-remote-ops: venv, зависимости, проверка .env, автоперезапуск при сбое.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\start_bot.ps1
    .\start_bot.ps1 -Reinstall      # переустановить зависимости
    .\start_bot.ps1 -NoRestart      # не перезапускать после падения
#>
[CmdletBinding()]
param(
    [switch]$Reinstall,
    [switch]$NoRestart,
    [int]$RestartDelaySec = 10
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# UTF-8 для консоли и дочерних процессов (иначе cp866/cp1251 ломают кириллицу)
try { chcp.com 65001 > $null } catch { }
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Find-Python {
    $candidates = @(
        @("py", "-3.14"), @("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"),
        @("python"), @("python3")
    )
    foreach ($cand in $candidates) {
        $exe = $cand[0]
        $extra = @($cand | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $ver = & $exe @extra -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or -not $ver) { continue }
        $parts = $ver.Trim().Split(".")
        if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11) {
            return @{ Exe = $exe; Args = $extra; Version = $ver.Trim() }
        }
    }
    return $null
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    $py = Find-Python
    if (-not $py) {
        Write-Host "[X] Не найден Python 3.11+. Установите с python.org (галочка 'Add to PATH')." -ForegroundColor Red
        exit 1
    }
    Write-Host "[*] Создаю .venv (Python $($py.Version))..." -ForegroundColor Cyan
    & $py.Exe @($py.Args) -m venv .venv
    if ($LASTEXITCODE -ne 0) { Write-Host "[X] Не удалось создать venv" -ForegroundColor Red; exit 1 }
    $Reinstall = $true
}

# Активация venv в текущей сессии PowerShell
$activate = Join-Path $PSScriptRoot ".venv\Scripts\Activate.ps1"
if (Test-Path -LiteralPath $activate) { . $activate }

# Переустановка зависимостей, если изменился requirements.txt
$reqFile = Join-Path $PSScriptRoot "requirements.txt"
$hashFile = Join-Path $PSScriptRoot ".venv\.requirements.sha256"
$reqHash = (Get-FileHash -LiteralPath $reqFile -Algorithm SHA256).Hash
$oldHash = if (Test-Path -LiteralPath $hashFile) { (Get-Content -LiteralPath $hashFile -Raw).Trim() } else { "" }
if ($Reinstall -or $reqHash -ne $oldHash) {
    Write-Host "[*] Устанавливаю зависимости..." -ForegroundColor Cyan
    & $venvPython -m pip install --disable-pip-version-check -q --upgrade pip
    & $venvPython -m pip install --disable-pip-version-check -q -r $reqFile
    if ($LASTEXITCODE -ne 0) { Write-Host "[X] pip install завершился с ошибкой" -ForegroundColor Red; exit 1 }
    Set-Content -LiteralPath $hashFile -Value $reqHash -Encoding ascii
}

# Проверка .env
$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot ".env.example") -Destination $envFile
    Write-Host "[!] Создан .env из .env.example." -ForegroundColor Yellow
    Write-Host "    Впишите BOT_TOKEN (@BotFather), ADMIN_USER_ID (@userinfobot) и ALLOWED_DIRS, затем запустите снова." -ForegroundColor Yellow
    Start-Process notepad.exe $envFile
    exit 1
}

# Проверка Claude CLI
$claude = Get-Command claude -ErrorAction SilentlyContinue
if ($claude) {
    Write-Host "[OK] Claude CLI: $($claude.Source)" -ForegroundColor Green
} else {
    Write-Host "[!] 'claude' не найден в PATH. Укажите полный путь в CLAUDE_BINARY (.env)." -ForegroundColor Yellow
}

# Если скрипт запущен из терминала Claude Code, не передаем боту переменные родительской сессии
foreach ($name in @("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION")) {
    Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
}

while ($true) {
    Write-Host "[>] Запуск бота ($(Get-Date -Format 'HH:mm:ss')). Остановка: Ctrl+C" -ForegroundColor Green
    & $venvPython (Join-Path $PSScriptRoot "main.py")
    $code = $LASTEXITCODE
    if ($code -eq 0 -or $code -eq 2 -or $NoRestart) {
        if ($code -eq 2) { Write-Host "[X] Исправьте .env (см. сообщения выше)" -ForegroundColor Red }
        exit $code
    }
    Write-Host "[!] Бот завершился с кодом $code, перезапуск через $RestartDelaySec с..." -ForegroundColor Yellow
    Start-Sleep -Seconds $RestartDelaySec
}
