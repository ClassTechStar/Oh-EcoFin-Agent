"""依赖许可证扫描（M9）。

分类：
  allowed    MIT/BSD/Apache/ISC/MPL/PSF 等宽松许可
  review     GPL/LGPL（分发前需复核）
  exception  已登记的已知例外（如 backtrader GPL，仅本地教学演示）
  conflict   AGPL/SSPL/Business Source/专有
默认仅 conflict 退出 1；--strict 时 review/unknown 也失败。
"""

from __future__ import annotations

import argparse
import json
from importlib.metadata import distributions

PERMISSIVE = ("MIT", "BSD", "APACHE", "ISC", "MPL", "PSF", "ZLIB", "CC0",
              "UNLICENSE", "0BSD", "PYTHON SOFTWARE FOUNDATION")
COPYLEFT = ("GPL", "LGPL")
DENIED = ("AGPL", "SSPL", "BUSINESS SOURCE", "PROPRIETARY", "COMMONS CLAUSE")

#: 已人工复核的例外：仅用于本地教学演示，不随二进制分发
KNOWN_EXCEPTIONS = {
    "backtrader": "GPLv3+；仅本地教学演示使用，商业分发前需替换或取得授权",
    "ecos": "GPLv3（cvxpy 可选求解器）；商业分发前需替换或取得授权",
}
#: 元数据缺失许可证的已知包
KNOWN_LICENSES = {
    "clr-loader": "MIT",
    "clr_loader": "MIT",
    "pythonnet": "MIT",
    "duckdb": "MIT",
}


def _license_text(dist) -> str:
    meta = dist.metadata
    parts = [
        meta.get("License-Expression") or "",
        meta.get("License") or "",
    ]
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License ::"):
            parts.append(classifier)
    return " ".join(" ".join(str(p).split()) for p in parts).strip()


def _classify(name: str, text: str, classifiers: list[str]) -> tuple[str, str]:
    if name in KNOWN_EXCEPTIONS:
        return "exception", KNOWN_EXCEPTIONS[name]
    if not text.strip() and name in KNOWN_LICENSES:
        return "allowed", KNOWN_LICENSES[name]
    joined = " ".join(classifiers).upper()
    if any(token in joined for token in DENIED):
        return "conflict", joined
    if any(token in joined for token in COPYLEFT):
        return "review", joined or text
    if any(token in joined for token in PERMISSIVE):
        return "allowed", joined
    upper = text.upper()
    # 长许可证正文可能包含 “GPL” 等第三方组件说明；先识别宽松模板
    if ("REDISTRIBUTION AND USE IN SOURCE AND BINARY FORMS" in upper
            or "PERMISSION IS HEREBY GRANTED" in upper
            or "APACHE LICENSE" in upper):
        return "allowed", text[:120]
    if len(text) <= 80:
        if any(token in upper for token in DENIED):
            return "conflict", text
        if any(token in upper for token in COPYLEFT):
            return "review", text
        if any(token in upper for token in PERMISSIVE):
            return "allowed", text
    return "unknown", text or "metadata missing"


def scan() -> dict:
    rows = []
    for dist in distributions():
        name = (dist.metadata.get("Name") or "").lower()
        if not name:
            continue
        text = _license_text(dist)
        classifiers = [c for c in (dist.metadata.get_all("Classifier") or [])
                       if c.startswith("License ::")]
        status, detail = _classify(name, text, classifiers)
        rows.append({"name": name, "version": dist.version,
                     "status": status, "license": detail[:200]})
    rows.sort(key=lambda row: (row["status"] != "conflict",
                               row["status"] != "review", row["name"]))
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"counts": counts, "packages": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description="依赖许可证扫描")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--strict", action="store_true", help="review/unknown 也失败")
    args = parser.parse_args()
    result = scan()
    conflicts = [p for p in result["packages"] if p["status"] == "conflict"]
    reviews = [p for p in result["packages"] if p["status"] == "review"]
    unknown = [p for p in result["packages"] if p["status"] == "unknown"]
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("许可证扫描:", result["counts"])
        for pkg in conflicts:
            print(f"  ❌ CONFLICT {pkg['name']} {pkg['version']}: {pkg['license']}")
        for pkg in reviews:
            print(f"  ⚠️ REVIEW   {pkg['name']} {pkg['version']}: {pkg['license']}")
        for pkg in unknown[:10]:
            print(f"  ❓ UNKNOWN  {pkg['name']} {pkg['version']}: {pkg['license']}")
    failed = bool(conflicts) or (args.strict and (reviews or unknown))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
