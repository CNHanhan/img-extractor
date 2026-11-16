# 一键打包单文件 exe
#
#   用法：
#     pwsh ./build.ps1                    # 用 PATH 里的 python
#     pwsh ./build.ps1 -Python "py -3.14" # 指定解释器
#
#   产物：dist\FirmwareExtractor.exe（并打印大小与 SHA256）
#
param(
    [string]$Python = "python",
    [string]$Name = "FirmwareExtractor"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$libs = Join-Path $root "libs"
if (-not (Test-Path (Join-Path $libs "PyQt5"))) {
    Write-Host "[1/3] 安装运行依赖到 libs\ ..."
    & $Python -m pip install --disable-pip-version-check --quiet --target $libs -r requirements.txt
} else {
    Write-Host "[1/3] libs\ 已存在，跳过安装"
}

Write-Host "[2/3] PyInstaller 打包 ..."
& $Python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name $Name `
    --icon app.ico `
    --add-data "app.ico;." `
    --paths $libs `
    --exclude-module tkinter `
    qt_extractor.py

$exe = Join-Path $root "dist\$Name.exe"
if (-not (Test-Path $exe)) {
    throw "打包失败：未找到 $exe"
}

Write-Host "[3/3] 产物"
Get-Item $exe | Select-Object FullName, @{ n = "MB"; e = { [math]::Round($_.Length / 1MB, 2) } }, LastWriteTime
Get-FileHash $exe -Algorithm SHA256 | Select-Object Hash

Write-Host ""
Write-Host "自检: & '$exe' --selftest   (返回码 0 = 全部通过)"
