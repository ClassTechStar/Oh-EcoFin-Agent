"""自由代码沙箱服务（P3）：校验 → 独立 worker → 限额/超时 → 审计落库。

安全边界（按强度排序）：
1. 独立子进程 + `python -I`，不共享应用内存/密钥；
2. 清空环境变量（仅保留系统必需项）；
3. Windows Job Object / POSIX rlimit 限制内存、进程数、CPU 与文件大小；
4. 用户代码只拿到 DataFrame 与 save_output，无文件路径、无 import os/socket；
5. AST 校验仅作防御层，不是安全边界的全部。

残余风险：Windows 下无法在进程级真正断网；如需强隔离请把 worker 放进
容器/虚拟机（见 Dockerfile 的 sandbox profile 说明）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.config import Settings
from backend.datasets.service import DatasetService
from backend.db import Database
from backend.sandbox import winjob
from backend.sandbox.models import (
    SandboxDisabledError,
    SandboxError,
    SandboxNotFoundError,
    SandboxValidationError,
)
from backend.sandbox.validator import validate_code

_RUN_ID = re.compile(r"^sbx_[0-9a-f]{12}$")
_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_KEEP_ENV = {
    "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "COMSPEC", "PATHEXT",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
}
_MAX_DATASETS = 5
_MAX_STDOUT_BYTES = 64 * 1024

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


class SandboxService:
    def __init__(self, db: Database, settings: Settings,
                 datasets: DatasetService):
        self.db = db
        self.settings = settings
        self.datasets = datasets
        self.root = Path(settings.sandbox.dir)
        self.run_root = self.root / "runs"
        self.worker = Path(__file__).with_name("worker.py")
        self._lock = threading.Lock()

    # ---------- 状态 ----------

    @property
    def enabled(self) -> bool:
        return bool(self.settings.sandbox.enabled)

    def limits(self) -> dict:
        cfg = self.settings.sandbox
        return {
            "timeout_seconds": int(cfg.timeout_seconds),
            "memory_mb": int(cfg.memory_mb),
            "max_output_mb": int(cfg.max_output_mb),
            "max_files": int(cfg.max_files),
            "max_code_chars": int(cfg.max_code_chars),
        }

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "dir": str(self.root),
            "worker": self.worker.name,
            "python": sys.executable,
            "runs": self.count(),
            "limits": self.limits(),
            "isolation": {
                "process": "subprocess (python -I)",
                "env": "allowlist（不继承 FINAGENT_* 密钥）",
                "network": "Python 层拒绝 os/socket/subprocess/requests 导入",
                "memory": "Job Object" if winjob.available() else "resource.setrlimit",
                "filesystem": "仅输出目录可写，数据集只读加载",
                "ast": "防御层（非唯一安全边界）",
            },
        }

    # ---------- 执行 ----------

    def run(self, code: str, dataset_ids: list[str] | None = None) -> dict:
        if not self.enabled:
            raise SandboxDisabledError()
        validated = validate_code(
            code, max_chars=int(self.settings.sandbox.max_code_chars))
        datasets = self._resolve_datasets(dataset_ids or [])

        run_id = "sbx_" + hashlib.sha256(
            f"{time.time_ns()}:{os.getpid()}".encode()).hexdigest()[:12]
        run_dir = self.run_root / run_id
        files_dir = run_dir / "files"
        files_dir.mkdir(parents=True, exist_ok=False)

        limits = self.limits()
        max_output_bytes = int(limits["max_output_mb"]) * 1024 * 1024
        request = {
            "code": validated.code,
            "datasets": datasets,
            "output_dir": str(files_dir),
            "timeout_seconds": limits["timeout_seconds"],
            "memory_mb": limits["memory_mb"],
            "max_output_bytes": max_output_bytes,
            "max_stdout_bytes": _MAX_STDOUT_BYTES,
            "max_files": limits["max_files"],
        }
        request_path = run_dir / "request.json"
        request_path.write_text(
            json.dumps(request, ensure_ascii=False), encoding="utf-8")

        code_hash = hashlib.sha256(validated.code.encode("utf-8")).hexdigest()
        self._start(run_id, datasets, code_hash, limits)

        started = time.perf_counter()
        try:
            with self._lock:  # 串行执行：避免并发放大内存/CPU 压力
                outcome = self._execute(run_dir, request_path, limits)
        except Exception as e:  # noqa: BLE001 - 启动失败也要留下审计记录
            logger.exception("沙箱 worker 启动失败")
            outcome = {
                "status": "failed", "error": f"worker 启动失败: {e}",
                "exit_code": None, "stdout": "", "stderr": "",
                "peak_memory": None, "payload": {},
            }
        duration_ms = int((time.perf_counter() - started) * 1000)

        files = self._collect_files(files_dir)
        total_bytes = sum(f["bytes"] for f in files)
        if (len(files) > int(limits["max_files"])
                or total_bytes > max_output_bytes):
            self._clear_files(files_dir)
            files = []
            outcome["status"] = "failed"
            outcome["error"] = "输出超过限额，已清理本次输出"
            outcome["exit_code"] = outcome.get("exit_code")

        payload = outcome.get("payload") or {}
        result = {
            "run_id": run_id,
            "status": outcome["status"],
            "exit_code": outcome.get("exit_code"),
            "duration_ms": duration_ms,
            "stdout": outcome.get("stdout", ""),
            "stderr": outcome.get("stderr", ""),
            "result": payload.get("result"),
            "error": outcome.get("error", ""),
            "traceback": payload.get("traceback", ""),
            "files": files,
            "datasets": [
                {k: d[k] for k in ("dataset_id", "name", "table_name", "sha256")}
                for d in datasets
            ],
            "limits": limits,
            "code_hash": code_hash,
            "peak_memory_mb": (round(outcome["peak_memory"] / 1024 / 1024, 1)
                               if outcome.get("peak_memory") else None),
            "created_at": _now(),
        }
        self._finish(result)
        return result

    def _execute(self, run_dir: Path, request_path: Path,
                 limits: dict) -> dict:
        timeout = max(1, int(limits["timeout_seconds"]))
        memory_bytes = max(64, int(limits["memory_mb"])) * 1024 * 1024
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

        proc = subprocess.Popen(
            [sys.executable, "-I", str(self.worker), str(request_path)],
            cwd=str(run_dir), env=self._clean_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=creationflags,
        )
        job = winjob.create_job(memory_bytes, max_processes=4)
        if job:
            winjob.assign_process(job, proc)

        timed_out = False
        try:
            stdout_b, stderr_b = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            winjob.terminate_job(job)
            try:
                proc.kill()
            except OSError:
                pass
            try:
                stdout_b, stderr_b = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout_b, stderr_b = b"", b""
        finally:
            peak = winjob.peak_memory_bytes(job)
            winjob.close_job(job)

        stdout = self._decode(stdout_b)[:_MAX_STDOUT_BYTES]
        stderr = self._decode(stderr_b)[:_MAX_STDOUT_BYTES]
        payload = self._read_result(run_dir)
        if payload:
            # worker 捕获的用户 print 输出与进程原生输出合并
            stdout = (stdout + str(payload.get("stdout") or ""))[:_MAX_STDOUT_BYTES]
            stderr = (str(payload.get("stderr") or "") + stderr)[:_MAX_STDOUT_BYTES]

        if timed_out:
            return {
                "status": "timeout",
                "error": f"执行超时（>{timeout}s），worker 已被终止",
                "exit_code": proc.returncode, "stdout": stdout,
                "stderr": stderr, "peak_memory": peak, "payload": payload,
            }
        if payload is None:
            return {
                "status": "failed",
                "error": "worker 未产出结果文件（可能被资源限额终止）",
                "exit_code": proc.returncode, "stdout": stdout,
                "stderr": stderr, "peak_memory": peak, "payload": {},
            }
        if payload.get("status") != "ok":
            return {
                "status": "error",
                "error": str(payload.get("error") or "用户代码执行失败"),
                "exit_code": proc.returncode, "stdout": stdout,
                "stderr": stderr, "peak_memory": peak, "payload": payload,
            }
        if proc.returncode not in (0, None):
            return {
                "status": "failed",
                "error": f"worker 异常退出（exit={proc.returncode}）",
                "exit_code": proc.returncode, "stdout": stdout,
                "stderr": stderr, "peak_memory": peak, "payload": payload,
            }
        return {
            "status": "ok", "error": "", "exit_code": proc.returncode,
            "stdout": stdout, "stderr": stderr, "peak_memory": peak,
            "payload": payload,
        }

    @staticmethod
    def _decode(raw: bytes | None) -> str:
        return (raw or b"").decode("utf-8", errors="replace")

    @staticmethod
    def _clean_env() -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if k.upper() in _KEEP_ENV}
        env.update({
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            # 单线程数值库：避免 OpenBLAS 线程池把地址空间打满
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        })
        return env

    @staticmethod
    def _read_result(run_dir: Path) -> dict | None:
        path = run_dir / "result.json"
        if not path.is_file():
            return None
        if path.stat().st_size > 8 * 1024 * 1024:
            return {"status": "error", "error": "结果文件超过解析上限"}
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return {"status": "error", "error": "结果文件不是合法 JSON"}
        return data if isinstance(data, dict) else None

    def _resolve_datasets(self, dataset_ids: list[str]) -> list[dict]:
        wanted = list(dict.fromkeys(str(x).strip() for x in dataset_ids if str(x).strip()))
        if len(wanted) > _MAX_DATASETS:
            raise SandboxValidationError(
                f"一次最多加载 {_MAX_DATASETS} 个数据集",
                hint="先用 dataset_list 选择必要的数据集")
        payloads = []
        if wanted:
            for dataset_id in wanted:
                payload = self.datasets.store.get_version(dataset_id)
                if not payload:
                    raise SandboxValidationError(
                        f"数据集不存在: {dataset_id}",
                        hint="先用 dataset_list 确认 dataset_id")
                payloads.append(payload)
        else:
            payloads = self.datasets.store.latest_versions()[:_MAX_DATASETS]

        resolved = []
        for payload in payloads:
            path = Path(payload["path"])
            if not path.is_file():
                raise SandboxError(
                    f"数据集快照缺失: {payload['name']}",
                    code="sandbox_dataset_missing",
                    hint="重新导入该数据集后再试")
            resolved.append({
                "dataset_id": payload["dataset_id"],
                "name": payload["name"],
                "table_name": payload["table_name"],
                "sha256": payload["sha256"],
                "path": str(path),
            })
        return resolved

    # ---------- 输出文件 ----------

    @staticmethod
    def _collect_files(files_dir: Path) -> list[dict]:
        if not files_dir.is_dir():
            return []
        files = []
        for path in sorted(files_dir.iterdir()):
            if not path.is_file() or not _OUTPUT_NAME.match(path.name):
                continue
            try:
                files.append({"name": path.name, "bytes": path.stat().st_size})
            except OSError:
                continue
        return files

    @staticmethod
    def _clear_files(files_dir: Path) -> None:
        for path in files_dir.iterdir():
            if path.is_file():
                path.unlink(missing_ok=True)

    def output_path(self, run_id: str, name: str) -> Path:
        record = self.get(run_id)
        if not _OUTPUT_NAME.match(str(name or "")) or ".." in str(name):
            raise SandboxNotFoundError(f"输出文件不存在: {name}")
        if name not in {f["name"] for f in record["files"]}:
            raise SandboxNotFoundError(f"输出文件不存在: {name}")
        files_dir = (self.run_root / run_id / "files").resolve()
        target = (files_dir / name).resolve()
        if target.parent != files_dir or not target.is_file():
            raise SandboxNotFoundError(f"输出文件不存在: {name}")
        return target

    # ---------- 审计查询 ----------

    def _start(self, run_id: str, datasets: list[dict], code_hash: str,
               limits: dict) -> None:
        """先落 running 记录：worker 异常退出/进程崩溃时仍能看到启动痕迹。"""
        self.db.execute(
            "INSERT INTO sandbox_runs(id, dataset_id, status, code_hash,"
            " exit_code, duration_ms, stdout, stderr, result_json, files_json,"
            " limits_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, ",".join(d["dataset_id"] for d in datasets),
             "running", code_hash, None, 0, "", "", "{}",
             json.dumps({"files": [],
                         "datasets": [
                             {k: d[k] for k in
                              ("dataset_id", "name", "table_name", "sha256")}
                             for d in datasets]},
                        ensure_ascii=False),
             json.dumps(limits, ensure_ascii=False), _now()))

    def _finish(self, record: dict) -> None:
        result_json = json.dumps({
            "result": record["result"],
            "traceback": record["traceback"],
        }, ensure_ascii=False, default=str)
        self.db.execute(
            "UPDATE sandbox_runs SET status=?, exit_code=?, duration_ms=?,"
            " stdout=?, stderr=?, result_json=?, files_json=? WHERE id=?",
            (record["status"], record["exit_code"], record["duration_ms"],
             record["stdout"], record["stderr"], result_json,
             json.dumps({"files": record["files"],
                         "peak_memory_mb": record["peak_memory_mb"],
                         "datasets": record["datasets"]},
                        ensure_ascii=False),
             record["run_id"]))

    def get(self, run_id: str) -> dict:
        if not _RUN_ID.match(str(run_id or "")):
            raise SandboxNotFoundError(f"沙箱运行不存在: {run_id}")
        row = self.db.query_one(
            "SELECT * FROM sandbox_runs WHERE id=?", (run_id,))
        if row is None:
            raise SandboxNotFoundError(f"沙箱运行不存在: {run_id}")
        return self._payload(row)

    def recent(self, limit: int = 50) -> list[dict]:
        rows = self.db.query(
            "SELECT id, dataset_id, status, code_hash, exit_code, duration_ms,"
            " files_json, limits_json, created_at FROM sandbox_runs"
            " ORDER BY created_at DESC, id DESC LIMIT ?", (int(limit),))
        return [self._summary(row) for row in rows]

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS c FROM sandbox_runs")
        return int(row["c"]) if row else 0

    @staticmethod
    def _loads(raw: str, default):
        try:
            value = json.loads(raw or "")
        except (TypeError, json.JSONDecodeError):
            return default
        return value if isinstance(value, type(default)) else default

    def _summary(self, row: dict) -> dict:
        extra = self._loads(row.get("files_json") or "", {})
        return {
            "run_id": row["id"],
            "dataset_id": row.get("dataset_id", ""),
            "status": row.get("status", ""),
            "code_hash": row.get("code_hash", ""),
            "exit_code": row.get("exit_code"),
            "duration_ms": row.get("duration_ms", 0),
            "files": extra.get("files", []),
            "datasets": extra.get("datasets", []),
            "peak_memory_mb": extra.get("peak_memory_mb"),
            "limits": self._loads(row.get("limits_json") or "", {}),
            "created_at": row.get("created_at", ""),
        }

    def _payload(self, row: dict) -> dict:
        result = self._loads(row.get("result_json") or "", {})
        return {
            **self._summary(row),
            "stdout": row.get("stdout", ""),
            "stderr": row.get("stderr", ""),
            "result": result.get("result"),
            "traceback": result.get("traceback", ""),
        }
