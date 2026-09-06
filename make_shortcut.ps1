<#
    Creates a "Hourglass Timer" shortcut on the desktop that launches the app
    with a double-click. Run it once, after setting up the virtual environment:

        powershell -ExecutionPolicy Bypass -File make_shortcut.ps1

    The shortcut points at pythonw.exe directly rather than at run.bat, so no
    console window flashes on launch.
#>

$ErrorActionPreference = 'Stop'

$root   = $PSScriptRoot
if (-not $root) { $root = Split-Path -Parent $MyInvocation.MyCommand.Definition }
$script = Join-Path $root 'hourglass_timer.py'
$venvw  = Join-Path $root '.venv\Scripts\pythonw.exe'

if (Test-Path $venvw) {
    $target = $venvw
} else {
    $onPath = Get-Command pythonw -ErrorAction SilentlyContinue
    if ($null -eq $onPath) {
        Write-Error "pythonw.exe not found. Create the virtual environment first (see the Setup section of README.md)."
        exit 1
    }
    $target = $onPath.Source
    Write-Warning "No .venv here; the shortcut will use $target and needs numpy installed there."
}

$link = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Hourglass Timer.lnk'

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($link)
$lnk.TargetPath       = $target
$lnk.Arguments        = '"' + $script + '"'
$lnk.WorkingDirectory = $root
$lnk.Description      = 'Hourglass countdown timer'
$lnk.WindowStyle      = 1

$icon = Join-Path $root 'hourglass.ico'
if (Test-Path $icon) { $lnk.IconLocation = $icon + ',0' }

$lnk.Save()

Write-Host "Created $link"
Write-Host "  -> $target `"$script`""
