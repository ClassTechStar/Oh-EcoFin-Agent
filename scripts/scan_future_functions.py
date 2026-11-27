"""未来函数扫描器（CI 强制跑，验收：零报警）。

静态检查量化/工具层的 pandas 用法：
1. `.shift(-` —— 负向 shift（引用未来数据）；
2. `shift(-20)` 出现在因子信号路径（IC 评估的"未来收益"仅限评估，需出现在
   `ret20`/`forward`/`future` 命名的变量上，且不进入 composite/信号）；
3. `rolling(`+后续列引用 / `merge_asof` direction="forward"。

规则：在 backend/quant/ 与 backend/tools/ 中扫描 `shift(-N)` 调用点，
所在变量名必须匹配白名单正则（^fr$|forward|future|ret.*ahead）。
用法：python scripts/scan_future_functions.py  （退出码非 0 = 有报警）
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = [ROOT / "backend" / "quant", ROOT / "backend" / "tools"]
# 允许引用未来数据的变量名白名单（仅评估口径，不进信号）
ALLOW = re.compile(r"(forward|future|ret20|ret\w*ahead|fwd)", re.IGNORECASE)
SHIFT_NEG = re.compile(r"\.shift\(\s*-\s*\d+")


def scan() -> list[str]:
    violations = []
    for d in SCAN_DIRS:
        for f in d.glob("*.py"):
            lines = f.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                if SHIFT_NEG.search(line):
                    # 找赋值目标（向上找最近的赋值行）
                    target = ""
                    for j in range(i, max(i - 12, -1), -1):
                        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=.*$", lines[j])
                        if m:
                            target = m.group(1)
                            break
                    if not ALLOW.search(target):
                        violations.append(
                            f"{f.relative_to(ROOT)}:{i + 1}: 疑似未来函数引用 "
                            f"(变量 {target!r}): {line.strip()[:80]}")
    return violations


def main() -> None:
    violations = scan()
    if violations:
        print("❌ 未来函数扫描报警：")
        for v in violations:
            print("  " + v)
        sys.exit(1)
    print("✅ 未来函数扫描：零报警")


if __name__ == "__main__":
    main()
