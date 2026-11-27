"""功能联调验证：真实启动 uvicorn → 验证静态托管 / API 端点 / SSE 协议。

用法：python scripts/smoke_e2e.py
验证项（与方案交付核对单对齐）：
  1. 服务启动 + /api/health（含新鲜度协议字段）
  2. 前端静态托管 / （index.html + tokens.css 三端同源相对路径）
  3. 会话 API 全链
  4. SSE v2 响应协议（防缓冲头 / 帧 id 格式 / replay_only）
  5. 成本看板 + 免责声明三层 + 无密钥诚实降级
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
PORT = 8899
BASE = f"http://127.0.0.1:{PORT}"
ok = True


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok
    print(("✅ " if cond else "❌ ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        ok = False


def main() -> None:
    env = dict(os.environ)
    env["DEMO_MODE"] = "1"
    env["FINAGENT_DB_PATH"] = str(ROOT / "data" / "smoke_e2e.db")
    env["FINAGENT_SERVER__PORT"] = str(PORT)
    proc = subprocess.Popen(
        [sys.executable, "-m", "backend.app"], cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        client = httpx.Client(base_url=BASE, trust_env=False, timeout=10)
        ready = False
        last_err = ""
        for _ in range(60):
            try:
                r0 = client.get("/api/health")
                if r0.status_code == 200:
                    ready = True
                    last_err = ""
                    break
                last_err = f"{r0.status_code} {r0.text[:120]}"
            except Exception as e:
                last_err = repr(e)[:120]
            time.sleep(0.5)
        check("1a 服务启动", ready, last_err)
        if not ready:
            return

        h = client.get("/api/health").json()
        check("1b /api/health 新鲜度协议",
              {"data_as_of", "is_stale", "freshness"} <= set(h["snapshot"]))
        check("1c 免责声明(API 层)", "不构成任何投资建议" in h["disclaimer"])

        r = client.get("/")
        check("2a 前端 index 托管", r.status_code == 200 and "哦经金智能体" in r.text)
        m = re.search(r'src="(\./assets/[^"]+\.js)"', r.text)
        check("2b 相对路径资产(三端同源)", m is not None, m.group(1) if m else "")
        if m:
            check("2c JS chunk 可达", client.get(m.group(1)).status_code == 200)
        cm = re.search(r'href="(\./assets/[^"]+\.css)"', r.text)
        if cm:
            css_text = client.get(cm.group(1)).text
            check("2d tokens.css 红涨绿跌 Token",
                  "--up: #e03e3e" in css_text and "--down: #0f8b6d" in css_text)

        s = client.post("/api/sessions").json()
        check("3a 会话创建", bool(s.get("id")))
        lst = client.get("/api/sessions").json()
        check("3b 会话列表", any(x["id"] == s["id"] for x in lst["sessions"]))
        msgs = client.get(f"/api/sessions/{s['id']}/messages").json()
        check("3c 消息接口(带免责)", "disclaimer" in msgs)

        q = client.get("/api/risk-questions").json()
        check("3d 投顾题库 5 题", len(q["questions"]) == 5 and "equity_caps" in q)

        sid = s["id"]
        r = client.get(f"/api/chat/stream?sid={sid}&replay_only=true")
        check("4a SSE 响应头(X-Accel-Buffering: no)",
              r.headers.get("x-accel-buffering") == "no")
        check("4b SSE Cache-Control: no-store",
              "no-store" in r.headers.get("cache-control", ""))
        check("4c SSE 帧 id/event 格式", "id: " in r.text and "event: ping" in r.text)

        cost = client.get("/api/cost/summary").json()
        check("5 成本看板 + 熔断字段", {"today_cents", "cap_cents", "fuse_open"} <= set(cost))

        miss = h.get("missing_keys")
        check("6 无密钥诚实降级(degraded 而非伪装)",
              h["status"] == "degraded" and bool(miss))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
    print("\n" + ("🎉 联调全部通过" if ok else "⚠️ 存在失败项，见上方 ❌"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
