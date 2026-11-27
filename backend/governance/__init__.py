"""治理层：append-only 哈希链账本（策略晋升、实盘开关等关键动作留痕）。"""

from backend.governance.ledger import GENESIS, Ledger

__all__ = ["Ledger", "GENESIS"]
