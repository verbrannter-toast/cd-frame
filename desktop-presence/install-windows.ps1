# Windows 11: run the presence script hidden at every logon via Task Scheduler.
#
# From PowerShell, inside this folder (after creating config.toml and the venv, see the README):
#   powershell -ExecutionPolicy Bypass -File .\install-windows.ps1
#
# Remove it again with:
#   Unregister-ScheduledTask -TaskName "CD Frame Presence" -Confirm:$false
#
# The script writes its log to %LOCALAPPDATA%\cd-frame-presence\presence.log (pythonw has no console).

$ErrorActionPreference = "Stop"

$script = Join-Path $PSScriptRoot "cd_frame_presence.py"
$venvPythonw = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"

if (Test-Path $venvPythonw) {
    $pythonw = $venvPythonw
} else {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "pythonw.exe not found. Install Python 3.11+ or create the .venv first (see the README)." }
    $pythonw = $cmd.Source
}

if (-not (Test-Path (Join-Path $PSScriptRoot "config.toml"))) {
    throw "config.toml not found next to this script. Copy config.example.toml to config.toml and fill it in first."
}

$action   = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`"" -WorkingDirectory $PSScriptRoot
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName "CD Frame Presence" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Discord Rich Presence for the CD Frame" -Force | Out-Null
Start-ScheduledTask -TaskName "CD Frame Presence"

Write-Host "Installed and started. It runs at every logon; log: $env:LOCALAPPDATA\cd-frame-presence\presence.log"
