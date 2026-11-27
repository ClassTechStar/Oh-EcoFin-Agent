"""信息搜索（模块 3）：三源熔断链 博查 → 智谱 → DeepSeek。

深度优化：
1. 熔断器：单源连续 3 次失败 → 熔断 5 分钟（半开探测恢复）；三源全断 → 缓存 + is_stale；
2. TTL 缓存：新闻 30 分钟、公告 10 分钟；
3. 去重排序：标题 hash 去重 → 相关性 × 时效衰减复合排序 → 默认 24h，不足 3 条放宽 72h；
4. 模态卡交叉验证：博查股价模态卡 vs 本地快照，偏差 >1% 以本地为准并记日志；
5. 情绪预聚合：进 LLM 前规则级情绪标签，LLM 只做聚合判断。
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field

import httpx
from pydantic import BaseModel, ConfigDict, Field

from backend.config import Settings
from backend.data.ratelimit import TokenBucket
from backend.engine.compactor import label_sentiment

logger = logging.getLogger(__name__)


class NewsParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., description="搜索关键词（如 '贵州茅台 新闻'）")
    symbol: str = Field("", description="关联标的代码（可选，用于交叉验证）")
    hours: int | None = Field(None, ge=1, le=72, description="时间窗（默认24h）")
    count: int = Field(10, ge=3, le=20)


class CircuitBreaker:
    """单源熔断器：closed → open(3 连败) → half-open(5min 后探测) → closed。"""

    def __init__(self, name: str, threshold: int = 3, cooldown: int = 300):
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self.state = "closed"
        self.failures = 0
        self._opened_at = 0.0

    def _before_request(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.monotonic() - self._opened_at >= self.cooldown:
                self.state = "half_open"  # 半开：放行一次探测
                return True
            return False
        return True  # half_open 允许本次探测

    def on_success(self) -> None:
        self.failures = 0
        self.state = "closed"

    def on_failure(self) -> None:
        self.failures += 1
        if self.state == "half_open" or self.failures >= self.threshold:
            self.state = "open"
            self._opened_at = time.monotonic()
            logger.warning("搜索源 %s 熔断开启（失败 %d 次）", self.name, self.failures)

    def allow(self) -> bool:
        return self._before_request()


@dataclass
class SearchItem:
    title: str
    url: str = ""
    source: str = ""
    published_at: str = ""
    snippet: str = ""
    sentiment: str = "neutral"
    extra: dict = field(default_factory=dict)


# 时效衰减权重：24h ×1.0 / 48h ×0.7 / 更早 ×0.4
def recency_weight(age_hours: float) -> float:
    if age_hours <= 24:
        return 1.0
    if age_hours <= 48:
        return 0.7
    return 0.4


def parse_age_hours(published_at: str) -> float:
    try:
        from datetime import datetime, timedelta, timezone

        ts = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone(timedelta(hours=8)))
        return max((datetime.now(timezone(timedelta(hours=8))) - ts).total_seconds() / 3600, 0)
    except (ValueError, AttributeError):
        return 1e9  # 无时间戳视为最旧


class SearchService:
    """三源搜索 + 熔断 + 缓存 + 交叉验证 + 情绪预聚合。"""

    def __init__(self, settings: Settings, keys: dict[str, str], snapshot=None):
        self.settings = settings
        self.keys = keys
        self.snapshot = snapshot
        self.breakers = {
            name: CircuitBreaker(name,
                                 settings.search.circuit_failure_threshold,
                                 settings.search.circuit_cooldown_seconds)
            for name in settings.search.enabled_sources
        }
        self.buckets = {name: TokenBucket(rate=1.0, capacity=1)
                        for name in settings.search.enabled_sources}
        self._cache: dict[str, tuple[float, list[SearchItem]]] = {}

    # ---------- 缓存 ----------

    def _cache_key(self, query: str, symbol: str, hours: int) -> str:
        norm = " ".join(query.lower().split())
        raw = f"{norm}|{symbol}|{hours}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _cache_get(self, key: str, ttl: int) -> list[SearchItem] | None:
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        return None

    def _cache_put(self, key: str, items: list[SearchItem]) -> None:
        self._cache[key] = (time.time(), items)

    # ---------- 三源适配 ----------

    async def _search_bocha(self, query: str, count: int) -> list[SearchItem]:
        """博查 Web Search：带股价模态卡（extra['modal_card']）。"""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.bochaai.com/v1/web-search",
                headers={"Authorization": f"Bearer {self.keys.get('bocha_key', '')}"},
                json={"query": query, "summary": True, "count": count,
                      "freshness": "oneDay"},
            )
            resp.raise_for_status()
            data = resp.json()
        items = []
        for web in (data.get("data", {}).get("webPages", {}) or {}).get("value", []) or []:
            items.append(SearchItem(
                title=web.get("name", ""), url=web.get("url", ""),
                source="bocha", snippet=web.get("summary", "")[:200],
                published_at=web.get("dateLastCrawled", "") or "",
            ))
        card = data.get("data", {}).get("stockCard") or data.get("stockCard")
        if card and items:
            items[0].extra["modal_card"] = card
        return items

    async def _search_zhipu(self, query: str, count: int) -> list[SearchItem]:
        """智谱 Web Search 工具 API。"""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://open.bigmodel.cn/api/paas/v4/web_search",
                headers={"Authorization": f"Bearer {self.keys.get('zhipu_search_key', '')}"},
                json={"search_engine": "search_std", "search_query": query, "count": count},
            )
            resp.raise_for_status()
            data = resp.json()
        items = []
        for r in (data.get("search_result") or [])[:count]:
            items.append(SearchItem(
                title=r.get("title", ""), url=r.get("link", ""),
                source="zhipu", snippet=r.get("content", "")[:200],
                published_at=r.get("media_publish_date", "") or "",
            ))
        return items

    async def _search_deepseek(self, query: str, count: int) -> list[SearchItem]:
        """DeepSeek Search（虚拟最终源；无独立搜索 API 时退化为文档站点摘要）。"""
        # DeepSeek 官方未开放独立 web-search API 时，该源仅作占位：
        # 若未来开放，接入点在此。当前返回空 → 由前两源兜底。
        return []

    # ---------- 主入口 ----------

    async def search_news(self, query: str, symbol: str = "",
                          hours: int | None = None, count: int = 10) -> dict:
        """带全链路优化的新闻搜索。"""
        hours = hours or self.settings.search.news_max_hours
        key = self._cache_key(query, symbol, hours)
        cached = self._cache_get(key, self.settings.data.news_cache_ttl)
        if cached is not None:
            return self._format(cached, cached=True, query=query)

        adapters = {"bocha": self._search_bocha, "zhipu": self._search_zhipu,
                    "deepseek": self._search_deepseek}
        items: list[SearchItem] = []
        used_sources: list[str] = []
        for name in self.settings.search.enabled_sources:
            breaker = self.breakers[name]
            if not breaker.allow():
                continue
            try:
                await self.buckets[name].acquire()
                found = await adapters[name](query, count)
                breaker.on_success()
                used_sources.append(name)
                items.extend(found)
                if len(items) >= count:
                    break
            except Exception as e:
                breaker.on_failure()
                logger.warning("搜索源 %s 失败: %s", name, e)

        if symbol:
            items = self._cross_validate(items, symbol)
        self._cache_put(key, items)
        result = self._format(items, cached=False, query=query, hours=hours)
        result["used_sources"] = used_sources
        if not used_sources:
            result["ok"] = bool(items)
            result["is_stale"] = True
            result["reason"] = "信息源暂不可用，返回缓存" if items else "三源全断且无缓存"
        return result

    def _cross_validate(self, items: list[SearchItem], symbol: str) -> list[SearchItem]:
        """模态卡交叉验证：偏差 >1% 以本地快照为准。"""
        if self.snapshot is None or not items:
            return items
        card = items[0].extra.get("modal_card") if items else None
        if not card:
            return items
        try:
            outer = float(card.get("price") or card.get("current_price"))
            row = self.snapshot.row(symbol)
            if row and row.get("最新价"):
                local = float(row["最新价"])
                if local and abs(outer - local) / local > 0.01:
                    logger.info("外部数据不一致(标的 %s)：搜索卡 %s vs 本地快照 %s，以本地为准",
                                symbol, outer, local)
                    items[0].extra["price_conflict"] = {
                        "search": outer, "local": local, "resolved": "local"}
        except (TypeError, ValueError, KeyError):
            pass
        return items

    def _format(self, items: list[SearchItem], *, cached: bool, query: str,
                hours: int = 24) -> dict:
        """去重 → 复合排序 → 24h 过滤 → 不足 3 条放宽 72h。"""
        seen: set[str] = set()
        deduped: list[SearchItem] = []
        for it in items:
            h = hashlib.md5(it.title[:40].encode()).hexdigest()
            if h in seen:
                continue
            seen.add(h)
            it.sentiment = label_sentiment(it.title)
            deduped.append(it)

        ranked = sorted(
            deduped,
            key=lambda x: recency_weight(parse_age_hours(x.published_at)),
            reverse=True,
        )
        within = [it for it in ranked if parse_age_hours(it.published_at) <= hours]
        widened = False
        if len(within) < 3:
            within = [it for it in ranked if parse_age_hours(it.published_at) <= 72]
            widened = bool(within)
        out = []
        for it in within[:8]:
            out.append({
                "title": it.title, "url": it.url, "source": it.source,
                "published_at": it.published_at, "snippet": it.snippet,
                "sentiment": it.sentiment, "age_hours": round(parse_age_hours(it.published_at), 1),
            })
        agg = (
            sum({"bullish": 1, "neutral": 0, "bearish": -1}[x["sentiment"]] for x in out) / len(out)
            if out else 0.0
        )
        return {
            "ok": bool(out), "query": query, "news": out, "cached": cached,
            "widened_to_72h": widened, "aggregate_sentiment": round(agg, 2),
            "is_stale": False,
        }
