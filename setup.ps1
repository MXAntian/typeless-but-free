# VoiceType 一键安装（Windows / PowerShell）
# 用法：在 E:\Project\voicetype 下右键“用 PowerShell 运行”，或：
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".\.venv")) {
    Write-Host "[1/3] 创建虚拟环境 .venv ..." -ForegroundColor Cyan
    python -m venv .venv
}

Write-Host "[2/3] 安装依赖 ..." -ForegroundColor Cyan
.\.venv\Scripts\python.exe -m pip install --upgrade pip -q
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

Write-Host "[3/3] 卸载 hf-xet（避免模型小文件下成 0 字节）..." -ForegroundColor Cyan
.\.venv\Scripts\python.exe -m pip uninstall -y hf-xet 2>$null

Write-Host ""
Write-Host "安装完成。运行：" -ForegroundColor Green
Write-Host "    .\run.bat" -ForegroundColor Yellow
Write-Host "首次运行会下载 Whisper 模型（large-v3 约 1.5GB），耐心等一次即可。"
