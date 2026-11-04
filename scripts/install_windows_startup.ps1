<#
.SYNOPSIS
  Start Signal automatically when you log in to Windows (Task Scheduler).

.DESCRIPTION
  Registers a per-user scheduled task named "Signal News" that runs
  `uv run signal serve` from the project folder, hidden, one minute after logon.
  Output goes to logs\startup.log. The task restarts Signal if it crashes.

  Run from the project folder in PowerShell:
      powershell -ExecutionPolicy Bypass -File .\scripts\install_windows_startup.ps1

  Remove it again:
      powershell -ExecutionPolicy Bypass -File .\scripts\install_windows_startup.ps1 -Uninstall

.PARAMETER Uninstall
  Remove the scheduled task.

.PARAMETER TaskName
  Name of the scheduled task (default "Signal News").
#>
param(
    [switch]$Uninstall,
    [string]$TaskName = "Signal News"
)

$ErrorActionPreference = "Stop"

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName' found."
    }
    exit 0
}

$ProjectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not (Test-Path (Join-Path $ProjectDir "pyproject.toml"))) {
    throw "Could not find pyproject.toml in $ProjectDir. Keep this script in the project's scripts folder."
}

$uvCmd = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCmd) {
    throw "uv was not found on PATH. Install it first: powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`""
}
$Uv = $uvCmd.Source

$LogDir = Join-Path $ProjectDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir "startup.log"

$command = "Set-Location -LiteralPath '$ProjectDir'; & '$Uv' run signal serve *>> '$LogFile'"
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$command`"" `
    -WorkingDirectory $ProjectDir

$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = "PT1M"   # give the network a minute to come up after logon

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Signal news collector and daily digest (web UI at http://127.0.0.1:8765)" `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName'."
Write-Host "Signal will start one minute after you log in. Log: $LogFile"
Write-Host "Start it now with:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Then open http://127.0.0.1:8765"
