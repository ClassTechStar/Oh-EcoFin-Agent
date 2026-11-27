"""独立沙箱 worker（P3）：由父进程以 `python -I worker.py request.json` 启动。

约束：
1. 不使用 backend 包，避免继承应用状态/密钥；
2. 只通过 request.json 接收代码、只读数据集路径、输出目录与限额；
3. 用户代码仅拿到安全 builtins、预加载的 DataFrame 与 save_output；
4. stdout/stderr 有字符上限，结果经 JSON 化后写 result.json，父进程负责审计。
"""

from __future__ import annotations

import builtins
import importlib
import json
import math
import os
import re
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

ALLOWED_MODULES = (
    "collections", "datetime", "decimal", "functools", "itertools",
    "json", "math", "numpy", "pandas", "re", "statistics",
)

SAFE_BUILTINS = (
    "abs", "all", "any", "ascii", "bin", "bool", "bytes", "callable", "chr",
    "dict", "divmod", "enumerate", "filter", "float", "format", "frozenset",
    "hash", "hex", "int", "isinstance", "issubclass", "iter", "len", "list",
    "map", "max", "min", "next", "oct", "ord", "pow", "range", "repr",
    "reversed", "round", "set", "slice", "sorted", "str", "sum", "tuple",
    "zip",
)
SAFE_EXCEPTIONS = (
    "ArithmeticError", "AttributeError", "Exception", "IndexError",
    "KeyError", "LookupError", "NameError", "RuntimeError", "StopIteration",
    "TypeError", "ValueError", "ZeroDivisionError",
)

_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_OUTPUT_SUFFIXES = {".csv", ".json", ".md", ".txt"}
_MAX_FILES = 20
_MAX_RESULT_CHARS = 200_000
_MAX_TRACEBACK_CHARS = 8_000


class CappedWriter:
    """有上限的文本缓冲：超出后停止写入并标记 truncated。"""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self._parts: list[str] = []
        self._size = 0
        self.truncated = False

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("沙箱输出流不支持 fileno")

    def write(self, text: str) -> int:
        value = str(text)
        if self._size >= self.limit:
            self.truncated = True
            return len(value)
        room = self.limit - self._size
        chunk = value[:room]
        self._parts.append(chunk)
        self._size += len(chunk)
        if len(chunk) < len(value):
            self.truncated = True
        return len(value)

    def flush(self) -> None:
        return None

    def getvalue(self) -> str:
        return "".join(self._parts)


def _safe_import(name: str, globals_: Any = None, locals_: Any = None,
                 fromlist: tuple = (), level: int = 0) -> Any:
    root = str(name or "").split(".", 1)[0]
    if level or root not in ALLOWED_MODULES:
        raise ImportError(f"沙箱禁止导入模块: {name}")
    return importlib.import_module(name)


def _limit_resources(memory_mb: int, timeout_seconds: int,
                     max_output_bytes: int) -> None:
    """POSIX 资源限额；Windows 由父进程 Job Object 负责。"""
    if os.name == "nt":
        return
    try:
        import resource
    except ImportError:  # pragma: no cover - 非 POSIX
        return
    limits = [
        # 地址空间按 3×RSS 估算：DuckDB/pandas 会预留虚拟内存，真实内存
        # 上限仍以容器 cgroup 或父进程 Job Object 为准。
        (resource.RLIMIT_AS, max(256, memory_mb) * 3 * 1024 * 1024),
        (resource.RLIMIT_CPU, max(1, int(math.ceil(timeout_seconds)) + 1)),
        (resource.RLIMIT_FSIZE, max_output_bytes + 4 * 1024 * 1024),
        (resource.RLIMIT_NOFILE, 64),
    ]
    for key, value in limits:
        try:
            soft, hard = resource.getrlimit(key)
            if hard != resource.RLIM_INFINITY:
                value = min(value, hard)
            resource.setrlimit(key, (value, value))
        except (ValueError, OSError):
            continue


def _jsonable(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if depth >= 4:
        return repr(value)[:200]
    if isinstance(value, dict):
        return {str(k)[:80]: _jsonable(v, depth + 1)
                for k, v in list(value.items())[:200]}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v, depth + 1) for v in list(value)[:200]]
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return _jsonable(value.item(), depth + 1)
    except ImportError:  # pragma: no cover - numpy 已随 pandas 安装
        pass
    try:
        import pandas as pd

        if isinstance(value, pd.DataFrame):
            head = value.head(200)
            return {
                "type": "dataframe",
                "columns": [str(c) for c in head.columns],
                "rows": [_jsonable(row, depth + 1) for row in head.to_dict("records")],
                "total_rows": int(len(value)),
                "truncated": len(value) > len(head),
            }
        if isinstance(value, pd.Series):
            head = value.head(200)
            return {
                "type": "series",
                "name": str(value.name) if value.name is not None else "",
                "values": [_jsonable(v, depth + 1) for v in head.tolist()],
                "total_rows": int(len(value)),
                "truncated": len(value) > len(head),
            }
    except ImportError:  # pragma: no cover
        pass
    if hasattr(value, "item") and callable(getattr(value, "item", None)):
        try:
            return _jsonable(value.item(), depth + 1)
        except (ValueError, TypeError):
            pass
    return repr(value)[:500]


