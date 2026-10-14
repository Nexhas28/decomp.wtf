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

# Everything runs in its own block: `irm | iex` runs a script in your PowerShell session, and its settings shouldn't
# stay behind there.
& {
$ErrorActionPreference = "Stop"
$Version = "0.9.3"
$Wheel = if ($env:DECOMP_WHEEL) { $env:DECOMP_WHEEL } else { "https://decomp.wtf/dl/decompnet-$Version-py3-none-any.whl" }

function Step($text) { Write-Host "> " -ForegroundColor Green -NoNewline; Write-Host $text }

# uv and decomp say whether they worked with their exit code. Windows PowerShell turns what a program writes to
# stderr into errors, and with "Stop" a harmless note (uv's "already in PATH") would end the install: run them with
# "Continue" and check the exit code instead.
function Run([scriptblock]$Command, [string]$Failure = "", [switch]$Quiet) {
    $ErrorActionPreference = "Continue"
    if ($Quiet) { & $Command *> $null } else { & $Command }
    if ($Failure -and $LASTEXITCODE -ne 0) { throw "$Failure (exit code $LASTEXITCODE)" }
}

Write-Host "decomp.wtf setup" -ForegroundColor White

$uvBin = Join-Path $env:USERPROFILE ".local\bin"
if (Test-Path (Join-Path $uvBin "uv.exe")) { $env:Path = "$uvBin;$env:Path" }
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Step "installing uv (Astral's Python tool manager)"
    Run { powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex" }
    $env:Path = "$uvBin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv was installed, but this window can't find it yet: open a new PowerShell window and run this again"
    }
}
Step ("uv " + ((uv --version) -split " ")[1])

Step "installing decomp.wtf $Version"
Run { uv tool install --force --quiet --python 3.12 "decompnet @ $Wheel" } "installing decomp.wtf failed"
Run { uv tool update-shell } -Quiet  # decomp on PATH for new windows (it says so when it already is: fine)
$bin = (uv tool dir --bin | Out-String).Trim()
if (($env:Path -split ";") -notcontains $bin) { $env:Path = "$bin;$env:Path" }  # and in this window
$decomp = Join-Path $bin "decomp.exe"
if (-not (Test-Path $decomp)) { throw "decomp.wtf was installed, but $decomp isn't there" }

Step "downloading the compilers and tools (a few minutes the first time)"
Run { & $decomp setup }
Run { & $decomp stop } -Quiet  # an older decomp.wtf still running from before this upgrade

Run { & $decomp shortcut } -Quiet
Step "added decomp.wtf to the Start menu and your desktop"

Step "starting decomp.wtf"
Start-Process -FilePath $decomp -WindowStyle Minimized

Write-Host ""
Write-Host "Done. decomp.wtf is opening in your browser." -ForegroundColor White
Write-Host "  Open it again any time: decomp.wtf in the Start menu or on your desktop (or type decomp)."
Write-Host "  It runs in a small minimized window; closing that window stops it."
Write-Host "  From your AI agent: run 'decomp connect' once, then ask it to decompile your game."
Write-Host "  Note: KMC GCC (used by many third-party N64 games) has no Windows build yet; IDO games work."
}
