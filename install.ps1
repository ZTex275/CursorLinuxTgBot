# Установка бота на Windows: venv + служба (PowerShell от администратора).
$ErrorActionPreference = "Stop"

$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServiceName = "cursor-linux-tg-bot"
$PythonCandidates = @("py -3.11", "py -3.12", "python3.11", "python3.12", "python3", "python")

function Find-Python {
    foreach ($candidate in $PythonCandidates) {
        $parts = $candidate -split " "
        try {
            $version = & $parts[0] @($parts[1..($parts.Length - 1)]) -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($LASTEXITCODE -eq 0 -and [version]$version -ge [version]"3.11") {
                return @{ Command = $candidate; Version = $version }
            }
        } catch {}
    }
    return $null
}

function Require-Admin {
    $current = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $current.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Error "Запустите PowerShell от администратора: .\install.ps1"
    }
}

Require-Admin

Write-Host "==> Репозиторий: $RepoDir"

$py = Find-Python
if (-not $py) {
    Write-Error "Нужен Python 3.11+. Установите с https://www.python.org/downloads/"
}
Write-Host "==> Python: $($py.Version) ($($py.Command))"

$venvPython = Join-Path $RepoDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    $parts = $py.Command -split " "
    & $parts[0] @($parts[1..($parts.Length - 1)]) -m venv (Join-Path $RepoDir ".venv")
}
& $venvPython -m pip install --upgrade pip -q
& $venvPython -m pip install -e $RepoDir -q

New-Item -ItemType Directory -Force -Path (Join-Path $RepoDir "data\sessions") | Out-Null

if (-not (Test-Path (Join-Path $RepoDir ".env"))) {
    if (Test-Path (Join-Path $RepoDir ".env.example")) {
        Copy-Item (Join-Path $RepoDir ".env.example") (Join-Path $RepoDir ".env")
    }
    $token = Read-Host "TELEGRAM_BOT_TOKEN"
    $cursorKey = Read-Host "CURSOR_API_KEY"
    @"
TELEGRAM_BOT_TOKEN=$token
CURSOR_API_KEY=$cursorKey
VK_BOT_TOKEN=
GITHUB_TOKEN=
"@ | Set-Content -Encoding UTF8 (Join-Path $RepoDir ".env")
}

if (-not (Test-Path (Join-Path $RepoDir "config.yaml"))) {
    $userId = Read-Host "Telegram user id (allowed_user_ids)"
    $defaultWs = $env:USERPROFILE
    $workspace = Read-Host "Workspace [$defaultWs]"
    if (-not $workspace) { $workspace = $defaultWs }
    $example = Get-Content (Join-Path $RepoDir "config.example.yaml") -Raw
    $example = $example -replace "workspace: /home/YOUR_USER", "workspace: $workspace"
    $example = $example -replace "- 123456789", "- $userId"
    $example | Set-Content -Encoding UTF8 (Join-Path $RepoDir "config.yaml")
}

$wrapper = Join-Path $RepoDir "run-bot.cmd"
@"
@echo off
cd /d "%~dp0"
for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do (
  if not "%%a"=="" set "%%a=%%b"
)
"%~dp0.venv\Scripts\python.exe" "%~dp0run.py" -c "%~dp0config.yaml"
"@ | Set-Content -Encoding ASCII $wrapper

$binPath = "cmd.exe /c `"$wrapper`""
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    if ($existing.Status -eq "Running") {
        Stop-Service -Name $ServiceName -Force
    }
    sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 2
}

New-Service -Name $ServiceName `
    -DisplayName "Cursor Telegram Bot" `
    -Description "Telegram/VK bridge to Cursor local agent (Windows)" `
    -BinaryPathName $binPath `
    -StartupType Automatic | Out-Null

Start-Service -Name $ServiceName

Write-Host ""
Write-Host "Готово. Бот запускается из $RepoDir"
Write-Host ""
Write-Host "  Конфиг:   $RepoDir\config.yaml"
Write-Host "  Секреты:  $RepoDir\.env"
Write-Host "  Сессии:   $RepoDir\data\sessions"
Write-Host "  Статус:   Get-Service $ServiceName"
Write-Host "  Логи:     Event Viewer -> Windows Logs -> Application"
Write-Host "  Обновить: .\update.ps1"
Write-Host ""
