"""性能红线测量：3 股对比端到端 <4s（请求发 → done 事件 ×10 中位，附录 E）。

用法：python scripts/bench_e2e.py [--base http://127.0.0.1:8765] [--n 10]
前置：后端已启动且有可用密钥。
"""

from __future__ import annotations

import argparse
import json
import statistics
import time

import httpx

PROMPT = "对比 600519、000858、601318 三只股票现价与涨跌幅"


def run_once(base: str) -> float:
    t0 = time.perf_counter()
    with httpx.Client(timeout=60) as client:
        with client.stream("POST", f"{base}/api/chat",
                           json={"message": PROMPT, "mode": "chat"}) as resp:
            assert resp.status_code == 200
            for line in resp.iter_lines():
                if line.startswith("event: done"):
                    return time.perf_counter() - t0
                if line.startswith("event: error"):
                    raise RuntimeError(f"error event: {line}")
    raise RuntimeError("未收到 done 事件")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--n", type=int, default=10)
    args = parser.parse_args()

    samples = []
    for i in range(args.n):
        try:
            elapsed = run_once(args.base)
            samples.append(elapsed)
            print(f"[{i + 1}/{args.n}] {elapsed:.2f}s")
        except Exception as e:
            print(f"[{i + 1}/{args.n}] 失败: {e}")
    if samples:
        med = statistics.median(samples)
        verdict = "✅ 达标" if med < 4.0 else "❌ 未达标"
        print(f"\n中位数: {med:.2f}s（红线 <4s）{verdict}")
        print(json.dumps({"n": len(samples), "median_s": round(med, 3),
                          "min": round(min(samples), 3), "max": round(max(samples), 3)},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
