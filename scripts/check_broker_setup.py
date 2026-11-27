"""实盘通道自检（不进行任何交易）：配置完整性 / SDK / HTTP 连通性。

用法：
    python scripts/check_broker_setup.py
环境变量（示例）：
    FINAGENT_EXECUTION__LIVE_ENABLED=1
    FINAGENT_EXECUTION__BROKER_KIND=http
    FINAGENT_EXECUTION__HTTP_BASE_URL=http://127.0.0.1:8898
    FINAGENT_EXECUTION__HTTP_TOKEN=dev-token

退出码：0 = 配置与连通性通过；1 = 存在问题（逐条打印修复建议）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import ExecutionConfig, load_settings  # noqa: E402
from backend.execution.brokers import mask_account  # noqa: E402


def check_qmt(cfg: ExecutionConfig) -> list[str]:
    problems: list[str] = []
    if not cfg.account_id:
        problems.append("缺少资金账号：FINAGENT_EXECUTION__ACCOUNT_ID")
    if not cfg.qmt_path:
        problems.append("缺少 miniQMT 路径：FINAGENT_EXECUTION__QMT_PATH")
    elif not Path(cfg.qmt_path).exists():
        problems.append(f"miniQMT 路径不存在: {cfg.qmt_path}（应为 userdata_mini 目录）")
    try:
        import xtquant  # noqa: F401
    except ImportError:
        problems.append(
            "未安装 xtquant：需券商 QMT/miniQMT 客户端，"
            "将 bin.x64/Lib/site-packages 加入 PYTHONPATH（非 pip 包）")
    return problems


def check_http(cfg: ExecutionConfig, *,
               transport: httpx.BaseTransport | None = None,
               timeout: float | None = None) -> list[str]:
    if not cfg.http_base_url:
        return ["缺少网关地址：FINAGENT_EXECUTION__HTTP_BASE_URL"]
    headers = {"Authorization": f"Bearer {cfg.http_token}"} if cfg.http_token else {}
    host = (urlparse(cfg.http_base_url).hostname or "").lower()
    try:
        with httpx.Client(
            base_url=cfg.http_base_url.rstrip("/"),
            headers=headers,
            timeout=float(timeout or cfg.timeout_seconds or 10.0),
            transport=transport,
            trust_env=host not in ("127.0.0.1", "localhost", "::1"),
        ) as client:
            resp = client.get("/health")
    except httpx.HTTPError as e:
        return [f"网关不可达: {type(e).__name__}: {e}"]
    if resp.status_code != 200:
        return [f"网关健康检查失败: HTTP {resp.status_code}"]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="实盘券商通道自检（只读）")
    parser.add_argument("--timeout", type=float, default=None)
    args = parser.parse_args(argv)

    settings = load_settings()
    cfg = settings.execution
    print("== 实盘通道配置 ==")
    print(f"live_enabled : {cfg.live_enabled}")
    print(f"broker_kind  : {cfg.broker_kind}")
    print(f"account      : {mask_account(cfg.account_id) or '(未配置)'}")
    if not cfg.live_enabled:
        print("[WARN] live_enabled=False：仅做配置检查，应用不会装配实盘网关")

    kind = (cfg.broker_kind or "").strip().lower()
    if kind == "qmt":
        problems = check_qmt(cfg)
    elif kind in ("http", "openapi"):
        problems = check_http(cfg, timeout=args.timeout)
    else:
        problems = [f"未知适配器类型: {cfg.broker_kind!r}（应为 qmt 或 http）"]

    if problems:
        print("\n== 检查结果：未通过 ==")
        for item in problems:
            print(f"[FAIL] {item}")
        print("\n提示：本地联调可先运行 python scripts/mock_broker_gateway.py --port 8898")
        return 1
    print("\n== 检查结果：通过 ==")
    print("[OK] 配置完整且通道可达；应用内可调用 POST /api/live/probe 复核")
    return 0


if __name__ == "__main__":
    sys.exit(main())
