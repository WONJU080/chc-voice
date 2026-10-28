#!/bin/bash
# Install BlackHole 2ch (the only virtual audio driver chc-voice needs).
# This script intentionally asks for administrator approval; it never installs silently.
set -euo pipefail

VERSION="0.7.1"
SHA256="57b540f27a3e29c37e310e01bee0fdfab76733087e47f997ef9dccf851400dcf"
URL="https://existential.audio/downloads/BlackHole2ch-${VERSION}.pkg"
DRIVER="/Library/Audio/Plug-Ins/HAL/BlackHole2ch.driver"

if [[ -d "$DRIVER" ]]; then
  echo "BlackHole 2ch 已安装：$DRIVER"
  exit 0
fi

TMP="$(mktemp -d)/BlackHole2ch-${VERSION}.pkg"
trap 'rm -rf "$(dirname "$TMP")"' EXIT

echo "下载 BlackHole ${VERSION}…"
curl -fL --retry 3 -o "$TMP" "$URL"
ACTUAL="$(shasum -a 256 "$TMP" | awk '{print $1}')"
if [[ "$ACTUAL" != "$SHA256" ]]; then
  echo "校验失败：期望 $SHA256，实际 $ACTUAL" >&2
  exit 1
fi
echo "SHA-256 校验通过。"

echo "安装需要管理员权限。"
sudo /usr/sbin/installer -pkg "$TMP" -target /

echo "重启 Core Audio…"
sudo /usr/bin/killall coreaudiod 2>/dev/null || true
sleep 1

if [[ -d "$DRIVER" ]]; then
  echo "BlackHole 2ch 安装完成。"
  echo "下一步：chc-voice aggregate create   # 创建 chc-voice Aggregate Device"
else
  echo "驱动目录未出现，请注销并重新登录（或重启）后再运行本脚本检查。" >&2
  exit 1
fi