def _load_datasets(entries: list[dict]) -> dict[str, Any]:
    import duckdb

    loaded: dict[str, Any] = {}
    for entry in entries:
        path = Path(entry["path"])
        if not path.is_file():
            raise FileNotFoundError(f"数据集快照缺失: {entry.get('name', path.name)}")
        conn = duckdb.connect(config={"threads": 1})
        try:
            frame = conn.execute(
                "SELECT * FROM read_parquet(?)", [str(path)]).df()
        finally:
            conn.close()
        for key in (entry.get("name"), entry.get("table_name"),
                    entry.get("dataset_id")):
            if key and key not in loaded:
                loaded[str(key)] = frame
    return loaded


def _save_output_factory(output_dir: Path, max_bytes: int, max_files: int):
    state = {"count": 0, "bytes": 0}

    def save_output(filename: str, content: Any) -> dict:
        name = str(filename or "").strip()
        if not _OUTPUT_NAME.match(name) or ".." in name:
            raise ValueError("输出文件名非法：仅允许字母/数字/._-，且不能包含路径")
        if Path(name).suffix.lower() not in _OUTPUT_SUFFIXES:
            raise ValueError(f"输出类型不支持：仅允许 {sorted(_OUTPUT_SUFFIXES)}")
        if state["count"] >= max_files:
            raise ValueError(f"输出文件数超过限额: {max_files}")
        if isinstance(content, bytes):
            payload = content
        elif isinstance(content, str):
            payload = content.encode("utf-8")
        else:
            payload = json.dumps(_jsonable(content), ensure_ascii=False,
                                 indent=2).encode("utf-8")
        if state["bytes"] + len(payload) > max_bytes:
            raise ValueError(
                f"输出总量超过限额: {max_bytes // (1024 * 1024)} MB")
        target = output_dir / name
        target.write_bytes(payload)
        state["count"] += 1
        state["bytes"] += len(payload)
        return {"ok": True, "file": name, "bytes": len(payload)}

    return save_output, state


def _build_builtins(print_fn) -> dict:
    table = {name: getattr(builtins, name) for name in SAFE_BUILTINS}
    table.update({name: getattr(builtins, name) for name in SAFE_EXCEPTIONS})
    table["True"], table["False"], table["None"] = True, False, None
    table["__import__"] = _safe_import
    table["__build_class__"] = builtins.__build_class__
    table["print"] = print_fn
    return table


def _write_result(path: Path, payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > _MAX_RESULT_CHARS:
        payload = {**payload, "result": None, "result_truncated": True}
        text = json.dumps(payload, ensure_ascii=False, default=str)[:_MAX_RESULT_CHARS]
    path.write_text(text, encoding="utf-8")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: worker.py request.json", file=sys.stderr)
        return 2
    request_path = Path(argv[1])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    run_dir = request_path.parent
    output_dir = Path(request["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    max_output_bytes = int(request.get("max_output_bytes") or 10 * 1024 * 1024)
    max_stdout_bytes = int(request.get("max_stdout_bytes") or 64 * 1024)
    max_files = int(request.get("max_files") or _MAX_FILES)
    timeout_seconds = int(request.get("timeout_seconds") or 10)
    memory_mb = int(request.get("memory_mb") or 1024)

    _limit_resources(memory_mb, timeout_seconds, max_output_bytes)

    stdout = CappedWriter(max_stdout_bytes)
    stderr = CappedWriter(max_stdout_bytes)
    payload: dict[str, Any] = {
        "status": "ok", "result": None, "error": "",
        "stdout": "", "stderr": "", "files": [],
        "stdout_truncated": False, "stderr_truncated": False,
    }

    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            import numpy as np
            import pandas as pd

            datasets = _load_datasets(request.get("datasets") or [])
            save_output, state = _save_output_factory(
                output_dir, max_output_bytes, max_files)
            print_fn = lambda *args, **kwargs: builtins.print(  # noqa: E731
                *args, **{**kwargs, "file": stdout})

            globals_: dict[str, Any] = {
                "__builtins__": _build_builtins(print_fn),
                "__name__": "__sandbox__",
                "datasets": datasets,
                "pd": pd,
                "np": np,
                "save_output": save_output,
                "result": None,
            }
            code = str(request.get("code") or "")
            compiled = compile(code, "<sandbox>", "exec")
            exec(compiled, globals_)  # noqa: S102 - 沙箱 worker 的设计目的
            payload["result"] = _jsonable(globals_.get("result"))
            payload["files"] = [
                {"name": p.name, "bytes": p.stat().st_size}
                for p in sorted(output_dir.iterdir()) if p.is_file()
            ][: _MAX_FILES]
        except BaseException as e:  # noqa: BLE001 - 用户代码任意异常都要回传
            payload["status"] = "error"
            payload["error"] = f"{type(e).__name__}: {e}"[:2000]
            payload["traceback"] = traceback.format_exc()[-_MAX_TRACEBACK_CHARS:]
            payload["files"] = [
                {"name": p.name, "bytes": p.stat().st_size}
                for p in sorted(output_dir.iterdir()) if p.is_file()
            ][: _MAX_FILES]

    payload["stdout"] = stdout.getvalue()
    payload["stderr"] = stderr.getvalue()
    payload["stdout_truncated"] = stdout.truncated
    payload["stderr_truncated"] = stderr.truncated
    _write_result(run_dir / "result.json", payload)
    return 0 if payload["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
