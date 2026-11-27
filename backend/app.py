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

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from backend.analysis.models import AnalysisError, AnalysisRunRequest
from backend.analysis.service import AnalysisService
from backend.audit.service import AuditService
from backend.catalog.importer import import_financedatabase
from backend.catalog.models import CatalogError, CatalogQuery
from backend.catalog.store import CatalogStore
from backend.config import Settings, load_settings
from backend.data.calendar import TradeCalendar
from backend.data.precompute import PrecomputeService
from backend.data.ratelimit import TokenBucket
from backend.data.snapshot import SnapshotService
from backend.datasets.models import (
    DatasetError,
    DatasetLimitError,
    DatasetNotFoundError,
)
from backend.datasets.service import DatasetService
from backend.db import AsyncDatabase, Database
from backend.engine.askuser import AskUserManager
from backend.engine.debate import DebateOrchestrator
from backend.engine.events import StreamHub
from backend.engine.gateway import CostLedger, LLMGateway
from backend.engine.prompts import DISCLAIMER
from backend.engine.react import ChatTask, ReActEngine
from backend.engine.run_state import RunBudget
from backend.engine.runs import RunStore
from backend.engine.tool_calls import ToolCallStore
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
from backend.query.engine import (
    QueryAuditStore,
    QueryEngine,
    QueryExecutionError,
    QueryTimeoutError,
)
from backend.query.guard import SqlGuardError
from backend.reports.models import (
    ReportAuditError,
    ReportComposeRequest,
    ReportError,
)
from backend.reports.service import MEDIA_TYPES, ReportService
from backend.sandbox.models import (
    SandboxDisabledError,
    SandboxError,
    SandboxNotFoundError,
    SandboxValidationError,
)
from backend.sandbox.service import SandboxService
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
from backend.tools.data import (
    AnalysisModulesParams,
    AnalysisRunParams,
    CatalogSearchParams,
    DatasetListParams,
    DatasetProfileParams,
    DataTools,
    ReportComposeParams,
    SandboxRunParams,
    SqlQueryParams,
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


class CatalogImportRequest(BaseModel):
    """本地 FinanceDatabase 目录导入（仅回环/本机模式开放）。"""

    path: str = Field(..., min_length=1, max_length=500)
    as_of: str = Field("", max_length=10, pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    source_version: str = Field("", max_length=80)
    force: bool = False


class SqlQueryRequest(BaseModel):
    sql: str = Field(..., min_length=1, max_length=20000)
    dataset_id: str = Field("", max_length=64)
    limit: int = Field(200, ge=1, le=50000)


class SandboxRunRequest(BaseModel):
    """自由代码沙箱请求：默认关闭，开启后仍受 AST/进程/资源三重限制。"""

    code: str = Field(..., min_length=1, max_length=20000)
    dataset_ids: list[str] = Field(default_factory=list, max_length=5)


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
        # M1-M3 数据基座：版本化标的目录 + 不可变数据集 + 只读 SQL
        self.catalog = CatalogStore(self.db)
        self.datasets = DatasetService(self.db, settings)
        self.query_audit = QueryAuditStore(self.db)
        self.query = QueryEngine(
            self.datasets, self.query_audit, settings.datasets.workspace_dir,
            max_rows=settings.datasets.query_max_rows,
            timeout_seconds=settings.datasets.query_timeout_seconds)
        self.analysis = AnalysisService(self.datasets, settings)
        self.audit = AuditService(self.db, settings)
        self.reports = ReportService(self.db, settings, self.analysis,
                                     self.query, audit=self.audit)
        self.sandbox = SandboxService(self.db, settings, self.datasets)
        self.data_tools = DataTools(self.catalog, self.datasets, self.query,
                                    self.analysis, self.reports)
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
        self.run_store = RunStore(self.db)
        self.tool_store = ToolCallStore(self.db)
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

        async def run_sandbox_code(code: str, dataset_ids: list[str] | None = None):
            try:
                result = await asyncio.to_thread(
                    self.sandbox.run, code, dataset_ids or [])
            except SandboxError as e:
                return {"ok": False, "error": str(e), "code": e.code,
                        "hint": e.hint}
            return {
                "ok": result["status"] == "ok",
                "run_id": result["run_id"],
                "status": result["status"],
                "result": result["result"],
                "stdout": result["stdout"],
                "stderr": result["stderr"],
                "error": result["error"],
                "files": result["files"],
                "datasets": result["datasets"],
                "hint": "自由代码结果需与确定性工具/SQL 结果交叉核对后再进入报告。",
            }

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
            parallel_safe=False, side_effect="external",
            confirmation="explicit_request"))
        reg.register(ToolSpec(
            "catalog_search", "检索标的目录（代码/名称/行业/交易所），返回 instrument_id、"
            "资产类别与上市状态。目录只回答“存在哪些标的”，不含行情；"
            "行情/财务请继续调用 get_quote/get_kline/get_financial。",
            CatalogSearchParams, self.data_tools.catalog_search,
            compressor="raw", parallel_safe=True))
        reg.register(ToolSpec(
            "dataset_list", "列出已导入数据集：dataset_id、SQL 表名 table_name、行列数。"
            "查询前先用本工具确认表名。",
            DatasetListParams, self.data_tools.dataset_list,
            compressor="raw", parallel_safe=True))
        reg.register(ToolSpec(
            "dataset_profile", "查看数据集画像：列类型、缺失数/缺失率、唯一值数、"
            "数值分位与样本行。写分析/报告前先用本工具核对列语义。",
            DatasetProfileParams, self.data_tools.dataset_profile,
            compressor="raw", parallel_safe=True))
        reg.register(ToolSpec(
            "sql_query", "对已导入数据集执行单条只读 SELECT/WITH 查询。表名使用 "
            "dataset_list 返回的 table_name；禁止 DDL/DML/ATTACH/文件函数；"
            "默认最多返回 100 行。报告引用数字须保留 result_hash 以便证据回放。",
            SqlQueryParams, self.data_tools.sql_query,
            compressor="raw", parallel_safe=True))
        reg.register(ToolSpec(
            "analysis_modules", "列出内置分析模块及参数说明：描述统计/缺失/相关/"
            "趋势/分组/漏斗/A-B。调用 analysis_run 前先用本工具确认模块名与参数。",
            AnalysisModulesParams, self.data_tools.analysis_modules,
            compressor="raw", parallel_safe=True))
        reg.register(ToolSpec(
            "analysis_run", "运行内置确定性分析模块，返回统计结果、可复现 result_hash "
            "与 ECharts chart spec。列名来自 dataset_profile；不要用自由代码计算。",
            AnalysisRunParams, self.data_tools.analysis_run,
            compressor="raw", parallel_safe=True))
        reg.register(ToolSpec(
            "report_compose", "把分析/SQL/文本块组合为报告，生成 HTML/Word/Excel 到"
            " data/reports，并返回每块的数据集 sha 与 result_hash。"
            "仅在用户明确要求“生成/导出报告”时调用；分析块会重新执行以保证可复现。",
            ReportComposeParams, self.data_tools.report_compose,
            compressor="raw", parallel_safe=False,
            side_effect="workspace", confirmation="explicit_request"))

        if s.sandbox.enabled:
            reg.register(ToolSpec(
                "run_sandbox_code", "在独立隔离 worker 中运行自由 Python 代码，"
                "用于内置分析模块与 SQL 无法覆盖的定制计算。代码通过 datasets 字典"
                "（键为数据集名/表名）获取 DataFrame，可用 pd/np，用 "
                "save_output(filename, content) 输出 csv/json/txt/md，把最终结论赋给 "
                "result。禁止文件、网络、系统调用与外部进程；默认超时/内存/输出均有限额。"
                "仅在用户明确要求“运行代码/写代码计算”时调用。",
                SandboxRunParams, run_sandbox_code, compressor="raw",
                parallel_safe=False, side_effect="workspace",
                confirmation="explicit_request",
                timeout_seconds=int(s.sandbox.timeout_seconds) + 15))

        ledger = CostLedger(self.adb, s.llm.daily_cost_cap_cny)
        self.gateway = LLMGateway(s, ledger, glm_key, deepseek_key)
        self.ask_manager = AskUserManager(self.db, s.server.ask_user_timeout_seconds)
        self.react = ReActEngine(
            self.gateway, reg, self.ask_manager,
            budget=RunBudget.from_config(s.agent, max_rounds=s.llm.max_rounds),
            run_store=self.run_store, tool_store=self.tool_store)
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
        try:
            # 启动不阻塞于外部行情源：超时后先提供 /api/health，后台循环继续重试
            await asyncio.wait_for(ctx.snapshot.refresh_once(), timeout=20)
        except TimeoutError:
            logger.warning("启动快照刷新超时（20s），继续启动并在后台重试")
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
            "version": app.version,
            "missing_keys": missing,
            "demo_mode": settings.app.demo_mode,
            "lan_mode": settings.app.lan_mode,
            "snapshot": {"as_of": ctx.snapshot.as_of,
                         "source": ctx.snapshot._source,
                         **ctx.snapshot.freshness()},
            "catalog": {"instruments": ctx.catalog.count(),
                        "snapshots": len(ctx.catalog.snapshots(5))},
            "datasets": {"count": ctx.datasets.store.count()},
            "analysis": {"modules": len(ctx.analysis.modules())},
            "reports": {"count": ctx.reports.store.count()},
            "agent": {"runs": ctx.run_store.count(),
                      "side_effects": ctx.registry.side_effect_map()},
            "sandbox": {"enabled": settings.sandbox.enabled,
                        "runs": ctx.sandbox.count()},
            "data_dirs": {"catalog": settings.catalog.dir,
                          "datasets": settings.datasets.dir,
                          "workspaces": settings.datasets.workspace_dir},
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

    # ---------- M1 标的目录 ----------

    @app.get("/api/catalog/search")
    async def catalog_search(
            q: str = Query("", max_length=100),
            symbol: str = Query("", max_length=32),
            asset_class: str = Query("", max_length=20),
            exchange: str = Query("", max_length=16),
            industry: str = Query("", max_length=80),
            country: str = Query("", max_length=80),
            listing_status: str = Query("active", max_length=16),
            limit: int = Query(50, ge=1, le=200),
            offset: int = Query(0, ge=0)):
        params = CatalogQuery(
            query=q, symbol=symbol, asset_class=asset_class, exchange=exchange,
            industry=industry, country=country, listing_status=listing_status,
            limit=limit, offset=offset)
        return {**ctx.catalog.search(params), "disclaimer": DISCLAIMER}

    @app.get("/api/catalog/stats")
    async def catalog_stats():
        return {**ctx.catalog.stats(), "snapshots": ctx.catalog.snapshots(20),
                "disclaimer": DISCLAIMER}

    @app.get("/api/catalog/instruments/{instrument_id}")
    async def catalog_instrument(instrument_id: str):
        item = ctx.catalog.get(instrument_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"标的不存在: {instrument_id}")
        return {"instrument": item, "aliases": ctx.catalog.aliases(instrument_id),
                "disclaimer": DISCLAIMER}

    @app.post("/api/catalog/import")
    async def catalog_import(req: CatalogImportRequest):
        """导入本地 FinanceDatabase 快照；局域网模式禁用（避免远程读本机路径）。"""
        if settings.app.lan_mode:
            raise HTTPException(status_code=403,
                                detail="局域网模式禁用本地路径导入，请在服务端执行脚本")
        try:
            result = await asyncio.to_thread(
                import_financedatabase, req.path, ctx.catalog,
                settings.catalog.dir, as_of=req.as_of,
                source_version=req.source_version, force=req.force)
        except CatalogError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {**result, "disclaimer": DISCLAIMER}

    # ---------- M2 数据集导入 ----------

    @app.get("/api/datasets")
    async def dataset_list():
        items = await asyncio.to_thread(ctx.datasets.list)
        return {"datasets": items, "count": len(items),
                "disclaimer": DISCLAIMER}

    @app.post("/api/datasets")
    async def dataset_upload(file: UploadFile = File(...)):
        try:
            result = await asyncio.to_thread(
                ctx.datasets.import_upload, file.file, file.filename or "upload.csv")
        except DatasetLimitError as e:
            raise HTTPException(status_code=413, detail=str(e)) from e
        except DatasetError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        finally:
            await file.close()
        return {**result, "disclaimer": DISCLAIMER}

    @app.get("/api/datasets/{dataset_id}")
    async def dataset_detail(dataset_id: str):
        item = await asyncio.to_thread(ctx.datasets.get, dataset_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"数据集不存在: {dataset_id}")
        return {"dataset": item, "disclaimer": DISCLAIMER}

    @app.get("/api/datasets/{dataset_id}/versions")
    async def dataset_versions(dataset_id: str):
        rows = await asyncio.to_thread(ctx.datasets.store.versions, dataset_id)
        if not rows:
            raise HTTPException(status_code=404, detail=f"数据集不存在: {dataset_id}")
        return {"versions": [{
            "version": r["version"], "sha256": r["sha256"],
            "created_at": r["created_at"],
            "rows": (r.get("manifest") or {}).get("rows", 0),
            "columns": (r.get("manifest") or {}).get("columns", 0),
        } for r in rows], "disclaimer": DISCLAIMER}

    @app.get("/api/datasets/{dataset_id}/profile")
    async def dataset_profile(dataset_id: str):
        try:
            result = await asyncio.to_thread(ctx.datasets.profile, dataset_id)
        except DatasetNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except DatasetError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {**result, "disclaimer": DISCLAIMER}

    # ---------- M3 只读 SQL ----------

    @app.get("/api/query/tables")
    async def query_tables():
        return {"tables": ctx.query.tables(), "disclaimer": DISCLAIMER}

    @app.post("/api/query")
    async def run_query(req: SqlQueryRequest):
        dataset_ids = [req.dataset_id] if req.dataset_id else None
        try:
            result = await asyncio.to_thread(
                ctx.query.execute, req.sql, max_rows=req.limit,
                dataset_ids=dataset_ids)
        except SqlGuardError as e:
            raise HTTPException(status_code=400, detail={
                "code": e.code, "message": str(e), "hint": e.hint}) from e
        except QueryTimeoutError as e:
            raise HTTPException(status_code=504, detail={
                "code": e.code, "message": str(e), "hint": e.hint}) from e
        except QueryExecutionError as e:
            raise HTTPException(status_code=400, detail={
                "code": e.code, "message": str(e), "hint": e.hint}) from e
        return {**result, "disclaimer": DISCLAIMER}

    @app.get("/api/query/audit")
    async def query_audit(limit: int = Query(50, ge=1, le=500)):
        rows = await asyncio.to_thread(ctx.query_audit.recent, limit)
        return {"audit": rows, "disclaimer": DISCLAIMER}

    # ---------- P3 自由代码隔离沙箱 ----------

    @app.get("/api/sandbox/status")
    async def sandbox_status():
        return {**ctx.sandbox.status(), "disclaimer": DISCLAIMER}

    @app.post("/api/sandbox/run")
    async def sandbox_run(req: SandboxRunRequest):
        try:
            result = await asyncio.to_thread(
                ctx.sandbox.run, req.code, req.dataset_ids)
        except SandboxDisabledError as e:
            raise HTTPException(status_code=403, detail={
                "code": e.code, "message": str(e), "hint": e.hint}) from e
        except SandboxValidationError as e:
            raise HTTPException(status_code=400, detail={
                "code": e.code, "message": str(e), "hint": e.hint}) from e
        except SandboxError as e:
            raise HTTPException(status_code=400, detail={
                "code": e.code, "message": str(e), "hint": e.hint}) from e
        return {**result, "disclaimer": DISCLAIMER}

    @app.get("/api/sandbox/runs")
    async def sandbox_runs(limit: int = Query(50, ge=1, le=200)):
        rows = await asyncio.to_thread(ctx.sandbox.recent, limit)
        return {"runs": rows, "count": ctx.sandbox.count(),
                "disclaimer": DISCLAIMER}

    @app.get("/api/sandbox/runs/{run_id}")
    async def sandbox_run_detail(run_id: str):
        try:
            row = await asyncio.to_thread(ctx.sandbox.get, run_id)
        except SandboxNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return {"run": row, "disclaimer": DISCLAIMER}

    @app.get("/api/sandbox/runs/{run_id}/files/{name}")
    async def sandbox_run_file(run_id: str, name: str):
        try:
            path = await asyncio.to_thread(ctx.sandbox.output_path, run_id, name)
        except SandboxNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        return FileResponse(path, filename=name)

    # ---------- M4 分析模块与图表 ----------

    @app.get("/api/analysis/modules")
    async def analysis_modules():
        modules = ctx.analysis.modules()
        return {"modules": modules, "count": len(modules),
                "disclaimer": DISCLAIMER}

    @app.post("/api/analysis/run")
    async def analysis_run(req: AnalysisRunRequest):
        try:
            result = await asyncio.to_thread(
                ctx.analysis.run, req.module, req.dataset_id, req.params,
                req.version)
        except AnalysisError as e:
            status = 504 if e.code == "timeout" else 400
            raise HTTPException(status_code=status, detail={
                "code": e.code, "message": str(e), "hint": e.hint}) from e
        return {**result, "disclaimer": DISCLAIMER}

    # ---------- M5 报告与导出 ----------

    @app.get("/api/reports")
    async def list_reports(limit: int = Query(50, ge=1, le=200)):
        rows = await asyncio.to_thread(ctx.reports.list, limit)
        return {"reports": rows, "count": len(rows), "disclaimer": DISCLAIMER}

    @app.post("/api/reports")
    async def compose_report(req: ReportComposeRequest):
        try:
            result = await asyncio.to_thread(ctx.reports.compose, req)
        except ReportAuditError as e:
            raise HTTPException(status_code=400, detail={
                "code": e.code, "message": str(e), "hint": e.hint,
                "report_id": e.report_id, "audit": e.audit}) from e
        except ReportError as e:
            status = 500 if e.code == "render_error" else 400
            raise HTTPException(status_code=status, detail={
                "code": e.code, "message": str(e), "hint": e.hint}) from e
        return {**result, "disclaimer": DISCLAIMER}

    @app.get("/api/reports/{report_id}")
    async def get_report(report_id: str):
        try:
            result = await asyncio.to_thread(ctx.reports.get, report_id)
        except ReportError as e:
            raise HTTPException(status_code=404, detail={
                "code": e.code, "message": str(e), "hint": e.hint}) from e
        return {**result, "disclaimer": DISCLAIMER}

    @app.get("/api/reports/{report_id}/export/{fmt}")
    async def export_report(report_id: str,
                            fmt: Literal["html", "docx", "xlsx"]):
        try:
            path, filename = await asyncio.to_thread(
                ctx.reports.export_path, report_id, fmt)
        except ReportError as e:
            raise HTTPException(status_code=404, detail={
                "code": e.code, "message": str(e), "hint": e.hint}) from e
        return FileResponse(path, filename=filename,
                            media_type=MEDIA_TYPES[fmt])

    @app.post("/api/reports/{report_id}/rerun")
    async def rerun_report(report_id: str):
        try:
            result = await asyncio.to_thread(ctx.reports.rerun, report_id)
        except ReportError as e:
            raise HTTPException(status_code=400, detail={
                "code": e.code, "message": str(e), "hint": e.hint}) from e
        return {**result, "disclaimer": DISCLAIMER}

    # ---------- M7 可信审计 ----------

    @app.get("/api/audit/reports")
    async def audit_reports(limit: int = Query(50, ge=1, le=200)):
        rows = await asyncio.to_thread(ctx.reports.list, limit)
        return {"reports": rows, "count": len(rows),
                "disclaimer": DISCLAIMER}

    @app.get("/api/audit/reports/{report_id}")
    async def audit_report_detail(report_id: str):
        try:
            report = await asyncio.to_thread(ctx.reports.get, report_id)
        except ReportError as e:
            raise HTTPException(status_code=404, detail={
                "code": e.code, "message": str(e), "hint": e.hint}) from e
        audit_data = await asyncio.to_thread(ctx.audit.get, report_id)
        return {**audit_data, "report": report,
                "audit": report.get("audit") or {},
                "disclaimer": DISCLAIMER}

    # ---------- M6 Agent 运行记录 ----------

    @app.get("/api/runs")
    async def list_runs(limit: int = Query(50, ge=1, le=200)):
        return {"runs": ctx.run_store.recent(limit),
                "count": ctx.run_store.count(),
                "disclaimer": DISCLAIMER}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        row = ctx.run_store.get(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"运行记录不存在: {run_id}")
        return {"run": row, "disclaimer": DISCLAIMER}

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
                        _run_debate_guarded(sid, symbol, req.message, stream))
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
                    # M6：只有 done 是终态；error 是过程告警
                    if ev.event == "done":
                        done_seen = True
                if not done_seen:
                    stream.publish("error", {
                        "code": "NO_TERMINAL", "message": "引擎未发送终态事件",
                        "recoverable": False})
                    stream.publish("done", {
                        "terminal": True, "stop_reason": "no_terminal",
                        "rounds": 0, "tool_stats": {"calls": 0},
                        "error": "引擎未发送终态事件", "disclaimer": DISCLAIMER})
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

    async def _run_debate_guarded(sid: str, symbol: str, question: str, stream):
        try:
            return await _run_debate(sid, symbol, question, stream)
        except Exception as e:
            logger.exception("辩论运行异常")
            stream.publish("error", {
                "code": "DEBATE_ERROR", "message": str(e), "recoverable": False})
            stream.publish("done", {
                "terminal": True, "stop_reason": "debate_error",
                "mode": "debate", "rounds": 0,
                "tool_stats": {"calls": 0}, "error": str(e),
                "disclaimer": DISCLAIMER,
            })
            return {"error": str(e)}

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
            "terminal": True, "stop_reason": "debate_answer",
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
