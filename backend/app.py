"""FastAPI 应用总装：SSE v2 富组件协议 + 三种部署形态 + 安全纪律。

关键约束：智能体推理与全部金融计算都在后端进程完成，API Key 永不出后端进程；
前端只消费 SSE 事件流。默认绑定 127.0.0.1；局域网模式需显式开启 + 随机 token。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from backend.config import Settings, load_settings
from backend.data.calendar import TradeCalendar
from backend.data.precompute import PrecomputeService
from backend.data.ratelimit import TokenBucket
from backend.data.snapshot import SnapshotService
from backend.db import AsyncDatabase, Database
from backend.engine.askuser import AskUserManager
from backend.engine.debate import DebateOrchestrator
from backend.engine.events import StreamHub
from backend.engine.gateway import CostLedger, LLMGateway
from backend.engine.prompts import DISCLAIMER
from backend.engine.react import ChatTask, ReActEngine
from backend.execution import (
    CapitalGate,
    ExecutionStore,
    GateChain,
    OrderRouter,
    PaperBroker,
    SuitabilityGate,
)
from backend.execution.brokers import build_live_gateway, mask_account
from backend.execution.gateway import GatewayError
from backend.execution.service import (
    InvalidRiskLevelError,
    QuoteUnavailableError,
    account_payload,
    positions_prices,
    submit_paper_order,
    symbol_quote,
    validate_risk_level,
)
from backend.memory.decisions import DecisionStore
from backend.memory.reflections import history_block
from backend.memory.store import SessionStore
from backend.memory.summarize import (
    build_summary_prompt,
    estimate_tokens,
    fact_readback_verify,
    keep_recent,
    parse_summary_json,
    render_history,
    should_summarize,
)
from backend.quant.audit_store import AuditStore
from backend.quant.compliance import EQUITY_CAPS, review_advice
from backend.quant.portfolio import PortfolioOptimizer
from backend.security.keyring_loader import load_key, missing_required_keys
from backend.security.logmask import SensitiveFilter, mask_result_for_storage
from backend.security.tokens import generate_token, verify_token
from backend.signals import SignalStore
from backend.signals.service import SignalDataError, preview_signals
from backend.tools.advisor import (
    RISK_QUESTIONS,
    AdvisorTools,
    AskRiskParams,
    PortfolioParams,
    profile_for_level,
    score_answers,
)
from backend.tools.market import KlineParams, MarketTools, QuoteParams
from backend.tools.quant import BacktestParams, FactorParams, QuantTools, WalkForwardParams
from backend.tools.registry import ToolRegistry, ToolSpec
from backend.tools.research import CorrelationParams, ResearchReport, ResearchTools, SymbolParams
from backend.tools.search import NewsParams, SearchService
from backend.tools.trading import (
    AccountToolParams,
    PaperOrderToolParams,
    SignalToolParams,
    TradingTools,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
for _h in logging.root.handlers:
    _h.addFilter(SensitiveFilter())
logger = logging.getLogger(__name__)

DEBATE_KEYWORDS = ("值得买", "能不能买", "研判", "分析一下", "怎么看", "多空",
                   "值得投资", "要不要买", "对比")
ADVISOR_KEYWORDS = ("测评", "风险等级", "投顾", "组合", "配置")


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    session_id: str = ""
    mode: str = Field("auto", description="auto|chat|debate|advisor")


class ResumeRequest(BaseModel):
    session_id: str
    qid: str
    choice: str


class PaperOrderRequest(BaseModel):
    """仿真下单请求：价格由服务端按最新快照决定（禁止前端伪造成交价）。"""

    symbol: str
    side: Literal["buy", "sell"]
    qty: int = Field(..., ge=1, le=1_000_000)
    risk_level: str = "R3"
    strategy_id: str = "manual"
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = Field(None, gt=0)


def _norm_symbol(raw: str) -> str:
    sym = str(raw).strip().zfill(6)
    if not re.fullmatch(r"\d{6}", sym):
        raise HTTPException(status_code=400, detail=f"非法股票代码: {raw}")
    return sym


class AdvisorAssessRequest(BaseModel):
    answers: list[dict] = Field(..., min_length=5, max_length=5)


class AdvisorPortfolioRequest(BaseModel):
    symbols: list[str] = Field(..., min_length=3, max_length=10)
    level: str = "R3"
    industry: dict[str, str] = Field(default_factory=dict)
    statement: str = Field("", max_length=2000)


class StatementCheckRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)


class AppContext:
    """全局单例容器（app.state.ctx）。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.adb = AsyncDatabase(self.db)
        self.calendar = TradeCalendar()
        self.hub = StreamHub(settings.server.replay_buffer_size)
        self.snapshot = SnapshotService(settings, self.calendar)
        self.precompute = PrecomputeService()
        self.snapshot.set_precompute(self.precompute.precompute)
        self.market = MarketTools(
            snapshot=self.snapshot,
            bucket=TokenBucket(settings.data.rate_limit_rps, settings.data.rate_limit_burst),
            kline_cache_ttl=settings.data.kline_cache_ttl,
            cassette_dir=settings.cassette_dir,
            demo_mode=settings.app.demo_mode,
        )
        self.sessions = SessionStore(self.db)
        self.decisions = DecisionStore(self.db)
        self.optimizer = PortfolioOptimizer()
        # M7 信号 / M10 仿真执行（进程内单账户，重启即重置——教学演示语义）
        self.signal_store = SignalStore(self.db)
        self.broker = PaperBroker()
        self.exec_store = ExecutionStore(self.db)
        self.router = OrderRouter(
            GateChain([SuitabilityGate(), CapitalGate()]),
            self.broker, self.exec_store)
        self.paper_open_equity = float(self.broker.initial_cash)
        self.live_connected = False
        self.live_gateway = None
        try:
            self.live_gateway = build_live_gateway(settings)
        except Exception:
            logger.exception("实盘通道装配失败（保持关闭）")
        self.registry = ToolRegistry()
        self.advisor = None
        self.search = None
        self.gateway: LLMGateway | None = None
        self.react: ReActEngine | None = None
        self.debate: DebateOrchestrator | None = None
        self.ask_manager: AskUserManager | None = None
        self.lan_token: str = ""

    def wire(self) -> None:
        """装配工具注册表 + 引擎（启动时调用）。"""
        s = self.settings
        glm_key = load_key("glm_key")
        deepseek_key = load_key("deepseek_key")
        self.search = SearchService(s, {
            "bocha_key": load_key("bocha_key"),
            "zhipu_search_key": load_key("zhipu_search_key"),
        }, self.snapshot)

        research = ResearchTools(self.market, self.search, self.precompute)
        advisor = AdvisorTools(self.market, self.optimizer)
        self.advisor = advisor
        quant = QuantTools(self.market, store=AuditStore(self.db))
        trading = TradingTools(
            self.market, self.signal_store, self.router, self.broker,
            self.snapshot, lambda: self.paper_open_equity)
        reg = self.registry

        async def search_news_tool(query: str, symbol: str = "",
                                   hours: int | None = None, count: int = 10):
            return await self.search.search_news(query, symbol=symbol,
                                                 hours=hours, count=count)

        async def ask_risk_stub(question: str, options: list[str], context: dict | None = None):
            return {"ok": False,
                    "error": "ask_risk_question 由引擎拦截处理，不应执行到此"}

        reg.register(ToolSpec(
            "get_quote", "查询 A 股实时快照（现价/涨跌幅/量比/换手/市值/PE/PB）。"
            "数据来自工具，引用须标注 data_as_of。",
            QuoteParams, self.market.get_quote, compressor="snapshot", parallel_safe=True))
        reg.register(ToolSpec(
            "get_kline", "获取 A 股日线 K 线（默认后复权 hfq）。",
            KlineParams, self.market.get_kline, compressor="kline", parallel_safe=True))
        reg.register(ToolSpec(
            "get_financial", "获取主要财务指标（营收/净利/毛利/经营现金流 + 同比）。",
            SymbolParams, research.get_financial, compressor="financial", parallel_safe=True))
        reg.register(ToolSpec(
            "get_valuation", "获取估值（PE/PB + 近3年价格分位）。",
            SymbolParams, research.get_valuation, compressor="raw", parallel_safe=True))
        reg.register(ToolSpec(
            "get_indicator", "获取预计算技术指标（MA/RSI/MACD/量比）。",
            SymbolParams, research.get_indicator, compressor="snapshot", parallel_safe=True))
        reg.register(ToolSpec(
            "search_news", "三源搜索个股新闻（默认限24小时，带情绪预聚合）。",
            NewsParams, search_news_tool, compressor="news", parallel_safe=False))
        reg.register(ToolSpec(
            "get_correlation", "多股对比：近120日收盘价 Pearson 相关性矩阵。",
            CorrelationParams, research.get_correlation, compressor="raw", parallel_safe=True))
        reg.register(ToolSpec(
            "run_backtest", "Backtrader 双均线回测（A股规则完整建模：佣金/印花税/T+1/涨跌停）。",
            BacktestParams, quant.run_backtest_tool, compressor="backtest",
            parallel_safe=False))
        reg.register(ToolSpec(
            "walk_forward", "Walk-forward 滚动验证（2年训练→6月样本外→4折衰减曲线）。",
            WalkForwardParams, quant.walk_forward_tool, compressor="backtest",
            parallel_safe=False))
        reg.register(ToolSpec(
            "factor_analysis", "多因子选股 IC 验证（价值/质量/动量/低波动四因子）。",
            FactorParams, quant.factor_analysis_tool, compressor="raw", parallel_safe=False))
        reg.register(ToolSpec(
            "portfolio_optimize", "Markowitz 组合优化（Ledoit-Wolf 收缩协方差 + 合规审查链）。",
            PortfolioParams, advisor.portfolio_optimize, compressor="raw", parallel_safe=False))
        reg.register(ToolSpec(
            "ask_risk_question", "向用户挂起式提问（投顾测评等需要用户输入的场景）。"
            "调用后暂停等待用户点选。",
            AskRiskParams, ask_risk_stub, compressor="raw", parallel_safe=False))
        reg.register(ToolSpec(
            "submit_research_report", "提交最终投研报告（结构化 JSON，"
            "正文数字必须 [n] 引用 citations，≥4 条）。",
            ResearchReport, research.submit_research_report, compressor="raw",
            parallel_safe=False))
        reg.register(ToolSpec(
            "get_alpha_signals", "运行内置 Alpha 模型并合成目标权重（只读，不生成委托）。"
            "弃权模型会显式标注原因；权重已过约束层。",
            SignalToolParams, trading.get_alpha_signals, compressor="raw",
            parallel_safe=True))
        reg.register(ToolSpec(
            "get_paper_account", "查询仿真账户（现金/权益/持仓/当日盈亏/对账状态），只读。",
            AccountToolParams, trading.get_paper_account, compressor="raw",
            parallel_safe=True))
        reg.register(ToolSpec(
            "submit_paper_order", "提交仿真委托（不涉及真实资金）。必须经 G1 适当性 / "
            "G2 资金仓位闸；仅在用户明确要求交易时调用，成交价由服务端快照决定。",
            PaperOrderToolParams, trading.submit_paper_order, compressor="raw",
            parallel_safe=False))

        ledger = CostLedger(self.adb, s.llm.daily_cost_cap_cny)
        self.gateway = LLMGateway(s, ledger, glm_key, deepseek_key)
        self.ask_manager = AskUserManager(self.db, s.server.ask_user_timeout_seconds)
        self.react = ReActEngine(self.gateway, reg, self.ask_manager)
        self.debate = DebateOrchestrator(self.gateway, reg, s.llm.debate_rounds)

        # 快照预计算 fetcher 接到 market 工具
        self.precompute.set_fetcher(self.market.get_kline_raw)


