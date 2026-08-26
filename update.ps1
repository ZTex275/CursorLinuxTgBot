# Обновление зависимостей в .venv и перезапуск службы Windows.
$ErrorActionPreference = "Stop"

$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServiceName = "cursor-linux-tg-bot"
$venvPython = Join-Path $RepoDir ".venv\Scripts\python.exe"
$venvPip = Join-Path $RepoDir ".venv\Scripts\pip.exe"

if (-not (Test-Path $venvPython)) {
    Write-Error "Сначала: .\install.ps1"
}

Write-Host "==> Обновление $RepoDir"
& $venvPip install --upgrade pip -q
& $venvPip install -e $RepoDir -q
New-Item -ItemType Directory -Force -Path (Join-Path $RepoDir "data\sessions") | Out-Null

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service) {
    Restart-Service -Name $ServiceName -Force
    Write-Host "==> Служба перезапущена"
    Get-Service -Name $ServiceName | Format-Table -AutoSize
} else {
    Write-Host "==> Служба не установлена. Запуск: .\install.ps1"
}

Write-Host "Готово."
