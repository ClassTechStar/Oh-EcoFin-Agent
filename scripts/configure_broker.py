"""交互式配置实盘券商通道（HTTP / QMT），写入本机 .env 或 Windows 凭据管理器。

安全约束：
- token 默认存入凭据管理器（服务名 oh-ecofin-agent，账户 broker_http_token），
  仅在明确选择 `--store file` 时才写入 .env（.env 已 gitignore）；
- 终端不回显也不打印 token，只显示脱敏账号；
- HTTP 模式默认先探测 `/health`，探测失败不落盘（可用 --force 跳过）。

用法：
    python scripts/configure_broker.py                     # 交互配置 HTTP
    python scripts/configure_broker.py --kind qmt          # 交互配置 miniQMT
    python scripts/configure_broker.py --kind http --base-url https://gw --account 123 \
        --token xxx --no-probe --store keyring
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import ExecutionConfig  # noqa: E402
from backend.execution.brokers import mask_account  # noqa: E402
from scripts.check_broker_setup import check_http, check_qmt  # noqa: E402

KEYRING_SERVICE = "oh-ecofin-agent"
KEYRING_TOKEN_NAME = "broker_http_token"

ENV_LIVE = "FINAGENT_EXECUTION__LIVE_ENABLED"
ENV_KIND = "FINAGENT_EXECUTION__BROKER_KIND"
ENV_ACCOUNT = "FINAGENT_EXECUTION__ACCOUNT_ID"
ENV_QMT_PATH = "FINAGENT_EXECUTION__QMT_PATH"
ENV_HTTP_URL = "FINAGENT_EXECUTION__HTTP_BASE_URL"
ENV_HTTP_TOKEN = "FINAGENT_EXECUTION__HTTP_TOKEN"
ENV_TIMEOUT = "FINAGENT_EXECUTION__TIMEOUT_SECONDS"


def upsert_env(path: Path, updates: dict[str, str]) -> None:
    """更新 .env 指定键，保留注释与其它配置；缺失键追加到末尾。"""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = (line.split("=", 1)[0].strip()
               if "=" in line and not stripped.startswith("#") else "")
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def store_keyring_token(token: str) -> bool:
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, KEYRING_TOKEN_NAME, token)
        return keyring.get_password(KEYRING_SERVICE, KEYRING_TOKEN_NAME) == token
    except Exception:
        return False


def _prompt(label: str, default: str = "", *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    if secret:
        value = getpass.getpass(f"{label}: ").strip()
    else:
        value = input(f"{label}{suffix}: ").strip()
    return value or default


def _configure_http(args) -> int:
    base_url = (args.base_url or _prompt("券商网关 Base URL")).strip()
    if not base_url.startswith(("http://", "https://")):
        print("[FAIL] Base URL 必须以 http:// 或 https:// 开头")
        return 1
    account = (args.account or _prompt("资金账号（仅脱敏展示）")).strip()
    if not account:
        print("[FAIL] 资金账号不能为空")
        return 1
    token = (args.token or _prompt("HTTP Token（输入不回显）", secret=True)).strip()
    if not token:
        print("[FAIL] Token 不能为空（也可先留空，改用 keyring 已存值）")
        return 1
    timeout = float(args.timeout or 10.0)

    cfg = ExecutionConfig(
        live_enabled=True, broker_kind="http", account_id=account,
        http_base_url=base_url, http_token=token, timeout_seconds=timeout)
    if not args.no_probe:
        problems = check_http(cfg, timeout=timeout)
        if problems:
            print("[FAIL] 网关探测未通过：")
            for item in problems:
                print(f"  - {item}")
            if not args.force:
                print("未写入配置；确认地址/令牌后重试，或加 --force 跳过探测")
                return 1
            print("[WARN] --force：仍写入配置，但应用内探测可能失败")

    store = args.store
    token_in_env = ""
    if store == "file":
        token_in_env = token
        print("[WARN] Token 将写入 .env（已 gitignore）；建议优先使用 --store keyring")
    else:
        if store_keyring_token(token):
            print(f"[OK] Token 已存入 Windows 凭据管理器"
                  f"（{KEYRING_SERVICE}/{KEYRING_TOKEN_NAME}）")
        elif store == "keyring":
            print("[FAIL] 凭据管理器不可用；请改用 --store file（写入 .env）")
            return 1
        else:
            token_in_env = token
            print("[WARN] 凭据管理器不可用，Token 回退写入 .env（已 gitignore）")

    updates = {
        ENV_LIVE: "1",
        ENV_KIND: "http",
        ENV_ACCOUNT: account,
        ENV_HTTP_URL: base_url.rstrip("/"),
        ENV_HTTP_TOKEN: token_in_env,
        ENV_TIMEOUT: str(timeout),
    }
    upsert_env(Path(args.env_file), updates)
    print(f"[OK] 已写入 {args.env_file}")
    print(f"     broker=http  account={mask_account(account)}  base_url={base_url}")
    print("下一步：python scripts/check_broker_setup.py")
    return 0


def _configure_qmt(args) -> int:
    account = (args.account or _prompt("资金账号（仅脱敏展示）")).strip()
    if not account:
        print("[FAIL] 资金账号不能为空")
        return 1
    qmt_path = (args.qmt_path or _prompt("miniQMT userdata_mini 路径")).strip()
    if not qmt_path:
        print("[FAIL] miniQMT 路径不能为空")
        return 1
    if not Path(qmt_path).exists():
        print(f"[FAIL] 路径不存在: {qmt_path}")
        return 1

    cfg = ExecutionConfig(
        live_enabled=True, broker_kind="qmt", account_id=account,
        qmt_path=qmt_path, qmt_session_id=int(args.session_id or 0))
    problems = check_qmt(cfg)
    if problems:
        print("[WARN] 依赖检查未全部通过（可先写入配置，安装后自检）：")
        for item in problems:
            print(f"  - {item}")

    updates = {
        ENV_LIVE: "1",
        ENV_KIND: "qmt",
        ENV_ACCOUNT: account,
        ENV_QMT_PATH: qmt_path,
        "FINAGENT_EXECUTION__QMT_SESSION_ID": str(int(args.session_id or 0)),
    }
    upsert_env(Path(args.env_file), updates)
    print(f"[OK] 已写入 {args.env_file}")
    print(f"     broker=qmt  account={mask_account(account)}")
    print("下一步：安装 xtquant 后运行 python scripts/check_broker_setup.py")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="配置实盘券商通道（HTTP / QMT）")
    parser.add_argument("--kind", choices=("http", "qmt"), default="http")
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--base-url", default="")
    parser.add_argument("--account", default="")
    parser.add_argument("--token", default="", help="HTTP token（不回显提示；勿写入 shell 历史）")
    parser.add_argument("--qmt-path", default="")
    parser.add_argument("--session-id", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--store", choices=("auto", "keyring", "file"), default="auto")
    parser.add_argument("--no-probe", action="store_true", help="跳过 /health 探测")
    parser.add_argument("--force", action="store_true", help="探测失败仍写入配置")
    args = parser.parse_args(argv)

    if args.kind == "qmt":
        return _configure_qmt(args)
    return _configure_http(args)


if __name__ == "__main__":
    sys.exit(main())
