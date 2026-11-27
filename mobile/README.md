# 移动壳（Capacitor → APK）

一份前端三端可达：移动 APK 复用 `frontend/` 构建产物做响应式适配。

## 标准构建路径

```bash
# 0) 前置：Node 18+，Android Studio（含 SDK 34），JDK 17
npm install -g @capacitor/cli

# 1) 构建前端产物
cd frontend && npm ci && npm run build && cd ..

# 2) 初始化 Capacitor 工程（首次）
cd mobile
npm init -y
npm install @capacitor/core @capacitor/android @capacitor/cli
npx cap init "Oh EcoFin Agent" "club.oh.ecofin.agent" --web-dir www

# 3) 拷贝前端产物到 www/ 并同步
rm -rf www && cp -r ../frontend/dist www
npx cap add android
npx cap sync

# 4) 明文 HTTP 白名单（避坑 #7）：把演示机局域网 IP 登记进
#    android/app/src/main/res/xml/network_security_config.xml
#    模板见本目录 network_security_config.xml

# 5) 打 APK
cd android && ./gradlew assembleDebug
# 产物: android/app/build/outputs/apk/debug/app-debug.apk
```

## 首屏配置后端地址（扫码配对）

桌面端「连接手机」按钮生成二维码：`http://<LAN-IP>:8765/?t=<token>`。
移动壳启动时若检测不到本地后端，弹出扫码页 → 把地址写入 localStorage 后加载。

## 降级预案（预先与助教对齐口径）

一键构建：`bash mobile/build_apk.sh`（需 Android Studio/JDK 17；`--release` 出 release 包）。

若 Capacitor 打包受阻（SDK/签名环境问题），移动端演示退化为：
**手机浏览器扫码直连局域网地址**——演示形态不变（同一前端三端可达的移动形态）。
