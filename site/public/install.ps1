# decomp.wtf installer for Windows (PowerShell).
#
#   irm https://decomp.wtf/install.ps1 | iex
#
# What it does, and nothing else:
#   1. installs uv, Astral's Python tool manager, if you don't have it
#   2. installs decomp.wtf (open source, MIT) with uv, in its own environment
#   3. downloads the N64 compilers that have Windows builds and the m2c draft decompiler (pinned, checksum-verified)
#   4. adds decomp.wtf to the Start menu and your desktop, and opens it
#
# Your projects go to %USERPROFILE%\Decomp. Nothing is sent to decomp.wtf.
# Uninstall: uv tool uninstall decompnet
$ErrorActionPreference = "Stop"
$Version = "0.8.5"
$Wheel = if ($env:DECOMP_WHEEL) { $env:DECOMP_WHEEL } else { "https://decomp.wtf/dl/decompnet-$Version-py3-none-any.whl" }

function Step($text) { Write-Host "> " -ForegroundColor Green -NoNewline; Write-Host $text }

Write-Host "decomp.wtf setup" -ForegroundColor White

$uvBin = Join-Path $env:USERPROFILE ".local\bin"
if (Test-Path (Join-Path $uvBin "uv.exe")) { $env:Path = "$uvBin;$env:Path" }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Step "installing uv (Astral's Python tool manager)"
    powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$uvBin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw "uv installed, but it isn't on PATH yet; open a new PowerShell and run this again" }
}
Step ("uv " + ((uv --version) -split " ")[1])

Step "installing decomp.wtf $Version"
uv tool install --force --quiet --python 3.12 "decompnet @ $Wheel"
if ($LASTEXITCODE -ne 0) { throw "installing decomp.wtf failed" }
uv tool update-shell 2>$null | Out-Null
$bin = (uv tool dir --bin).Trim()
$decomp = Join-Path $bin "decomp.exe"

Step "downloading the compilers and tools"
& $decomp setup
& $decomp stop | Out-Null  # an older decomp.wtf still running from before this upgrade

& $decomp shortcut | Out-Null
Step "added decomp.wtf to the Start menu and desktop"

Write-Host "Done." -ForegroundColor White
Write-Host "  Start it any time from the Start menu, or with: decomp"
Write-Host "  From your AI agent: run 'decomp connect' once, then ask it to decompile your game."
Write-Host "  Note: KMC GCC (used by many third-party games) has no Windows build yet; IDO games work."
Start-Process -FilePath $decomp -WindowStyle Minimized
