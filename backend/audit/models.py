"""审计契约（M7）。"""

from __future__ import annotations


class AuditError(ValueError):
    def __init__(self, message: str, *, code: str = "audit_error",
                 hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint
