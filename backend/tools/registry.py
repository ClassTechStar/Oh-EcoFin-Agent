"""工具注册与参数沙箱。

沙箱语义：违例参数不抛异常，而是作为工具结果返回错误提示——模型收到错误后
通常一轮内自纠；同一调用失败自动重试 1 次为上限（由 ReAct 层执行）。
参数白名单承担类型与范围钳制（pydantic 模型声明）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)


@dataclass
class ToolSpec:
    name: str
    description: str
    params: type[BaseModel]        # pydantic 模型 = 白名单 + 类型 + 范围
    handler: Callable[..., Awaitable[dict]]
    compressor: str = "raw"        # kline | snapshot | news | financial | backtest | raw
    parallel_safe: bool = True     # 快照读类可并行；搜索类串行防限速
    side_effect: str = "none"      # none | session | workspace | external
    confirmation: str = "none"     # none | explicit_request
    timeout_seconds: int = 60


def pydantic_to_openai_schema(name: str, description: str, model: type[BaseModel]) -> dict:
    """pydantic 模型 → OpenAI tools Schema（两模型共用一份）。"""
    schema = model.model_json_schema()
    schema.pop("title", None)
    schema["additionalProperties"] = False  # 白名单外字段显式拒绝
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema,
        },
    }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具重复注册: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self, allowed: list[str] | None = None) -> list[dict]:
        """OpenAI tools 格式；allowed 为消融实验 C 组（无工具）预留。"""
        specs = [s for n, s in self._tools.items() if allowed is None or n in allowed]
        return [pydantic_to_openai_schema(s.name, s.description, s.params) for s in specs]

    def parallel_safe_names(self) -> set[str]:
        return {n for n, s in self._tools.items() if s.parallel_safe}

    def side_effect_map(self) -> dict[str, dict]:
        return {
            name: {"side_effect": spec.side_effect,
                   "confirmation": spec.confirmation,
                   "timeout_seconds": spec.timeout_seconds}
            for name, spec in self._tools.items()
        }

    async def execute(self, name: str, raw_args: str, *,
                      allow_side_effects: bool = True) -> dict:
        """沙箱执行：校验失败返回错误提示（不抛异常）；handler 异常同样包装。

        `allow_side_effects=False` 时，带 `confirmation=explicit_request` 的
        副作用工具不执行，返回 requires_confirmation 错误（由编排层决定是否放行）。
        """
        spec = self._tools.get(name)
        if spec is None:
            return {"ok": False, "error": f"未知工具: {name}",
                    "hint": "请对照工具说明修正后重试"}
        if spec.confirmation == "explicit_request" and not allow_side_effects:
            return {
                "ok": False,
                "error": f"工具 {name} 需要用户明确授权",
                "requires_confirmation": True,
                "side_effect": spec.side_effect,
                "hint": "请先向用户确认操作意图（交易/生成报告），得到明确指令后再调用",
            }
        try:
            args = spec.params.model_validate_json(raw_args or "{}")
        except ValidationError as e:
            return {"ok": False, "error": f"参数校验失败: {e.errors()[:3]}",
                    "hint": "请对照工具说明修正参数后重试"}
        except ValueError:
            return {"ok": False, "error": "参数不是合法 JSON",
                    "hint": "arguments 必须是 JSON 字符串"}
        try:
            result = await spec.handler(**args.model_dump())
        except TypeError as e:
            return {"ok": False, "error": f"参数不匹配: {e}",
                    "hint": "请对照工具说明修正参数后重试"}
        except Exception as e:
            logger.warning("工具 %s 执行异常: %s", name, e, exc_info=True)
            return {"ok": False, "error": f"工具执行失败: {type(e).__name__}: {e}",
                    "hint": "可修正参数重试一次，或换用其他工具"}
        if isinstance(result, dict) and "ok" not in result:
            result = {"ok": True, **result}
        return result

    def validate_only(self, name: str, raw_args: str) -> tuple[bool, str]:
        """仅校验不执行（沙箱测试与重试逻辑用）。"""
        spec = self._tools.get(name)
        if spec is None:
            return False, f"未知工具: {name}"
        try:
            spec.params.model_validate_json(raw_args or "{}")
            return True, ""
        except ValidationError as e:
            return False, str(e.errors()[:3])
        except ValueError:
            return False, "参数不是合法 JSON"
