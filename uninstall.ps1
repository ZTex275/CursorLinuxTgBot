# Удаление службы Windows (PowerShell от администратора).
$ErrorActionPreference = "Stop"

$RepoDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServiceName = "cursor-linux-tg-bot"

function Require-Admin {
    $current = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $current.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Error "Запустите PowerShell от администратора: .\uninstall.ps1"
    }
}

Require-Admin

Write-Host "==> Репозиторий: $RepoDir"

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service) {
    if ($service.Status -eq "Running") {
        Stop-Service -Name $ServiceName -Force
    }
    sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 2
    Write-Host "==> Служба $ServiceName удалена."
} else {
    Write-Host "==> Служба $ServiceName не найдена."
}

$wrapper = Join-Path $RepoDir "run-bot.cmd"
if (Test-Path $wrapper) {
    $ans = Read-Host "Удалить run-bot.cmd? [y/N]"
    if ($ans -eq "y" -or $ans -eq "Y") {
        Remove-Item $wrapper -Force
        Write-Host "==> run-bot.cmd удалён."
    }
}

Write-Host ""
Write-Host "Репозиторий $RepoDir не тронут (.env, config.yaml, data/ остаются)."

$ans = Read-Host "Удалить виртуальное окружение .venv? [y/N]"
if ($ans -eq "y" -or $ans -eq "Y") {
    $venv = Join-Path $RepoDir ".venv"
    if (Test-Path $venv) {
        Remove-Item $venv -Recurse -Force
        Write-Host "==> .venv удалён."
    }
}

Write-Host ""
Write-Host "Удаление завершено."
