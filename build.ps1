# Build single-file exe
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
#   add -OutDir "D:\some\dir" to change the release folder
#
# Output:
#   dist\FirmwareExtractor.exe   (for tests)
#   E:\tools\A-IMG提取\FirmwareExtractor.exe  (release copy, default)
#
# Note: PyInstaller's Qt hook and onefile bootloader reject non-ASCII paths,
# so dependencies and the build stage live in an ASCII temp dir; only the
# final exe is copied back to (possibly Chinese) directories.

param(
    [string]$Python = "python",
    [string]$Name = "FirmwareExtractor",
    [string]$OutDir = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# Build the default release path from Unicode code points so it survives
# PowerShell 5.1 reading this UTF-8 file as GBK (A-IMG提取).
if (-not $OutDir) {
    $OutDir = "E:\tools\A-IMG" + [char]0x63D0 + [char]0x53D6
}

function Test-Ascii([string]$s) { return ($s -match '^[\x20-\x7E]+$') }

$base = $env:TEMP
if (-not (Test-Ascii $base)) { $base = "$env:SystemDrive\fwx_tmp" }
if (-not (Test-Path $base)) { New-Item -ItemType Directory -Force -Path $base | Out-Null }

$libs = Join-Path $base "fwx_libs"
$stage = Join-Path $base ("fwx_build_" + [guid]::NewGuid().ToString("N").Substring(0, 6))

Write-Host "Project : $root"
Write-Host "Build   : $stage"
Write-Host "Deps    : $libs"
Write-Host "Release : $OutDir"

if (-not (Test-Path (Join-Path $libs "PyQt5"))) {
    Write-Host "[1/4] install dependencies into ASCII path ..."
    & $Python -m pip install --disable-pip-version-check --quiet --target $libs -r (Join-Path $root "requirements.txt")
} else {
    Write-Host "[1/4] dependencies ready"
}

Write-Host "[2/4] prepare build stage ..."
New-Item -ItemType Directory -Force -Path $stage | Out-Null
foreach ($f in @("qt_extractor.py", "payload_extractor.py", "update_metadata_pb2.py", "app.ico")) {
    Copy-Item (Join-Path $root $f) (Join-Path $stage $f) -Force
}

Write-Host "[3/4] PyInstaller ..."
Push-Location $stage
try {
    & $Python -m PyInstaller --noconfirm --clean --onefile --windowed `
        --name $Name `
        --icon app.ico `
        --add-data "app.ico;." `
        --paths $libs `
        --distpath (Join-Path $stage "dist") `
        --workpath (Join-Path $stage "build") `
        --exclude-module tkinter `
        qt_extractor.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}

$produced = Join-Path $stage "dist\$Name.exe"
if (-not (Test-Path $produced)) { throw "Build failed: $produced not found" }

Write-Host "[4/4] output"
$distDir = Join-Path $root "dist"
New-Item -ItemType Directory -Force -Path $distDir | Out-Null
$final = Join-Path $distDir "$Name.exe"
Copy-Item $produced $final -Force
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue

Get-Item $final | Select-Object FullName, @{ n = "MB"; e = { [math]::Round($_.Length / 1MB, 2) } }, LastWriteTime
$hash = (Get-FileHash $final -Algorithm SHA256).Hash
Write-Host "SHA256: $hash"

try {
    if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Force -Path $OutDir | Out-Null }
    $target = Join-Path $OutDir "$Name.exe"
    Copy-Item $final $target -Force -ErrorAction Stop
    Write-Host "Released to: $target"
} catch {
    Write-Host "WARN: cannot overwrite target exe; close the running program first."
    Write-Host "Build output remains at: $final"
}

Write-Host "SELFTEST: & '$final' --selftest  (exit 0 = all passed)"
