"""桌面壳入口（pywebview，冷启动 <2s 纪律）。

启动序：uvicorn 线程先起 → 窗口加载 127.0.0.1:8765（前端骨架屏衔接）→ webview.start()。
重型 import（backtrader / pandas_ta / PyPortfolioOpt）全部在各模块函数内延迟导入，
冷启动路径上只有 FastAPI/pywebview 骨架。

运行：python desktop/main.py
打包：PyInstaller desktop/finagent.spec（onedir）
"""

from __future__ import annotations

import sys
import threading
import time


def _detect_webview2() -> bool:
    """WebView2 运行时检测（Win11 自带；Win10 缺失给下载指引，不裸崩）。"""
    import os

    for sub in ("Microsoft Edge WebView2 Runtime",):
        base = os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                            r"Microsoft\EdgeWebView\Application")
        if os.path.isdir(base):
            return True
        base64 = os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                              r"Microsoft\EdgeWebView\Application")
        if os.path.isdir(base64):
            return True
    return False


def _start_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    """后台线程起 uvicorn（生产配置显式关 reload）。"""
    import uvicorn

    from backend.app import create_app
    from backend.config import load_settings

    settings = load_settings()
    settings.server.host = host
    settings.server.port = port
    app = create_app(settings)
    config = uvicorn.Config(app, host=host, port=port, reload=False,
                            log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()


def _wait_ready(url: str, timeout: float = 15.0) -> bool:
    """轮询健康端点就绪后再开窗（骨架屏衔接）。"""
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1.5):
                return True
        except Exception:
            time.sleep(0.2)
    return False


def main() -> None:
    if not _detect_webview2():
        print("未检测到 WebView2 运行时：请安装 "
              "https://developer.microsoft.com/microsoft-edge/webview2/ 后重试。")
        # 不裸崩：降级为浏览器打开
        host, port = "127.0.0.1", 8765
        _start_server(host, port)
        url = f"http://{host}:{port}"
        if _wait_ready(url):
            import webbrowser

            webbrowser.open(url)
            print(f"服务已启动: {url}（浏览器模式降级）")
            try:
                while True:
                    time.sleep(3600)
            except KeyboardInterrupt:
                return
        return

    import webview

    host, port = "127.0.0.1", 8765
    _start_server(host, port)
    url = f"http://{host}:{port}"
    _wait_ready(url)
    webview.create_window(
        "哦经金智能体 · Oh EcoFin Agent",
        url,
        width=1280, height=860, min_size=(420, 640),
    )
    webview.start()


if __name__ == "__main__":
    sys.exit(main())
