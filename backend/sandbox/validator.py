"""自由代码静态防御层（P3）。

定位：AST 检查只是“防呆 + 降低误用面”，不是安全边界；真正的隔离依赖
独立 worker 子进程、清空环境变量、Job Object/rlimit 资源限额与默认断网。
因此规则宁可严格，也不承担“识别所有逃逸”的职责。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from backend.sandbox.models import SandboxValidationError

#: 允许导入的模块根名（worker 内已预加载/可从标准库安全导入）
ALLOWED_IMPORTS = frozenset({
    "collections", "datetime", "decimal", "functools", "itertools",
    "json", "math", "numpy", "pandas", "re", "statistics",
})

#: 直接禁用的内置名（调用或引用都拒绝）
DENIED_NAMES = frozenset({
    "__builtins__", "__import__", "__loader__", "__package__", "__spec__",
    "breakpoint", "compile", "delattr", "dir", "eval", "exec", "exit",
    "getattr", "globals", "help", "input", "locals", "memoryview", "object",
    "open", "quit", "setattr", "type", "vars",
})

#: 禁用的属性名（覆盖文件/网络/序列化/绘图落盘等常见副作用）
DENIED_ATTRS = frozenset({
    "read_clipboard", "read_csv", "read_excel", "read_feather", "read_fwf",
    "read_gbq", "read_hdf", "read_html", "read_json", "read_orc",
    "read_parquet", "read_pickle", "read_sas", "read_spss", "read_sql",
    "read_sql_query", "read_sql_table", "read_stata", "read_table",
    "read_xml", "save", "savefig", "savetxt", "to_clipboard", "to_csv",
    "to_excel", "to_feather", "to_gbq", "to_hdf", "to_html", "to_json",
    "to_latex", "to_markdown", "to_orc", "to_parquet", "to_pickle",
    "to_sql", "to_stata", "to_xml", "tofile", "fromfile", "load",
    "loads", "dump", "dumps",
})

DENIED_NODES = (
    ast.AsyncFor, ast.AsyncFunctionDef, ast.AsyncWith, ast.Await,
)

_PATH_SUFFIXES = (
    ".db", ".dll", ".parquet", ".pkl", ".py", ".so", ".xlsx", ".zip",
)
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


@dataclass
class ValidationResult:
    code: str
    imports: list[str] = field(default_factory=list)
    nodes: int = 0


def _looks_like_path(value: str) -> bool:
    if not value or len(value) > 300:
        return False
    if ".." in value or "://" in value or "\\" in value:
        return True
    if value[0] in "/~":
        return True
    if _WINDOWS_DRIVE.match(value):
        return True
    lowered = value.lower()
    return any(lowered.endswith(suffix) for suffix in _PATH_SUFFIXES)


def _root(name: str) -> str:
    return str(name or "").split(".", 1)[0]


def _fail(message: str, hint: str = "") -> None:
    raise SandboxValidationError(message, hint=hint)


def validate_code(code: str, *, max_chars: int = 20000,
                  max_nodes: int = 20000) -> ValidationResult:
    """校验代码并返回可执行源码；任何风险命中都抛 SandboxValidationError。"""
    text = str(code or "")
    if not text.strip():
        _fail("代码为空")
    if len(text) > max_chars:
        _fail(f"代码长度超过限额: {len(text)} > {max_chars}",
              hint="精简代码，或调大 FINAGENT_SANDBOX__MAX_CODE_CHARS")
    try:
        tree = ast.parse(text, filename="<sandbox>", mode="exec")
    except SyntaxError as e:
        lineno = e.lineno or 0
        _fail(f"语法错误: {e.msg}（第 {lineno} 行）", hint="修正语法后重试")

    imports: set[str] = set()
    nodes = 0
    for node in ast.walk(tree):
        nodes += 1
        if nodes > max_nodes:
            _fail(f"AST 节点数超过限额: > {max_nodes}", hint="精简代码后重试")

        if isinstance(node, DENIED_NODES):
            _fail(f"沙箱不允许语法: {type(node).__name__}",
                  hint="移除 async/await 后重试")

        if isinstance(node, ast.Import):
            for alias in node.names:
                root = _root(alias.name)
                if root not in ALLOWED_IMPORTS:
                    _fail(f"沙箱禁止导入模块: {alias.name}",
                          hint=f"允许: {', '.join(sorted(ALLOWED_IMPORTS))}")
                imports.add(root)
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                _fail("沙箱禁止相对导入")
            root = _root(node.module)
            if root not in ALLOWED_IMPORTS:
                _fail(f"沙箱禁止导入模块: {node.module}",
                      hint=f"允许: {', '.join(sorted(ALLOWED_IMPORTS))}")
            for alias in node.names:
                if alias.name in DENIED_ATTRS or alias.name.startswith("_"):
                    _fail(f"沙箱禁止导入符号: {node.module}.{alias.name}")
            imports.add(root)
        elif isinstance(node, ast.Name):
            if node.id in DENIED_NAMES:
                _fail(f"沙箱禁止使用名称: {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                _fail(f"沙箱禁止访问下划线属性: {node.attr}",
                      hint="不要访问 __class__/__globals__ 等内部属性")
            if node.attr in DENIED_ATTRS:
                _fail(f"沙箱禁止调用/访问属性: {node.attr}",
                      hint="读写文件/网络/序列化请改用 save_output 或 datasets")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in DENIED_NAMES:
                _fail(f"沙箱禁止调用: {func.id}")
            if isinstance(func, ast.Attribute) and func.attr in DENIED_ATTRS:
                _fail(f"沙箱禁止调用: {func.attr}",
                      hint="读写文件请使用 save_output(filename, content)")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _looks_like_path(node.value):
                _fail(f"沙箱禁止路径字符串: {node.value[:80]}",
                      hint="数据已通过 datasets 注入，输出请用 save_output")

    return ValidationResult(code=text, imports=sorted(imports), nodes=nodes)
