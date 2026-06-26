#!/usr/bin/env bash
# 构建 macOS 免安装包（必须在一台 Mac 上跑；PyInstaller 不能从 Windows 跨平台编译）
# 用法：chmod +x build_release_mac.sh && ./build_release_mac.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/4] venv + 依赖 ..."
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip -q
# Mac 不用 nvidia-* 那几个包（无 mac wheel），只装核心依赖
pip install -q faster-whisper sounddevice numpy pynput pyperclip requests pystray pillow zhconv jieba pyinstaller
pip uninstall -y hf-xet 2>/dev/null || true   # 同 Windows：hf-xet 会把模型小文件下成 0 字节

echo "[2/4] PyInstaller 构建 ..."
pyinstaller --noconfirm --clean --name "TypelessButFree" \
  --collect-all faster_whisper --collect-all ctranslate2 \
  --collect-all onnxruntime --collect-all tokenizers \
  --collect-all av --collect-all sounddevice \
  --collect-all pystray --collect-all PIL --collect-all zhconv --collect-all jieba \
  --exclude-module nvidia --exclude-module torch \
  voicetype.py

echo "[3/4] 放入配置/说明 ..."
DIST="dist/TypelessButFree"
rm -rf "$DIST/models" "$DIST/config.json"
cp release-config.json "$DIST/config.json"
cp RELEASE_README.txt "$DIST/README.txt"

echo "[4/4] 打包 ..."
mkdir -p release
ARCH="$(uname -m)"   # arm64 = Apple Silicon, x86_64 = Intel
( cd dist && zip -qr "../release/TypelessButFree-macos-$ARCH.zip" TypelessButFree )
echo "完成：release/TypelessButFree-macos-$ARCH.zip"
