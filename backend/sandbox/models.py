"""沙箱错误类型（P3）。"""

from __future__ import annotations


class SandboxError(ValueError):
    def __init__(self, message: str, *, code: str = "sandbox_error",
                 hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint


class SandboxDisabledError(SandboxError):
    def __init__(self) -> None:
        super().__init__(
            "自由代码沙箱未启用", code="sandbox_disabled",
            hint="设置 FINAGENT_SANDBOX__ENABLED=1 后在受控环境使用")


class SandboxValidationError(SandboxError):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message, code="sandbox_rejected", hint=hint)


class SandboxNotFoundError(SandboxError):
    def __init__(self, message: str):
        super().__init__(message, code="sandbox_not_found")
