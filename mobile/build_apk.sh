#!/usr/bin/env bash
# 移动 APK 一键构建（mobile 实体化：README 手工流程 → 可复现脚本）。
#
# 前置：Node 18+ / JDK 17 / Android Studio（SDK 34）
# 用法：bash mobile/build_apk.sh [--release]
# 产物：mobile/android/app/build/outputs/apk/debug/app-debug.apk
#       （--release: apk/release/app-release-unsigned.apk，需自行签名）
#
# 若本机缺 Android SDK/签名环境导致失败，按 mobile/README.md 的降级预案：
# 手机浏览器扫码直连局域网地址（演示形态不变）。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VARIANT="debug"
if [[ "${1:-}" == "--release" ]]; then VARIANT="release"; fi

echo "==> [1/6] 构建前端产物"
(cd frontend && npm ci --no-audit --no-fund && npm run build)

echo "==> [2/6] 准备 Capacitor 工程"
cd mobile
if [[ ! -f package.json ]]; then
  npm init -y
fi
npm install @capacitor/core@latest @capacitor/android@latest @capacitor/cli@latest

echo "==> [3/6] 同步前端产物到 www/"
rm -rf www
cp -r ../frontend/dist www

echo "==> [4/6] 初始化/同步 Android 工程"
if [[ ! -d android ]]; then
  npx cap add android
fi
npx cap sync android

echo "==> [5/6] 登记明文 HTTP 白名单（局域网演示）"
NSC_DST="android/app/src/main/res/xml/network_security_config.xml"
if [[ -f "$NSC_DST" ]]; then
  echo "    已存在：$NSC_DST（如需登记新演示机 IP，参照 mobile/network_security_config.xml 手工更新）"
else
  mkdir -p "$(dirname "$NSC_DST")"
  cp network_security_config.xml "$NSC_DST"
  echo "    已从模板复制 → $NSC_DST"
fi

echo "==> [6/6] Gradle 打包（$VARIANT）"
cd android
if [[ "$(uname -s)" == MINGW* || "$(uname -s)" == CYGWIN* ]]; then
  ./gradlew.bat "assemble${VARIANT^}"
else
  ./gradlew "assemble${VARIANT^}"
fi

APK="app/build/outputs/apk/${VARIANT}/app-${VARIANT}.apk"
if [[ -f "$APK" ]]; then
  echo ""
  echo "✅ APK 构建完成: mobile/android/$APK"
  [[ "$VARIANT" == "debug" ]] && echo "   安装: adb install -r $APK"
else
  echo "⚠️ 未找到预期产物 $APK，请检查 android/app/build/outputs/ 实际输出"
fi
