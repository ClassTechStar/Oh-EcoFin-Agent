"""LLMGateway：双模型统一网关（GLM-5.3-Flash primary / DeepSeek-flash economy）。

深度优化点：
1. tier 路由策略（成本第一道闸）；
2. 失败降级链：primary 超时/429/5xx → economy 重试 1 次 → error 事件（绝不静默吞错）；
3. 成本计量：usage 写 llm_calls 表 + 日累计成本熔断（DAILY_COST_CAP）；
4. OpenAI 兼容适配：tool_calls[].function.arguments 是 JSON 字符串 → 统一容错解析；
5. 流式优先：thinking_delta / message_delta 双通道一次实现，两模型复用。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from openai import APIError, AsyncOpenAI, RateLimitError

from backend.config import Settings

logger = logging.getLogger(__name__)

StreamCallback = Callable[[str, str], Awaitable[None]]  # (channel, delta_text)


class CostFuseOpenError(Exception):
    """日成本熔断开启：拒绝新调用。"""


class RecoverableLLMError(Exception):
    """可恢复错误：前端展示重试按钮。"""


class MalformedToolArgsError(Exception):
    """模型返回的 arguments 非法 JSON：交沙箱拒绝路径。"""


# 元/M tokens 计价口径（保守估算；官方价以官网为准，成本看板仅供演示参考）
PRICE_TABLE_CNY_PER_MTOK = {
    "glm-5.3-flash": {"in": 0.5, "cached_in": 0.1, "out": 2.8},
    "deepseek-flash": {"in": 1.0, "cached_in": 0.02, "out": 2.0},
}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # 原始 JSON 字符串（沙箱负责校验）
    parallel_safe: bool = True


@dataclass
class LLMResult:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage_in: int = 0
    usage_out: int = 0
    usage_cached: int = 0
    model: str = ""
    tier: str = ""
    cost_cents: int = 0
    latency_ms: int = 0
    finish_reason: str = ""


def estimate_cost_cents(model: str, tokens_in: int, tokens_out: int, cached: int) -> int:
    """按 PRICE_TABLE 估算本调用成本（分）。"""
    price = PRICE_TABLE_CNY_PER_MTOK.get(model, {"in": 1.0, "cached_in": 0.5, "out": 2.0})
    plain_in = max(tokens_in - cached, 0)
    cost_cny = (
        plain_in / 1e6 * price["in"]
        + cached / 1e6 * price["cached_in"]
        + tokens_out / 1e6 * price["out"]
    )
    return int(round(cost_cny * 100))


def parse_tool_calls(raw_calls: list[dict]) -> list[ToolCall]:
    """统一容错解析 tool_calls[].function.arguments（JSON 字符串）。"""
    calls = []
    for tc in raw_calls or []:
        fn = tc.get("function", {}) or {}
        calls.append(
            ToolCall(
                id=tc.get("id", ""),
                name=fn.get("name", ""),
                arguments=fn.get("arguments", "{}"),
            )
        )
    return calls


class CostLedger:
    """日成本熔断器：达到 DAILY_COST_CAP 拒绝新调用（答辩前夜超支保险丝）。"""

    def __init__(self, adb, cap_cny: float):
        self.adb = adb
        self.cap_cents = int(cap_cny * 100)

    async def today_cents(self) -> int:
        return await self.adb.cost_today_cents()

    async def guard(self, tier: str) -> None:
        """超日上限 → CostFuseOpenError。economy 缓存价近零，可豁免一部分，但简单起见统一管控。"""
        spent = await self.today_cents()
        if spent >= self.cap_cents:
            raise CostFuseOpenError(
                f"今日 LLM 成本已达上限 {self.cap_cents / 100:.2f} 元（已花 {spent / 100:.2f} 元），"
                "新调用被熔断拒绝。可在 config.yaml 调整 llm.daily_cost_cap_cny。"
            )

    async def record(self, session_id: str, model: str, tier: str, role: str,
                     tokens_in: int, tokens_out: int, cached: int,
                     cost_cents: int, latency_ms: int) -> None:
        await self.adb.insert_llm_call(
            session_id=session_id, model=model, tier=tier, role=role,
            tokens_in=tokens_in, tokens_out=tokens_out, cached_tokens=cached,
            cost_cents=cost_cents, latency_ms=latency_ms,
        )


class LLMGateway:
    TIERS = {"primary": "primary", "economy": "economy"}

    def __init__(self, settings: Settings, cost_ledger: CostLedger,
                 glm_key: str, deepseek_key: str):
        cfg = settings.llm
        self.models = {
            "primary": cfg.primary_model,
            "economy": cfg.economy_model,
        }
        self.clients = {
            "primary": AsyncOpenAI(
                base_url=cfg.primary_base_url, api_key=glm_key or "missing",
                timeout=cfg.timeout_seconds, max_retries=0,
            ),
            "economy": AsyncOpenAI(
                base_url=cfg.economy_base_url, api_key=deepseek_key or "missing",
                timeout=cfg.timeout_seconds, max_retries=0,
            ),
        }
        self.cost_ledger = cost_ledger
        self.timeout = cfg.timeout_seconds

    async def chat(self, messages: list[dict], tools: list[dict] | None = None,
                   tier: str = "primary", *, session_id: str = "", role: str = "chat",
                   stream_cb: StreamCallback | None = None,
                   temperature: float = 0.3) -> LLMResult:
        """统一入口：tier 路由 + 成本熔断 + 降级链。"""
        await self.cost_ledger.guard(tier)
        try:
            return await self._call(tier, messages, tools, stream_cb, session_id, role, temperature)
        except CostFuseOpenError:
            raise
        except (TimeoutError, RateLimitError, APIError, RecoverableLLMError) as e:
            if tier == "primary":
                # 降级链：仅 primary 降级到 economy 重试 1 次
                logger.warning("primary 调用失败(%s)，降级 economy 重试", type(e).__name__)
                await self.cost_ledger.guard("economy")
                try:
                    return await self._call("economy", messages, tools, stream_cb,
                                            session_id, role, temperature)
                except CostFuseOpenError:
                    raise
                except Exception as e2:
                    raise RecoverableLLMError(
                        f"双模型均调用失败：primary={type(e).__name__}, economy={type(e2).__name__}"
                    ) from e2
            raise RecoverableLLMError(f"模型调用失败：{type(e).__name__}: {e}") from e

    async def _call(self, tier: str, messages: list[dict], tools: list[dict] | None,
                    stream_cb: StreamCallback | None, session_id: str, role: str,
                    temperature: float) -> LLMResult:
        client = self.clients[tier]
        model = self.models[tier]
        kwargs: dict[str, Any] = dict(
            model=model, messages=messages, stream=True, temperature=temperature,
            stream_options={"include_usage": True},
        )
        if tools:
            kwargs["tools"] = tools
        start = time.perf_counter()
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_raw: dict[int, dict] = {}
        finish_reason = ""
        usage: Any = None

        try:
            stream = await client.chat.completions.create(**kwargs)
            async for chunk in stream:
                if chunk.usage:
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                finish_reason = choice.finish_reason or finish_reason
                delta = choice.delta
                if delta is None:
                    continue
                # 思考通道：GLM/DeepSeek 的 reasoning_content 独立透传
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning and stream_cb:
                    reasoning_parts.append(reasoning)
                    await stream_cb("thinking_delta", reasoning)
                if delta.content:
                    text_parts.append(delta.content)
                    if stream_cb:
                        await stream_cb("message_delta", delta.content)
                for idx, tc in enumerate(delta.tool_calls or []):
                    slot = tool_calls_raw.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments
        except (TimeoutError, RateLimitError, APIError):
            raise
        except Exception as e:  # 网络/协议层异常 → 统一可恢复
            raise RecoverableLLMError(f"流式响应中断：{type(e).__name__}: {e}") from e

        latency_ms = int((time.perf_counter() - start) * 1000)
        tokens_in = getattr(usage, "prompt_tokens", 0) or 0
        tokens_out = getattr(usage, "completion_tokens", 0) or 0
        cached = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached = getattr(details, "cached_tokens", 0) or 0
        cost_cents = estimate_cost_cents(model, tokens_in, tokens_out, cached)

        if session_id:
            try:
                await self.cost_ledger.record(
                    session_id, model, tier, role, tokens_in, tokens_out,
                    cached, cost_cents, latency_ms,
                )
            except Exception:
                logger.exception("llm_calls 落库失败")

        return LLMResult(
            text="".join(text_parts),
            reasoning="".join(reasoning_parts),
            tool_calls=parse_tool_calls([tool_calls_raw[i] for i in sorted(tool_calls_raw)]),
            usage_in=tokens_in, usage_out=tokens_out, usage_cached=cached,
            model=model, tier=tier, cost_cents=cost_cents, latency_ms=latency_ms,
            finish_reason=finish_reason,
        )

    async def simple(self, messages: list[dict], tier: str = "economy",
                     *, session_id: str = "", role: str = "aux",
                     temperature: float = 0.3, max_tokens: int | None = None) -> str:
        """无工具的一次性调用（摘要/标题/反思等模板化场景）。"""
        await self.cost_ledger.guard(tier)
        client = self.clients[tier]
        model = self.models[tier]
        start = time.perf_counter()
        kwargs: dict[str, Any] = dict(model=model, messages=messages,
                                      temperature=temperature, stream=False)
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        try:
            resp = await client.chat.completions.create(**kwargs)
        except (TimeoutError, RateLimitError, APIError) as e:
            if tier == "primary":
                return await self.simple(messages, "economy", session_id=session_id,
                                         role=role, temperature=temperature,
                                         max_tokens=max_tokens)
            raise RecoverableLLMError(f"模型调用失败：{type(e).__name__}") from e
        latency_ms = int((time.perf_counter() - start) * 1000)
        usage = getattr(resp, "usage", None)
        tokens_in = getattr(usage, "prompt_tokens", 0) or 0
        tokens_out = getattr(usage, "completion_tokens", 0) or 0
        cached = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached = getattr(details, "cached_tokens", 0) or 0
        cost_cents = estimate_cost_cents(model, tokens_in, tokens_out, cached)
        if session_id:
            try:
                await self.cost_ledger.record(session_id, model, tier, role,
                                              tokens_in, tokens_out, cached,
                                              cost_cents, latency_ms)
            except Exception:
                logger.exception("llm_calls 落库失败")
        return resp.choices[0].message.content or ""
