# 打 Windows 包（onedir）：powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#
# 产物：dist\小邹RPA\（整个文件夹就是"安装包"，拷给别人即可）
#       dist\小邹RPA\小邹RPA.exe 是入口，第一次运行会弹安装向导。
#
# 注意：浏览器内核（Chromium，约 150MB）不打进包里，由安装向导联网下载到
#       %LOCALAPPDATA%\ms-playwright。
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot            # 仓库根目录
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "没找到 $python —— 先在项目目录建好 .venv 并装依赖"
}

Write-Host "== 1/2 检查打包工具 ==" -ForegroundColor Cyan
& $python -m pip install --quiet -r (Join-Path $PSScriptRoot "requirements-pack.txt")

Write-Host "== 2/2 开始打包（几分钟，中间会刷很多日志）==" -ForegroundColor Cyan
& $python -m PyInstaller `
    --noconfirm --clean `
    --distpath (Join-Path $root "dist") `
    --workpath (Join-Path $root "build") `
    (Join-Path $PSScriptRoot "小邹RPA.spec")

$exe = Join-Path $root "dist\小邹RPA\小邹RPA.exe"
if (-not (Test-Path $exe)) { throw "打包结束但没找到 $exe" }

$size = (Get-ChildItem (Join-Path $root "dist\小邹RPA") -Recurse -File |
         Measure-Object -Property Length -Sum).Sum / 1MB
Write-Host ("打包完成：{0}（{1:N0} MB）" -f $exe, $size) -ForegroundColor Green
Write-Host "把这个 dist\小邹RPA 文件夹整个拷给别人，双击 exe 就会弹安装向导。"
