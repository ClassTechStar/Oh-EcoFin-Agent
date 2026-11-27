"""Windows Job Object 资源限额（P3）。

父进程把 worker 子进程加入 Job Object，实现：
- 进程内存上限（超限分配失败，而非拖垮整机）；
- 活动进程数上限（阻止 fork 炸弹式创建子进程）；
- 句柄关闭即杀进程（父进程异常退出也不遗留 worker）。

非 Windows 平台返回 None，由 worker 内的 resource.setrlimit 兜底。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def available() -> bool:
    return os.name == "nt"


def create_job(memory_bytes: int | None = None,
               max_processes: int = 4):
    """创建并配置 Job Object；失败返回 None（不阻塞沙箱执行）。"""
    if os.name != "nt":
        return None
    try:
        kernel32 = _kernel32()
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = _ExtendedLimitInformation()
        flags = (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                 | JOB_OBJECT_LIMIT_ACTIVE_PROCESS)
        info.BasicLimitInformation.ActiveProcessLimit = max(1, int(max_processes))
        if memory_bytes and memory_bytes > 0:
            flags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY
            info.ProcessMemoryLimit = int(memory_bytes)
        info.BasicLimitInformation.LimitFlags = flags
        ok = kernel32.SetInformationJobObject(
            handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            kernel32.CloseHandle(handle)
            return None
        return handle
    except (OSError, AttributeError):
        return None


def assign_process(job, process) -> bool:
    """把 subprocess.Popen 加入 Job Object。"""
    if os.name != "nt" or not job:
        return False
    raw = getattr(process, "_handle", None)
    if raw is None:
        return False
    try:
        return bool(_kernel32().AssignProcessToJobObject(job, int(raw)))
    except (OSError, TypeError, ValueError):
        return False


def peak_memory_bytes(job) -> int | None:
    """查询 Job 内进程峰值内存（字节）。"""
    if os.name != "nt" or not job:
        return None
    try:
        info = _ExtendedLimitInformation()
        returned = wintypes.DWORD(0)
        ok = _kernel32().QueryInformationJobObject(
            job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
            ctypes.sizeof(info), ctypes.byref(returned))
        if not ok:
            return None
        return int(info.PeakProcessMemoryUsed) or None
    except (OSError, AttributeError):
        return None


def terminate_job(job) -> None:
    if os.name != "nt" or not job:
        return
    try:
        _kernel32().TerminateJobObject(job, 1)
    except OSError:
        return


def close_job(job) -> None:
    """关闭句柄；KILL_ON_JOB_CLOSE 会连带杀掉仍存活的 worker。"""
    if os.name != "nt" or not job:
        return
    try:
        _kernel32().CloseHandle(job)
    except OSError:
        return
