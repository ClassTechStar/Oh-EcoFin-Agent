"""MCP stdio 适配（M9）：把只读数据工具暴露给 MCP 客户端。

用法（Claude Desktop / Cursor 的 MCP 配置）：
  command: <项目>/.venv/Scripts/python.exe
  args: ["<项目>/scripts/mcp_server.py"]
环境变量同后端（FINAGENT_DB_PATH 等）；只暴露只读工具，不暴露交易/报告写入。

协议：JSON-RPC 2.0，逐行 JSON（MCP stdio transport）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import load_settings  # noqa: E402
from backend.mcp.adapter import McpAdapter  # noqa: E402

SERVER_INFO = {"name": "oh-ecofin-agent", "version": "4.0.0"}
PROTOCOL_VERSION = "2024-11-05"


def _write(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


async def serve(adapter: McpAdapter) -> None:
    tools = adapter.list_tools()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            _write(_error(None, -32700, "Parse error"))
            continue
        request_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}
        is_notification = "id" not in request
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_INFO,
                }
            elif method in ("notifications/initialized", "notifications/cancelled"):
                continue
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tools}
            elif method == "tools/call":
                name = params.get("name", "")
                arguments = params.get("arguments") or {}
                tool_result = await adapter.call(name, arguments)
                result = adapter.text_result(tool_result)
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            elif method == "shutdown":
                _write({"jsonrpc": "2.0", "id": request_id, "result": None})
                return
            else:
                if is_notification:
                    continue
                _write(_error(request_id, -32601, f"Method not found: {method}"))
                continue
            if not is_notification:
                _write({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as e:  # 工具异常返回 JSON-RPC error，不污染 stdout 日志
            if not is_notification:
                _write(_error(request_id, -32603, f"{type(e).__name__}: {e}"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Oh-EcoFin-Agent MCP stdio server")
    parser.add_argument("--db", default="", help="覆盖 SQLite 元数据库路径")
    args = parser.parse_args()
    settings = load_settings()
    if args.db:
        settings.db_path = args.db
    adapter = McpAdapter(settings)
    asyncio.run(serve(adapter))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
