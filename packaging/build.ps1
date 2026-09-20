# 打 Windows 包（单文件 exe）：powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#
# 产物：dist\小邹RPA.exe —— 就这一个文件，拷给别人即可；
#       第一次运行会弹一个窗口：选数据放哪儿，然后在里面建好示例项目、
#       下载浏览器内核（Chromium，约 150MB）到 <那个目录>\浏览器\。
#
# 注意：浏览器内核不打进包里，运行时才下载。
#       单文件版每次启动要先解包到 %TEMP%（几秒钟），想秒开就改成 onedir（见 spec 注释）。
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

$exe = Join-Path $root "dist\小邹RPA.exe"
if (-not (Test-Path $exe)) { throw "打包结束但没找到 $exe" }

$size = (Get-Item $exe).Length / 1MB
Write-Host ("打包完成：{0}（{1:N0} MB，单文件）" -f $exe, $size) -ForegroundColor Green
Write-Host "把这个 exe 拷给别人，双击第一次运行会让他选个数据目录，然后自动准备好。"
