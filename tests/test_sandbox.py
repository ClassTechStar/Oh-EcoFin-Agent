"""参数沙箱：违例参数 100% 拒绝且回错误提示（验收标准：50 个畸形参数用例）。"""

from __future__ import annotations

import asyncio
import json

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.tools.registry import ToolRegistry, ToolSpec


class DemoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(..., description="6位代码")
    days: int = Field(120, ge=30, le=500)
    symbols: list[str] | None = Field(None, min_length=1, max_length=5)

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        # 与真实工具校验器同语义：zfill 归一 + 纯数字校验
        v = str(v).strip().zfill(6)
        if not v.isdigit() or len(v) != 6:
            raise ValueError(f"非法股票代码: {v}")
        return v

    @field_validator("symbols")
    @classmethod
    def _symbols(cls, v):
        if v is None:
            return v
        out = []
        for s in v:
            s = str(s).strip().zfill(6)
            if not s.isdigit() or len(s) != 6:
                raise ValueError(f"非法股票代码: {s}")
            out.append(s)
        return out


REG = ToolRegistry()


async def ok_handler(symbol: str, days: int = 120, symbols=None, **kw):
    return {"ok": True, "symbol": symbol, "days": days}


REG.register(ToolSpec("demo", "demo tool", DemoParams, ok_handler))


async def boom_handler(**kw):
    raise RuntimeError("x")


REG.register(ToolSpec("boom", "raising tool", DemoParams, boom_handler))


MALFORMED_CASES = [
    # 1-5: 非法 JSON
    "{bad json", "", "   ", "[1,2]", "null",
    # 6-10: 缺必填/类型错
    "{}", '{"days": 30}', '{"symbol": 600519}', '{"symbol": null}',
    '{"symbol": true, "days": 30}',
    # 11-15: 代码格式错
    '{"symbol": "60051x"}', '{"symbol": "6005199"}', '{"symbol": "abc123"}',
    '{"symbol": "60 0519"}', '{"symbol": "ABC"}',
    # 16-20: 范围钳制
    '{"symbol": "600519", "days": 10}', '{"symbol": "600519", "days": 29}',
    '{"symbol": "600519", "days": 501}', '{"symbol": "600519", "days": -1}',
    '{"symbol": "600519", "days": 0}',
    # 21-25: 列表越界
    '{"symbol": "600519", "symbols": []}',
    '{"symbol": "600519", "symbols": ["1","2","3","4","5","6"]}',
    '{"symbol": "600519", "symbols": "not-a-list"}',
    '{"symbol": "600519", "symbols": [12345]}',
    '{"symbol": "600519", "symbols": ["600519", "600519", "600519", "600519", "600519", "600519"]}',
    # 26-30: 未知字段（白名单外）
    '{"symbol": "600519", "hack": "rm -rf"}',
    '{"symbol": "600519", "extra": 1}',
    '{"symbol": "600519", "sql": "1; DROP TABLE users"}',
    '{"symbol": "600519", "days": 100, "unknown": {"nested": true}}',
    '{"foo": "bar"}',
    # 31-35: 注入与脚本
    '{"symbol": "600519 OR 1=1"}',
    '{"symbol": "\' OR \'1\'=\'1"}',
    '{"symbol": "<script>alert(1)</script>"}',
    '{"symbol": "${jndi:ldap://x}"}',
    '{"symbol": "600519", "days": "all"}',
    # 36-40: 嵌套畸形
    '{"symbol": {"$gt": ""}}', '{"symbol": ["600519"]}',
    '{"days": {"$ne": 1}, "symbol": "600519"}',
    '{"symbol": "600519", "days": [100]}',
    '{"symbols": {"$regex": ".*"}, "symbol": "600519"}',
    # 41-45: 数字类型混乱
    '{"symbol": "600519", "days": 30.5}', '{"symbol": "600519", "days": {"$ne": 1}}',
    '{"symbol": "600519", "days": true}', '{"symbol": "600519", "days": null}',
    '{"symbol": "NaN", "days": 30}',
    # 46-50: 超长与编码
    '{"symbol": "' + "6" * 300 + '", "days": 30}',
    '{"symbol": "600519", "days": 30, "symbols": ["600519' + "x" * 1000 + '"]}',
    '{"symbol": "👨‍👩‍👧‍👦", "days": 30}',
    '{"symbol": "600519\\u0000"}',
    '{"symbol": "600519", "days": 1e10}',
]


def test_sandbox_rejects_50_malformed():
    assert len(MALFORMED_CASES) == 50
    for i, raw in enumerate(MALFORMED_CASES):
        result = asyncio.run(REG.execute("demo", raw))
        assert result.get("ok") is False, f"用例 {i} 未拒绝: {raw[:60]!r}"
        assert "error" in result and "hint" in result


def test_sandbox_allows_valid():
    result = asyncio.run(REG.execute("demo", '{"symbol": "600519", "days": 120}'))
    assert result["ok"] is True
    assert result["symbol"] == "600519"


def test_sandbox_unknown_tool():
    result = asyncio.run(REG.execute("nope", "{}"))
    assert result["ok"] is False and "未知工具" in result["error"]


def test_sandbox_handler_exception_wrapped():
    result = asyncio.run(REG.execute("boom", '{"symbol": "600519"}'))
    assert result["ok"] is False
    assert "工具执行失败" in result["error"]


def test_sandbox_validate_only():
    ok, _ = REG.validate_only("demo", '{"symbol": "600519"}')
    assert ok
    ok, err = REG.validate_only("demo", '{"days": 5}')
    assert not ok and err


def test_schemas_openai_format():
    schemas = REG.schemas()
    assert all(s["type"] == "function" and "name" in s["function"] for s in schemas)
    # C 组消融：allowed 过滤
    filtered = REG.schemas(["demo"])
    assert len(filtered) == 1 and filtered[0]["function"]["name"] == "demo"


def test_parallel_safe_partition():
    REG2 = ToolRegistry()
    REG2.register(ToolSpec("a", "", DemoParams, ok_handler, parallel_safe=True))
    REG2.register(ToolSpec("b", "", DemoParams, ok_handler, parallel_safe=False))
    assert REG2.parallel_safe_names() == {"a"}


def test_duplicate_registration_rejected():
    REG3 = ToolRegistry()
    REG3.register(ToolSpec("dup", "", DemoParams, ok_handler))
    try:
        REG3.register(ToolSpec("dup", "", DemoParams, ok_handler))
        assert False, "应拒绝重复注册"
    except ValueError:
        pass


def test_arguments_string_to_model():
    """网关传来的 arguments 是 JSON 字符串 → 沙箱负责解析校验。"""
    raw = json.dumps({"symbol": "000001", "days": 500})
    ok, _ = REG.validate_only("demo", raw)
    assert ok
