# Enable or disable Start with Windows for Grok Bot Pace.
#   .\install-startup.ps1
#   .\install-startup.ps1 -Remove

param([switch]$Remove)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$shortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\Grok Bot Pace.lnk"

if ($Remove) {
    if (Test-Path $shortcut) { Remove-Item $shortcut }
    Write-Host "Removed startup shortcut."
    exit 0
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$venvPythonW = Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $venvPython)) {
    python -m venv .venv
}
& $venvPython -m pip install -r requirements.txt | Out-Null

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($shortcut)
$lnk.TargetPath = $venvPythonW
$lnk.Arguments = "-m grok_bot_pace"
$lnk.WorkingDirectory = $PSScriptRoot
$lnk.WindowStyle = 7
$lnk.Description = "Grok Bot weekly pace"
$lnk.Save()
Write-Host "Startup shortcut written to:"
Write-Host "  $shortcut"
