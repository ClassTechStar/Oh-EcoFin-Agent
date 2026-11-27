"""性能红线测量：桌面冷启动 <2s（进程启动 → 主窗口可见，×10 中位，附录 E 口径）。

说明：DWM 首帧时间戳跨机差异大，统一采用"进程启动 → 顶层可见窗口出现"轮询口径，
与附录 E "窗口首帧"精神一致；打包产物用 --exe 指定，缺省测量开发模式等效值。

用法：python scripts/bench_cold.py [--exe dist/finagent/finagent.exe] [--n 10]
"""

from __future__ import annotations

import argparse
import ctypes
import os
import statistics
import subprocess
import sys
import time

u32 = ctypes.windll.user32

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TITLE = "Oh EcoFin Agent"


def wait_window_visible(deadline: float) -> float | None:
    """轮询顶层可见窗口标题出现 → 返回 perf_counter 时间。"""
    while time.perf_counter() < deadline:
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(hwnd, _):
            length = u32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                u32.GetWindowTextW(hwnd, buf, length + 1)
                if TITLE in buf.value and u32.IsWindowVisible(hwnd):
                    found.append(time.perf_counter())
                    return False
            return True

        u32.EnumWindows(cb, None)
        if found:
            return found[0]
        time.sleep(0.05)
    return None


def bench_once(exe: str | None) -> float:
    t0 = time.perf_counter()
    if exe:
        proc = subprocess.Popen([exe], cwd=os.path.dirname(os.path.abspath(exe)))
    else:
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "desktop", "main.py")],
                                cwd=ROOT)
    try:
        seen = wait_window_visible(t0 + 30)
        return (seen - t0) if seen else 999.0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", default=None, help="打包产物路径（缺省测量开发模式）")
    parser.add_argument("--n", type=int, default=10)
    args = parser.parse_args()

    samples = []
    for i in range(args.n):
        elapsed = bench_once(args.exe)
        samples.append(elapsed)
        print(f"[{i + 1}/{args.n}] 窗口首帧 {elapsed:.2f}s")
        time.sleep(1)
    med = statistics.median(samples)
    verdict = "✅ 达标" if med < 2.0 else "❌ 未达标"
    print(f"\n冷启动中位数: {med:.2f}s（红线 <2s）{verdict}")


if __name__ == "__main__":
    main()
