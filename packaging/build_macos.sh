#!/bin/bash
# agentHUB macOS 桌面版构建：.app → 签名 → dmg。只能在 macOS 上运行。
#
# 用法：
#   packaging/build_macos.sh                 # ad-hoc 签名（本机及自己的 Mac 可用）
#   SIGN_ID="Developer ID Application: 名字 (TEAMID)" packaging/build_macos.sh
#                                            # 正式签名；公证另见脚本末尾说明
# 产物：dist-macos/agentHUB.app、dist-macos/agentHUB-<版本>-<架构>.dmg
set -euo pipefail
cd "$(dirname "$0")/.."

[ "$(uname)" = "Darwin" ] || { echo "只能在 macOS 上构建"; exit 1; }
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
ARCH=$(uname -m)
SIGN_ID="${SIGN_ID:--}"   # "-" = ad-hoc 签名

echo "==> 依赖（独立的构建环境 .venv-build-macos，不污染开发环境）"
UV_PROJECT_ENVIRONMENT=.venv-build-macos uv sync -q --extra desktop
uv pip install -q --python .venv-build-macos/bin/python "pyinstaller>=6.10"

echo "==> 图标"
[ -f packaging/agenthub.icns ] || .venv-build-macos/bin/python packaging/make_icns.py

echo "==> PyInstaller"
rm -rf build-macos dist-macos
.venv-build-macos/bin/pyinstaller --noconfirm --clean \
  --workpath build-macos --distpath dist-macos packaging/agenthub_macos.spec

APP=dist-macos/agentHUB.app
echo "==> 签名（$SIGN_ID）"
if [ "$SIGN_ID" = "-" ]; then
  codesign --force --deep --sign - "$APP"
else
  codesign --force --deep --options runtime --timestamp --sign "$SIGN_ID" "$APP"
fi
codesign --verify --deep --strict "$APP"

echo "==> dmg"
DMG="dist-macos/agentHUB-$VERSION-$ARCH.dmg"
STAGE=$(mktemp -d)
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"   # 打开 dmg 后拖进「应用程序」即可安装
hdiutil create -quiet -volname "agentHUB" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"

echo "完成：$APP"
echo "      $DMG"
# 对外分发（别人的 Mac 双击不被 Gatekeeper 拦）还需公证，需要 Apple Developer 账号：
#   xcrun notarytool submit "$DMG" --keychain-profile <profile> --wait
#   xcrun stapler staple "$DMG"
