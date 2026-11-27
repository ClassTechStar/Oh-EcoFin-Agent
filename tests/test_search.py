"""搜索三源熔断：状态机转换 / TTL 缓存 / 去重 / 时效放宽 / 情绪预聚合。"""

from __future__ import annotations

import asyncio

from backend.tools.search import CircuitBreaker, SearchItem, SearchService
from backend.config import Settings


def test_circuit_breaker_state_machine():
    cb = CircuitBreaker("bocha", threshold=3, cooldown=1)
    assert cb.state == "closed" and cb.allow()
    cb.on_failure(); cb.on_failure()
    assert cb.state == "closed" and cb.allow()  # 未达阈值
    cb.on_failure()
    assert cb.state == "open" and not cb.allow()  # 连续 3 败 → 熔断
    import time

    time.sleep(1.05)
    assert cb.allow() and cb.state == "half_open"  # 半开探测
    cb.on_success()
    assert cb.state == "closed"


def test_circuit_breaker_half_open_failure_reopens():
    cb = CircuitBreaker("zhipu", threshold=3, cooldown=0.05)
    for _ in range(3):
        cb.on_failure()
    import time

    time.sleep(0.2)  # 留足 Windows 计时器余量，避免 wall-clock 抖动导致偶发失败
    assert cb.allow()
    cb.on_failure()
    assert cb.state == "open"  # 半开失败 → 立即再熔断


def make_svc(snapshot=None) -> SearchService:
    s = Settings()
    return SearchService(s, {"bocha_key": "k", "zhipu_search_key": "k"}, snapshot)


async def test_three_source_fallback_and_dedup():
    svc = make_svc()

    async def fake_bocha(query, count):
        return [SearchItem(title=f"利好新闻{i}", published_at="2026-11-27T09:00:00",
                           source="bocha") for i in range(5)]

    async def fail_zhipu(query, count):
        raise RuntimeError("down")

    svc._search_bocha = fake_bocha
    svc._search_zhipu = fail_zhipu
    out = await svc.search_news("600519 新闻")
    assert out["ok"] is True
    assert "bocha" in out["used_sources"] and "zhipu" not in out["used_sources"]
    assert out["aggregate_sentiment"] == 1.0


async def test_cache_ttl_hit():
    svc = make_svc()
    calls = []

    async def counter(query, count):
        calls.append(1)
        return [SearchItem(title=f"新闻{i}", source="bocha") for i in range(3)]

    svc._search_bocha = counter
    svc.breakers["bocha"].on_success()
    r1 = await svc.search_news("cached query")
    r2 = await svc.search_news("cached query")
    assert r1["cached"] is False and r2["cached"] is True
    assert len(calls) == 1  # TTL 缓存命中即省钱省延迟


async def test_all_sources_down_returns_stale_or_empty():
    svc = make_svc()

    async def fail(query, count):
        raise RuntimeError("down")

    for name in ("bocha", "zhipu", "deepseek"):
        svc._search_deepseek = fail
        svc._search_zhipu = fail
        svc._search_bocha = fail
    out = await svc.search_news("dead query")
    assert out["ok"] is False
    assert out.get("reason") and ("暂不可用" in out["reason"] or "全断" in out["reason"])


async def test_dedup_10_to_at_most_8():
    """去重排序：重复标题被合并。"""
    svc = make_svc()
    items = [SearchItem(title=f"标题{i}", published_at=f"2026-11-27T0{i}:00:00")
             for i in range(1, 9)]
    items += items[:3]  # 3 条重复
    out = svc._format(items, cached=False, query="q")
    assert out["ok"] is True
    titles = [x["title"] for x in out["news"]]
    assert len(titles) == len(set(titles))
    assert len(titles) <= 8


async def test_widen_to_72h_when_under_3():
    svc = make_svc()
    old_items = [SearchItem(title=f"旧闻{i}", published_at="2026-11-20T00:00:00")
                 for i in range(5)]  # 7 天前
    out = svc._format(old_items, cached=False, query="q", hours=24)
    assert out["widened_to_72h"] is False  # 72h 也没有 → 空结果
    # 40 小时前 → 24h 内不足 3 条 → 放宽到 72h
    mid_items = [SearchItem(title=f"昨夜{i}", published_at="2026-11-25T18:00:00")
                 for i in range(4)]
    out2 = svc._format(mid_items, cached=False, query="q", hours=24)
    assert out2["widened_to_72h"] is True and out2["ok"] is True


async def test_recency_decay_ranking():
    from backend.tools.search import recency_weight

    assert recency_weight(10) == 1.0
    assert recency_weight(40) == 0.7
    assert recency_weight(80) == 0.4


async def test_cross_validation_price_conflict():
    """博查模态卡与快照偏差 >1% → 以本地为准并记日志。"""
    class FakeSnapshot:
        def row(self, symbol):
            return {"最新价": 100.0, "名称": "X"}

    svc = make_svc(FakeSnapshot())
    item = SearchItem(title="模态卡新闻", source="bocha")
    item.extra["modal_card"] = {"price": 105.0}  # 偏差 5%
    items = svc._cross_validate([item], "600519")
    assert items[0].extra["price_conflict"]["resolved"] == "local"
    # 偏差 <1% 不标记
    item2 = SearchItem(title="一致新闻", source="bocha")
    item2.extra["modal_card"] = {"price": 100.5}
    items2 = svc._cross_validate([item2], "600519")
    assert "price_conflict" not in items2[0].extra
