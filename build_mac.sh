#!/bin/bash
# Typeless (macOS) 真·打包脚本 —— 把 voicetype_mac.py 打成 Typeless.app
# 与原仓库 build_release_mac.sh(演示用、产物跑不了)不同,本脚本产出可运行的 .app。
set -e
cd "$(dirname "$0")"

VENV=.venv-mac
PY="$VENV/bin/python"
APP_NAME="Typeless"

echo "========================================="
echo "  Typeless macOS 打包 (PyInstaller)"
echo "========================================="

if [ ! -x "$PY" ]; then
  echo "[!] 未找到 venv,先创建: /opt/homebrew/bin/python3.12 -m venv $VENV"
  /opt/homebrew/bin/python3.12 -m venv "$VENV"
fi

echo "[1/4] 安装依赖 + PyInstaller..."
"$PY" -m pip install -q -r requirements-mac.txt pyinstaller

echo "[2/4] PyInstaller 打包(--collect-all 兜全 mlx Metal 库与资源)..."
# torch(437M)只被 mlx_whisper 的 legacy torch_whisper 路径用,我们走 mlx 后端,排除以瘦身
"$PY" -m PyInstaller --noconfirm --clean --windowed --name "$APP_NAME" \
  --collect-all mlx \
  --collect-all mlx_whisper \
  --collect-all sounddevice \
  --collect-all rumps \
  --collect-all opencc \
  --hidden-import pynput.keyboard._darwin \
  --hidden-import pynput.mouse._darwin \
  --exclude-module torch \
  --exclude-module torchaudio \
  --exclude-module tensorboard \
  voicetype_mac.py

PLIST="dist/$APP_NAME.app/Contents/Info.plist"
echo "[3/4] 注入权限声明 + 后台 App 标记到 Info.plist..."
/usr/libexec/PlistBuddy -c "Add :NSMicrophoneUsageDescription string '需要麦克风进行语音录入'" "$PLIST" 2>/dev/null || \
  /usr/libexec/PlistBuddy -c "Set :NSMicrophoneUsageDescription '需要麦克风进行语音录入'" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PLIST" 2>/dev/null || true

# 改 Info.plist 会让签名失效 → macOS 报"已损坏"拒开,必须改完 plist 后重签。
# 用稳定自签证书「Typeless Dev」签名(而非 adhoc):TCC 按"证书根 + bundle id"认,
# designated requirement 跨重建不变 → 辅助功能/麦克风授权只需给一次,重打包不用重授。
# 证书不在钥匙串(换机/未导入)时回退 adhoc(功能一样,只是每次重建要重授权)。
SIGN_ID="Typeless Dev"
echo "[3.5/4] 代码签名(优先稳定证书,回退 adhoc)..."
xattr -cr "dist/$APP_NAME.app" 2>/dev/null || true
if codesign --force --deep --sign "$SIGN_ID" "dist/$APP_NAME.app" 2>/dev/null; then
  echo "  ✓ 已用稳定证书「$SIGN_ID」签名 → TCC 授权跨重建保留"
else
  echo "  ⚠ 「$SIGN_ID」证书不可用,回退 adhoc(每次重建需重授权;跑 ./setup_signing_cert.sh 建证书)"
  codesign --force --deep --sign - "dist/$APP_NAME.app"
fi
codesign --verify --deep "dist/$APP_NAME.app" && echo "  签名校验通过 ✓"

echo "[4/4] 完成 ✅"
echo "产物: dist/$APP_NAME.app"
echo ""
echo "首次运行需在『系统设置 → 隐私与安全性』授予:"
echo "  · 麦克风   (录音)"
echo "  · 辅助功能 (全局热键 + 模拟 Cmd+V 输入)"
echo "首次说话会自动下载 whisper 模型(~500MB,之后离线可用)。"