def _extract_symbol(text: str) -> str:
    """从消息中提取 6 位股票代码。"""
    m = re.search(r"(?<!\d)\d{6}(?!\d)", text or "")
    return m.group(0) if m else ""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    ctx = AppContext(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        ctx.wire()
        missing = missing_required_keys()
        if missing:
            logger.error(
                "缺少必需密钥: %s —— 请设置环境变量 FINAGENT_*，"
                "或配置 Windows 凭据管理器（keyring 服务名 oh-ecofin-agent），"
                "或填写 .env（复制 .env.example）。绝不把无密钥伪装成模型故障。", missing)
        if settings.app.lan_mode:
            ctx.lan_token = load_key("lan_token") or generate_token()
            logger.warning("局域网模式已开启，访问令牌: %s（URL ?t= 校验）", ctx.lan_token)
        if not settings.app.demo_mode:
            # 交易日历补抓（跨年自动补抓）；DEMO_MODE 跳过联网
            asyncio.get_running_loop().create_task(ctx.calendar.refresh())
        await ctx.snapshot.refresh_once()
        ctx.snapshot.start()
        # T+N 决策结算定时任务（交易日历感知，30 分钟一次）
        settle_stop = asyncio.Event()

        async def settle_loop():
            from backend.memory.reflections import settle_pending

            while not settle_stop.is_set():
                try:
                    settled = await settle_pending(
                        ctx.decisions, ctx.market.get_close_series, ctx.gateway)
                    if settled:
                        logger.info("T+N 决策结算 %d 笔", len(settled))
                except Exception:
                    logger.exception("结算循环异常")
                try:
                    await asyncio.wait_for(settle_stop.wait(), timeout=1800)
                except TimeoutError:
                    continue

        settle_task = asyncio.get_running_loop().create_task(settle_loop())
        yield
        settle_stop.set()
        settle_task.cancel()
        await ctx.snapshot.stop()

    app = FastAPI(title="Oh EcoFin Agent", version="4.0.0",
                  description="教学演示用 A 股投研智能体；所有输出不构成投资建议。",
                  lifespan=lifespan)
    app.state.ctx = ctx

    @app.middleware("http")
    async def access_control(request: Request, call_next):
        client = request.client.host if request.client else ""
        if settings.app.lan_mode and client not in ("127.0.0.1", "::1", "testclient"):
            token = request.query_params.get("t") or request.headers.get("X-Access-Token", "")
            if not verify_token(token, ctx.lan_token):
                return JSONResponse({"detail": "无效访问令牌（局域网模式需 ?t= 校验）"},
                                    status_code=403)
        return await call_next(request)

    def _sse_headers() -> dict:
        return {"X-Accel-Buffering": "no", "Cache-Control": "no-store",
                "Connection": "keep-alive"}

    def _persist_event(session_id: str):
        def fn(event_id: int, event: str, payload: dict):
            try:
                ctx.db.append_event(session_id, event_id, event,
                                    mask_result_for_storage(payload))
            except Exception:
                logger.exception("事件落库失败")
        return fn

    # ---------- M7/M10 看板辅助 ----------

    def _paper_account(risk_level: str) -> dict:
        payload = account_payload(ctx.broker, ctx.snapshot, risk_level,
                                  ctx.paper_open_equity)
        payload["disclaimer"] = DISCLAIMER
        return payload

    def _risk_level(raw: str) -> str:
        try:
            return validate_risk_level(raw)
        except InvalidRiskLevelError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    # ---------- 基础端点 ----------

    @app.get("/api/health")
    async def health():
        missing = missing_required_keys()
        return {
            "status": "ok" if not missing else "degraded",
            "missing_keys": missing,
            "demo_mode": settings.app.demo_mode,
            "lan_mode": settings.app.lan_mode,
            "snapshot": {"as_of": ctx.snapshot.as_of,
                         "source": ctx.snapshot._source,
                         **ctx.snapshot.freshness()},
            "disclaimer": DISCLAIMER,
        }

    @app.post("/api/sessions")
    async def create_session():
        return ctx.sessions.create()

    @app.get("/api/sessions")
    async def list_sessions():
        return {"sessions": ctx.sessions.list_sessions()}

    @app.get("/api/sessions/{sid}/messages")
    async def get_messages(sid: str):
        return {"messages": ctx.sessions.messages(sid),
                "disclaimer": DISCLAIMER}

    @app.get("/api/cost/summary")
    async def cost_summary():
        today = await ctx.adb.cost_today_cents()
        cap = int(settings.llm.daily_cost_cap_cny * 100)
        return {"today_cents": today, "cap_cents": cap,
                "fuse_open": today >= cap,
                "by_model": ctx.db.export_cost_breakdown(),
                "disclaimer": DISCLAIMER}

    @app.get("/api/decisions/{symbol}")
    async def decision_history(symbol: str):
        return {"decisions": ctx.decisions.history_for(symbol.zfill(6)),
                "disclaimer": DISCLAIMER}

    # ---------- M7 信号与组合看板 ----------

    @app.get("/api/signals/history")
    async def signal_history(symbol: str = "", model: str = "",
                             limit: int = Query(50, ge=1, le=200)):
        sym = _norm_symbol(symbol) if symbol else None
        return {"signals": ctx.signal_store.recent_signals(
                    model_name=model or None, symbol=sym, limit=limit),
                "disclaimer": DISCLAIMER}

    @app.get("/api/signals/{symbol}")
    async def signal_preview(symbol: str,
                             mode: str = Query("long_only", description="组合模式"),
                             gross: float = Query(0.8, gt=0, le=1,
                                                  description="目标总仓位")):
        sym = _norm_symbol(symbol)
        try:
            payload = await preview_signals(ctx.market, ctx.signal_store, sym,
                                            mode=mode, gross=gross)
        except SignalDataError as e:
            status = 503 if str(e).startswith("K线获取失败") else 400
            raise HTTPException(status_code=status, detail=str(e)) from e
        return {**payload, "disclaimer": DISCLAIMER}

    # ---------- M10 仿真执行看板 ----------

    @app.get("/api/paper/quote/{symbol}")
    async def paper_quote(symbol: str):
        try:
            quote = symbol_quote(ctx.snapshot, _norm_symbol(symbol))
        except QuoteUnavailableError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return {"quote": quote, "disclaimer": DISCLAIMER}

    @app.get("/api/paper/account")
    async def paper_account(risk_level: str = "R3"):
        return _paper_account(_risk_level(risk_level))

    @app.post("/api/paper/orders")
    async def paper_order(req: PaperOrderRequest):
        sym = _norm_symbol(req.symbol)
        _risk_level(req.risk_level)
        try:
            result = await submit_paper_order(
                ctx.router, ctx.broker, ctx.snapshot,
                day_open_equity=ctx.paper_open_equity,
                symbol=sym, side=req.side, qty=req.qty,
                risk_level=req.risk_level, strategy_id=req.strategy_id,
                order_type=req.order_type, limit_price=req.limit_price)
        except QuoteUnavailableError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except (InvalidRiskLevelError, ValidationError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {**result, "disclaimer": DISCLAIMER}

    @app.get("/api/paper/orders")
    async def paper_orders(strategy_id: str = "",
                           limit: int = Query(20, ge=1, le=100)):
        rows = ctx.exec_store.recent_orders(limit)
        if strategy_id:
            rows = [r for r in rows if r.get("strategy_id") == strategy_id]
        return {"orders": rows, "disclaimer": DISCLAIMER}

    @app.get("/api/paper/risk-events")
    async def paper_risk_events(limit: int = Query(50, ge=1, le=200)):
        return {"events": ctx.exec_store.recent_risk_events(limit),
                "disclaimer": DISCLAIMER}

    @app.post("/api/paper/roll-day")
    async def paper_roll_day(risk_level: str = "R3"):
        """教学演示：模拟交易日切换（T+1 解冻 + 日初权益重置）。"""
        risk = _risk_level(risk_level)
        ctx.broker.roll_day()
        prices = positions_prices(ctx.broker, ctx.snapshot)
        ctx.paper_open_equity = ctx.broker.context(prices).total_equity
        return {"ok": True, "account": _paper_account(risk),
                "disclaimer": DISCLAIMER}

    # ---------- P2 实盘通道（默认关闭，只读状态 + 显式探测） ----------

    @app.get("/api/live/status")
    async def live_status():
        cfg = settings.execution
        if ctx.live_gateway is None:
            return {
                "enabled": False, "configured": False,
                "adapter": cfg.broker_kind, "connected": False, "account": "",
                "detail": "实盘通道未启用（FINAGENT_EXECUTION__LIVE_ENABLED=1 开启，"
                          "并配置券商账户/路径）",
                "disclaimer": DISCLAIMER,
            }
        return {
            "enabled": True, "configured": True,
            "adapter": cfg.broker_kind, "connected": ctx.live_connected,
            "account": mask_account(cfg.account_id),
            "detail": "实盘通道已装配，但未自动连接；交易需显式探测并人工确认",
            "disclaimer": DISCLAIMER,
        }

    @app.post("/api/live/probe")
    async def live_probe():
        if ctx.live_gateway is None:
            raise HTTPException(status_code=400,
                                detail="实盘通道未启用：先配置 execution.live_enabled")
        try:
            await ctx.live_gateway.connect()
            ctx.live_connected = await ctx.live_gateway.health()
            return {"ok": ctx.live_connected, "connected": ctx.live_connected,
                    "adapter": settings.execution.broker_kind,
                    "detail": "连接与健康检查通过" if ctx.live_connected
                              else "连接成功但健康检查未通过",
                    "disclaimer": DISCLAIMER}
        except GatewayError as e:
            ctx.live_connected = False
            return {"ok": False, "connected": False,
                    "adapter": settings.execution.broker_kind,
                    "error": str(e), "hint": getattr(e, "hint", ""),
                    "disclaimer": DISCLAIMER}

    @app.get("/api/risk-questions")
    async def risk_questions():
        return {"questions": [
            {"qid": q["qid"], "dimension": q["dimension"], "question": q["question"],
             "options": [o["text"] for o in q["options"]]} for q in RISK_QUESTIONS],
            "equity_caps": EQUITY_CAPS, "disclaimer": DISCLAIMER}

    # ---------- M11 投顾与合规看板 ----------

    @app.post("/api/advisor/assess")
    async def advisor_assess(req: AdvisorAssessRequest):
        qids = [str(a.get("qid", "")) for a in req.answers]
        if len(set(qids)) != len(qids):
            raise HTTPException(status_code=400, detail="答案存在重复题目")
        missing = {q["qid"] for q in RISK_QUESTIONS} - set(qids)
        if missing:
            raise HTTPException(status_code=400,
                                detail=f"缺少题目答案: {sorted(missing)}")
        try:
            total, level = score_answers(req.answers)
            profile = profile_for_level(level)
        except (ValueError, KeyError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"ok": True, "score": total, "level": level, "profile": profile,
                "equity_cap": EQUITY_CAPS[level],
                "single_cap": profile["single_cap"],
                "disclaimer": DISCLAIMER}

    @app.post("/api/advisor/portfolio")
    async def advisor_portfolio(req: AdvisorPortfolioRequest):
        try:
            params = PortfolioParams(symbols=req.symbols, level=req.level)
        except ValidationError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if ctx.advisor is None:
            raise HTTPException(status_code=503, detail="投顾服务未装配")
        try:
            result = await ctx.advisor.portfolio_optimize(params.symbols, params.level)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if result.get("ok"):
            review = review_advice(
                result["weights"], params.level, industry=req.industry,
                statement=req.statement)
            result = {**result, "compliance_report": review,
                      "status": review["status"]}
        else:
            result = {**result, "status": "attention",
                      "compliance_report": {"status": "attention",
                                            "violations": [],
                                            "disclaimer": DISCLAIMER}}
        return {**result, "disclaimer": DISCLAIMER}

    @app.post("/api/advisor/statement")
    async def advisor_statement(req: StatementCheckRequest):
        review = review_advice({}, "R3", statement=req.text)
        return {"ok": True, **review}

    # ---------- 会话切换：SSE 断线重连重放 ----------

    @app.get("/api/chat/stream")
    async def chat_stream(request: Request, sid: str = Query(...),
                          last_event_id: int = 0, replay_only: bool = False):
        """断线重连重放：Last-Event-ID 之后全部重放，随后实时尾部（长连）。

        replay_only=true 仅回放历史（会话切换加载历史消息），回放完即返回。
        """
        stream = ctx.hub.get(sid)
        if last_event_id == 0:
            try:
                last_event_id = int(request.headers.get("Last-Event-ID", "0"))
            except ValueError:
                last_event_id = 0
        db_rows = await ctx.adb.load_events_after(sid, last_event_id)

        async def gen():
            for ev in stream.replay_from(last_event_id, db_rows):
                yield ev.sse_format()
            if replay_only:
                yield stream.publish("ping", {}).sse_format()  # 终止哨兵
                return
            q = stream.subscribe()
            idle = 0
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(
                            q.get(), timeout=settings.server.sse_heartbeat_seconds)
                        yield ev.sse_format()
                        idle = 0
                    except TimeoutError:
                        idle += 1
                        yield stream.publish("ping", {}).sse_format()
                        if idle > 20:  # ~5 分钟无活动主动断开（客户端可再重连）
                            break
            finally:
                stream.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers=_sse_headers())

    # ---------- 主入口：POST /api/chat → SSE v2 ----------

    @app.post("/api/chat")
    async def chat(req: ChatRequest):
        sid = req.session_id or uuid.uuid4().hex[:16]
        session = ctx.sessions.get(sid)
        if session is None:
            ctx.sessions.create("")
        ctx.sessions.rename(sid, req.message[:12])

        stream = ctx.hub.get(sid)
        stream.persist_fn = _persist_event(sid)

        mode = req.mode
        if mode == "auto":
            if any(k in req.message for k in DEBATE_KEYWORDS):
                mode = "debate"
            elif any(k in req.message for k in ADVISOR_KEYWORDS):
                mode = "advisor"
            else:
                mode = "chat"

        history = ctx.sessions.messages(sid)[-40:]
        history_block_text = ""
        if mode == "debate":
            symbol = _extract_symbol(req.message)
            if symbol:
                history_block_text = history_block(ctx.decisions.history_for(symbol))

        rendered = await _maybe_summarize(history)

        task = ChatTask(session_id=sid, user_message=req.message,
                        history=rendered, history_block=history_block_text, mode=mode)
        ctx.sessions.insert_message(sid, "user", req.message)

        async def gen():
            q = stream.subscribe()
            runner: asyncio.Task | None = None
            assistant_saved = False
            try:
                if mode == "debate":
                    symbol = _extract_symbol(req.message)
                    runner = asyncio.create_task(
                        _run_debate(sid, symbol, req.message, stream))
                else:
                    runner = asyncio.create_task(ctx.react.run(task, stream))
                done_seen = False
                while not done_seen:
                    try:
                        ev = await asyncio.wait_for(
                            q.get(), timeout=settings.server.sse_heartbeat_seconds)
                    except TimeoutError:
                        yield stream.publish("ping", {}).sse_format()
                        continue
                    yield ev.sse_format()
                    if ev.event == "done" or ev.event == "error" and not ev.payload.get("recoverable"):
                        done_seen = True
                if runner is not None:
                    try:
                        result = await runner
                        ctx.sessions.insert_message(
                            sid, "assistant",
                            json.dumps(result, ensure_ascii=False, default=str)[:8000])
                        assistant_saved = True
                    except Exception:
                        logger.exception("引擎任务异常")
            finally:
                stream.unsubscribe(q)
                if runner and not runner.done():
                    runner.cancel()
                if not assistant_saved:
                    pass  # done 事件已含结果，消息落库尽力而为

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers=_sse_headers())

    async def _run_debate(sid: str, symbol: str, question: str, stream):
        result = await ctx.debate.run(sid, [symbol] if symbol else ["000001"],
                                      question, stream)
        # 六段原文 + context_id 落库（断线/重启后可回放，报告 5.9 debate_runs）
        transcript = result.get("transcript")
        if transcript:
            try:
                from backend.engine.committee import persist_audit

                result["debate_run_id"] = persist_audit(
                    ctx.db, transcript, symbol=symbol or "000001",
                    verdict=str(result.get("report", {}).get("verdict", "neutral")),
                    status="review" if result.get("verification_required") else "ok")
            except Exception:
                logger.exception("辩论审计落库失败")
        stream.publish("message_delta", {"text": json.dumps(
            {"type": "report", "report": result["report"],
             "debate": {k: v for k, v in result["debate"].items()},
             "datapack": result["datapack"][:2000]},
            ensure_ascii=False, default=str)})
        report = result.get("report", {})
        if report and symbol:
            try:
                ctx.decisions.save_decision(
                    symbol, report.get("verdict", "neutral"),
                    report.get("thesis_digest", ""),
                    {"datapack": result["datapack"], "question": question,
                     "raw_tool_results": result["raw_tool_results"][:20]},
                    report.get("meta", {}).get("data_as_of", ""), horizon_days=5)
            except Exception:
                logger.exception("决策落库失败")
        stream.publish("done", {
            "usage": {"calls": 1},
            "cost_breakdown": {"note": "分模型聚合见 /api/cost/summary"},
            "rounds": result["debate"].get("rounds", 0) + 1,
            "tool_stats": {"calls": len(result["raw_tool_results"])},
            "mode": "debate",
            "disclaimer": DISCLAIMER,
        })
        return {"report": report, "debate": result["debate"]}

    async def _maybe_summarize(history: list[dict]) -> list[dict]:
        """触发条件：tokens > 20k 或轮次 > 20 → 分层摘要（economy）+ 事实回读校验。"""
        msgs = [{"role": m["role"], "content": m["content"]} for m in history]
        total_chars = sum(len(str(m["content"])) for m in msgs)
        n_tokens = estimate_tokens("x" * total_chars)
        n_rounds = sum(1 for m in msgs if m["role"] == "user")
        if not should_summarize(n_tokens, n_rounds,
                                token_trigger=settings.memory.summarize_trigger_tokens,
                                round_trigger=settings.memory.summarize_trigger_rounds):
            return msgs
        try:
            conv_text, recent = keep_recent(msgs, settings.memory.keep_recent_rounds)
            if not conv_text:
                return recent
            summary_text = await ctx.gateway.simple(
                [{"role": "user", "content": build_summary_prompt(conv_text)}],
                "economy", role="summarize", max_tokens=800)
            summary = fact_readback_verify(parse_summary_json(summary_text), conv_text)
            return render_history(summary, recent)
        except Exception:
            logger.exception("分层摘要失败，降级原文")
            return msgs

    # ---------- resume ----------

    @app.post("/api/chat/resume")
    async def resume(req: ResumeRequest):
        ok = await ctx.ask_manager.resume(req.session_id, req.qid, req.choice)
        if not ok:
            raise HTTPException(404, "无匹配的挂起提问（可能已超时）")
        return {"ok": True}

    # ---------- 静态托管（前端构建产物；相对路径三端同源） ----------

    static_dir = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


def main() -> None:
    import uvicorn

    settings = load_settings()
    app = create_app(settings)
    uvicorn.run(app, host=settings.server.host, port=settings.server.port,
                reload=False, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
