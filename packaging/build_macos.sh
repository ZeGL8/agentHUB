#!/bin/bash
# agentHUB macOS 桌面版构建：.app → 签名 →（可选）公证 → dmg。只能在 macOS 上运行。
#
# 三种用法，区别在于别人的 Mac 打开时会不会被 Gatekeeper 拦下：
#
# 1) ad-hoc 签名（默认，免费）：
#      packaging/build_macos.sh
#    本机构建、本机运行没问题；传给别人（或经浏览器/微信/AirDrop 传到另一台 Mac）
#    会提示「Apple 无法验证 agentHUB 是否包含恶意软件」。dmg 里附带「打不开请看我.txt」
#    说明如何放行。
#
# 2) Developer ID 签名 + 公证（需要 Apple Developer 账号，彻底不弹警告）：
#      先存一次公证凭据（App 专用密码在 appleid.apple.com 生成）：
#        xcrun notarytool store-credentials agenthub-notary \
#          --apple-id 你的AppleID --team-id TEAMID --password App专用密码
#      之后每次构建：
#        SIGN_ID="Developer ID Application: 名字 (TEAMID)" NOTARY_PROFILE=agenthub-notary \
#          packaging/build_macos.sh
#    CI 中不方便用钥匙串配置文件时，也可改设 APPLE_ID / APPLE_TEAM_ID / APPLE_APP_PASSWORD。
#
# 3) 只签名不公证（设置 SIGN_ID，不设公证凭据）：别人打开仍会被拦，仅用于调试签名。
#
# 产物：dist-macos/agentHUB.app、dist-macos/agentHUB-<版本>-<架构>.dmg
set -euo pipefail
cd "$(dirname "$0")/.."

[ "$(uname)" = "Darwin" ] || { echo "只能在 macOS 上构建"; exit 1; }
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
ARCH=$(uname -m)
SIGN_ID="${SIGN_ID:--}"   # "-" = ad-hoc 签名
ENTITLEMENTS=packaging/entitlements.plist

# macOS 自带 bash 3.2 在 set -u 下展开空数组会报错，故用 NOTARIZE 开关而不判断数组长度
NOTARIZE=0
if [ -n "${NOTARY_PROFILE:-}" ]; then
  NOTARIZE=1; NOTARY_ARGS=(--keychain-profile "$NOTARY_PROFILE")
elif [ -n "${APPLE_ID:-}" ] && [ -n "${APPLE_TEAM_ID:-}" ] && [ -n "${APPLE_APP_PASSWORD:-}" ]; then
  NOTARIZE=1; NOTARY_ARGS=(--apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_PASSWORD")
fi
if [ "$SIGN_ID" = "-" ] && [ "$NOTARIZE" = 1 ]; then
  echo "公证需要 Developer ID 签名：请同时设置 SIGN_ID"; exit 1
fi

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
# 去掉构建过程中可能带上的扩展属性（Finder 信息、隔离标记等），否则 codesign 会报
# “resource fork, Finder information, or similar detritus not allowed”
xattr -cr "$APP"

sign_one() {
  if [ "$SIGN_ID" = "-" ]; then
    codesign --force --sign - "$1"
  else
    codesign --force --timestamp --options runtime \
      --entitlements "$ENTITLEMENTS" --sign "$SIGN_ID" "$1"
  fi
}

echo "==> 签名（$SIGN_ID）"
# 由内向外逐个签名（Apple 不推荐 --deep 用于正式签名，公证时内层二进制缺
# 时间戳 / Hardened Runtime 会被拒）：先签所有 Mach-O 文件，再签嵌套 .framework，最后签 .app。
while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q "Mach-O"; then
    sign_one "$f" >/dev/null 2>&1 || sign_one "$f"   # 失败时再跑一次以显示错误
  fi
done < <(find "$APP/Contents" -type f ! -path "$APP/Contents/MacOS/agentHUB" -print0)
while IFS= read -r -d '' fw; do
  sign_one "$fw"
done < <(find "$APP/Contents" -type d -name "*.framework" -print0 | sort -rz)
sign_one "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

notarize() {   # $1 = 要提交的文件（zip 或 dmg）
  echo "    提交公证：$1（通常 1–10 分钟）"
  local out
  if ! out=$(xcrun notarytool submit "$1" "${NOTARY_ARGS[@]}" --wait 2>&1); then
    echo "$out"; echo "公证提交失败"; exit 1
  fi
  echo "$out" | tail -n 3
  if ! echo "$out" | grep -q "status: Accepted"; then
    local id
    id=$(echo "$out" | sed -n 's/^ *id: \(.*\)$/\1/p' | head -1)
    echo "公证未通过，查看原因："
    [ -n "$id" ] && xcrun notarytool log "$id" "${NOTARY_ARGS[@]}" || true
    exit 1
  fi
}

if [ "$NOTARIZE" = 1 ]; then
  echo "==> 公证 .app"
  ZIP=dist-macos/agentHUB-notarize.zip
  ditto -c -k --keepParent "$APP" "$ZIP"
  notarize "$ZIP"
  rm -f "$ZIP"
  xcrun stapler staple "$APP"   # 票据钉进 .app，离线也能通过校验
  spctl --assess --type execute --verbose=2 "$APP"
fi

echo "==> dmg"
DMG="dist-macos/agentHUB-$VERSION-$ARCH.dmg"
STAGE=$(mktemp -d)
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"   # 打开 dmg 后拖进「应用程序」即可安装
if [ "$NOTARIZE" = 0 ]; then
  # 未公证的包在别人 Mac 上一定会被拦，附上放行说明（纯文本不受 Gatekeeper 限制）
  cp packaging/macos_open_guide.txt "$STAGE/打不开请看我.txt"
fi
hdiutil create -quiet -volname "agentHUB" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"

if [ "$SIGN_ID" != "-" ]; then
  codesign --force --timestamp --sign "$SIGN_ID" "$DMG"
fi
if [ "$NOTARIZE" = 1 ]; then
  echo "==> 公证 dmg"
  notarize "$DMG"
  xcrun stapler staple "$DMG"
  spctl --assess --type open --context context:primary-signature --verbose=2 "$DMG"
fi

echo "完成：$APP"
echo "      $DMG"
if [ "$NOTARIZE" = 0 ]; then
  echo
  echo "注意：此 dmg 未经 Apple 公证。传到其他 Mac（或经浏览器/微信/AirDrop 下载）后打开会提示"
  echo "「Apple 无法验证…」。对方可按 dmg 内「打不开请看我.txt」放行，或运行："
  echo "    xattr -dr com.apple.quarantine /Applications/agentHUB.app"
  echo "要彻底消除提示，请用 Developer ID 签名并公证（见本脚本开头说明）。"
fi
