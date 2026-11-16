# 一键打包单文件 exe
#
#   用法：
#     powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
#     powershell -ExecutionPolicy Bypass -File .\build.ps1 -Python "py -3.14"
#
#   产物：dist\FirmwareExtractor.exe（并打印大小与 SHA256）
#
#   说明：PyInstaller 的 Qt 钩子与 onefile 引导器都不吃中文路径，
#         所以依赖与打包工作区统一放到 ASCII 临时目录，最后只把 exe 拷回来。
#
param(
    [string]$Python = "python",
    [string]$Name = "FirmwareExtractor"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-Ascii([string]$s) { return ($s -match '^[\x20-\x7E]+$') }

# 选一个纯 ASCII 的临时根目录
$base = $env:TEMP
if (-not (Test-Ascii $base)) { $base = "$env:SystemDrive\fwx_tmp" }
if (-not (Test-Path $base)) { New-Item -ItemType Directory -Force -Path $base | Out-Null }

$libs = Join-Path $base "fwx_libs"
$stage = Join-Path $base ("fwx_build_" + [guid]::NewGuid().ToString("N").Substring(0, 6))

Write-Host "项目目录: $root"
Write-Host "构建目录: $stage"
Write-Host "依赖目录: $libs"

if (-not (Test-Path (Join-Path $libs "PyQt5"))) {
    Write-Host "[1/4] 安装运行依赖（ASCII 路径）..."
    & $Python -m pip install --disable-pip-version-check --quiet --target $libs -r (Join-Path $root "requirements.txt")
} else {
    Write-Host "[1/4] 依赖已就绪，跳过安装"
}

Write-Host "[2/4] 准备打包工作区..."
New-Item -ItemType Directory -Force -Path $stage | Out-Null
foreach ($f in @("qt_extractor.py", "payload_extractor.py", "update_metadata_pb2.py", "app.ico")) {
    Copy-Item (Join-Path $root $f) (Join-Path $stage $f) -Force
}

Write-Host "[3/4] PyInstaller 打包..."
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
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 退出码 $LASTEXITCODE" }
} finally {
    Pop-Location
}

$produced = Join-Path $stage "dist\$Name.exe"
if (-not (Test-Path $produced)) { throw "打包失败：未找到 $produced" }

Write-Host "[4/4] 产物"
$outDir = Join-Path $root "dist"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$final = Join-Path $outDir "$Name.exe"
Copy-Item $produced $final -Force
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue

Get-Item $final | Select-Object FullName, @{ n = "MB"; e = { [math]::Round($_.Length / 1MB, 2) } }, LastWriteTime
Get-FileHash $final -Algorithm SHA256 | Select-Object Hash

Write-Host ""
Write-Host "自检: & '$final' --selftest    (返回码 0 = 全部通过)"
Write-Host "提示: 请把 exe 放在常规目录运行（低完整性/沙箱目录会导致解包失败）"
