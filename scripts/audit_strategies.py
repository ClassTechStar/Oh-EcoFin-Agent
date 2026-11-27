"""前视偏差截断检验门禁（CI 强制跑，零报警才允许合入）。

用法：
    python scripts/audit_strategies.py [--cut 0.7]

退出码：
    0 = 全部策略通过（跳过项不算失败，但会在输出中显式标注）
    1 = 至少一个策略检测到泄漏

覆盖策略：项目内置双均线参数网格的代表性组合（多标的、多周期），
数据使用确定性 fixture，保证 CI 零网络、零密钥、结果可复现。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.quant.audit import check_many, summarize  # noqa: E402
from backend.quant.fixtures import synthetic_ohlcv  # noqa: E402

#: (标的, 回测参数, 数据种子) —— 覆盖短/中/长周期与不同行情形态
CASES: list[tuple[str, dict, int]] = [
    ("600519", {"fast": 5, "slow": 20}, 101),
    ("000001", {"fast": 10, "slow": 60}, 102),
    ("300750", {"fast": 20, "slow": 120}, 103),
    ("601318", {"fast": 5, "slow": 60}, 104),
]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--cut", type=float, default=0.7, help="截断比例（默认 0.7）")
    parser.add_argument("--bars", type=int, default=900, help="合成数据根数")
    args = parser.parse_args()

    cases = [
        (synthetic_ohlcv(args.bars, seed=seed), symbol, params)
        for symbol, params, seed in CASES
    ]
    reports = check_many(cases, cut_ratio=args.cut)
    print(summarize(reports))

    leaked = [r for r in reports if not r.passed]
    if leaked:
        print(f"\n[FAIL] {len(leaked)} 个策略检测到前视偏差，禁止合入")
        sys.exit(1)
    print("\n[OK] 截断检验全部通过")
    sys.exit(0)


if __name__ == "__main__":
    main()
