# 构建 Windows 免安装包（CPU 版）→ release\TypelessButFree-windows-x64.zip
# 用法：powershell -ExecutionPolicy Bypass -File .\build_release.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = ".\.venv\Scripts\python.exe"

Write-Host "[1/4] PyInstaller 构建（排除 CUDA，保持便携）..." -ForegroundColor Cyan
& $py -m PyInstaller --noconfirm --clean --name "TypelessButFree" `
  --collect-all faster_whisper --collect-all ctranslate2 `
  --collect-all onnxruntime --collect-all tokenizers `
  --collect-all av --collect-all sounddevice `
  --collect-all pystray --collect-all PIL `
  --exclude-module nvidia --exclude-module torch `
  voicetype.py | Out-Null

$dist = ".\dist\TypelessButFree"
Write-Host "[2/4] 清理测试残留 + 放入配置/说明 ..." -ForegroundColor Cyan
Remove-Item "$dist\models", "$dist\config.json", "$dist\exe.log", "$dist\exe.err.log" -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item ".\release-config.json" "$dist\config.json" -Force
Copy-Item ".\RELEASE_README.txt" "$dist\README.txt" -Force

Write-Host "[3/4] 打包 zip ..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force ".\release" | Out-Null
$zip = ".\release\TypelessButFree-windows-x64.zip"
Remove-Item $zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path "$dist\*" -DestinationPath $zip

$mb = [math]::Round((Get-Item $zip).Length / 1MB)
Write-Host "[4/4] 完成：$zip  ($mb MB)" -ForegroundColor Green
