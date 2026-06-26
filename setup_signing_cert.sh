#!/bin/bash
# 为 Typeless.app 建/恢复稳定的自签名代码签名证书「Typeless Dev」。
#
# 为什么需要:adhoc 签名的 app,macOS TCC(辅助功能/麦克风授权)按 cdhash 认 —— 每次
# 重打包 cdhash 都变,授权失效要重授。用一个稳定的自签证书签名后,designated requirement
# 变成「identifier Typeless and certificate root = H"<固定哈希>"」,跨重建不变 → 授权给一次永久保留。
#
# 幂等:证书已在钥匙串则跳过。换机/钥匙串丢失后跑本脚本即可恢复(优先用 DATA_DIR 的 p12 备份)。
set -e

SIGN_ID="Typeless Dev"
DATA_DIR="$HOME/Library/Application Support/Typeless"
P12_BACKUP="$DATA_DIR/typeless-dev-cert.p12"
P12_PASS="typeless"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if codesign --force --sign "$SIGN_ID" "$(mktemp -d)/probe" 2>/dev/null; then
  echo "✓ 证书「$SIGN_ID」已可用,无需操作。"
  exit 0
fi
# 上面 probe 对空文件会失败,改用 keychain 查存在性
if security find-certificate -c "$SIGN_ID" "$KEYCHAIN" >/dev/null 2>&1; then
  echo "✓ 证书「$SIGN_ID」已在钥匙串。"
  exit 0
fi

echo "[*] 钥匙串无「$SIGN_ID」证书,开始创建/恢复…"

# 优先用 DATA_DIR 的 p12 备份恢复
if [ -f "$P12_BACKUP" ]; then
  echo "[*] 发现备份 $P12_BACKUP,直接导入。"
  security import "$P12_BACKUP" -k "$KEYCHAIN" -P "$P12_PASS" -T /usr/bin/codesign -A
  echo "✓ 已从备份恢复证书。"
  exit 0
fi

# 否则全新生成(openssl 3.x 必须 -legacy + SHA1 MAC,否则 Apple security 报 MAC verification failed)
echo "[*] 无备份,全新生成自签证书…"
WORK="$(mktemp -d)"
openssl req -x509 -newkey rsa:2048 -keyout "$WORK/key.pem" -out "$WORK/cert.pem" -days 3650 -nodes \
  -subj "/CN=$SIGN_ID/O=xiaomao" \
  -addext "extendedKeyUsage=codeSigning" -addext "keyUsage=critical,digitalSignature"
openssl pkcs12 -export -inkey "$WORK/key.pem" -in "$WORK/cert.pem" -out "$WORK/td.p12" \
  -passout "pass:$P12_PASS" -name "$SIGN_ID" \
  -legacy -macalg sha1 -keypbe PBE-SHA1-3DES -certpbe PBE-SHA1-3DES
security import "$WORK/td.p12" -k "$KEYCHAIN" -P "$P12_PASS" -T /usr/bin/codesign -A

mkdir -p "$DATA_DIR"
cp "$WORK/td.p12" "$P12_BACKUP"
rm -rf "$WORK"
echo "✓ 证书「$SIGN_ID」已创建并导入钥匙串,备份在 $P12_BACKUP"
echo "  现在跑 ./build_mac.sh 即用此证书签名;授权一次后重打包不再需要重授。"
