# 一键准备开发环境：创建虚拟环境并安装依赖
# 用法（任选一种）：
#   1) powershell -ExecutionPolicy Bypass -File .\scripts\setup_env.ps1
#   2) 先执行一次 Set-ExecutionPolicy -Scope CurrentUser RemoteSigned，之后直接 .\scripts\setup_env.ps1

$ErrorActionPreference = "Stop"

# 无论从哪里调用，都回到项目根目录（scripts 的上一级）
Set-Location (Split-Path $PSScriptRoot -Parent)

# 找 Python 3.12（WindowsApps 下的是商店占位符，不算）
$python = $null
$candidates = @(
    "C:\Users\$env:USERNAME\AppData\Local\Programs\Python\Python312\python.exe",
    "C:\Program Files\Python312\python.exe"
)
foreach ($c in $candidates) {
    if (Test-Path $c) { $python = $c; break }
}
if (-not $python) {
    foreach ($cmd in @("py", "python")) {
        try {
            $ver = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($ver -eq "3.12") { $python = $cmd; break }
        } catch { }
    }
}
if (-not $python) {
    Write-Error "没有找到 Python 3.12，请先安装：https://www.python.org/downloads/release/python-31210/ （勾选 Add to PATH）"
}

Write-Host "使用 Python: $python"
& $python -c "import sys; print(sys.version)"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "创建虚拟环境 .venv ..."
    & $python -m venv .venv
}

Write-Host "安装依赖（可能需要几分钟）..."
& .venv\Scripts\python.exe -m pip install --upgrade pip
& .venv\Scripts\python.exe -m pip install -r requirements.txt

Write-Host ""
Write-Host "自检："
& .venv\Scripts\python.exe -m app.main --version

Write-Host ""
Write-Host "完成。以后运行程序用：.venv\Scripts\python.exe -m app.main"
