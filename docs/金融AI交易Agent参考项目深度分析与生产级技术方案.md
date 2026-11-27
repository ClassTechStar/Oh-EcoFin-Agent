# 金融量化 AI 交易 Agent 参考项目深度分析与生产级技术方案

> 版本：v1.0 · 研究基线：本地克隆代码（2026-11）
> 研究范围：14 个开源项目 · 代码级证据索引见附录 A
> 定位：面向 A 股、可实盘演进的金融 AI 交易 Agent 系统工程蓝图

---

## 0. 研究方法与样本说明

### 0.1 方法

| 维度 | 做法 |
|---|---|
| 证据来源 | 直接读取本地克隆源码，所有结论标注 `项目/文件:行号` |
| 分析粒度 | 架构分层 → 模块职责 → 关键数据结构 → 数据流 → 代码契约 |
| 对比口径 | 统一按「Agent 编排 / 策略实现 / 数据流 / 回测 / 风控」五轴横向对比 |
| 方案约束 | 仅采纳能在代码中找到实现依据的机制，推断项显式标注 |

### 0.2 样本完整性

| 项目 | 本地状态 | 说明 |
|---|---|---|
| TradingAgents (TauricResearch) | ✅ 已克隆（TS 重写版） | 以 TypeScript 引擎为核心 |
| TradingAgents-CN | ✅ 已克隆（Python 分支） | 事实上的 A 股适配参考，替代缺失的 TradingAgents-AShare |
| ai-hedge-fund | ✅ 已克隆 | 轻量 AlphaModel 契约范本 |
| StockAgent | ✅ 已克隆 | 多节点分布式 Agent 服务 |
| Vibe-Trading | ✅ 已克隆 | 生产化程度最高（网关探针/治理账本/Swarm） |
| openInvest | ✅ 已克隆 | 委员会协议（Committee Protocol）范本 |
| FinancialAI-Agent | ✅ 已克隆 | 回测审计（截断检验）范本 |
| multi-agent-robo-advisor | ✅ 已克隆 | 投顾适当性与合规链范本 |
| qlib | ✅ 已克隆 | 表达式引擎 + 嵌套回测 |
| FinRL | ✅ 已克隆 | RL 交易环境范本 |
| StockRL | ✅ 已克隆 | 轻量 RL 环境 + 回撤惩罚 |
| vnpy | ✅ 已克隆 | 事件驱动实盘内核范本 |
| awesome-quant-ai | ✅ 已克隆 | 资料库（选型与陷阱索引） |
| **data_analysis_agent** | ❌ 本地缺失 | 未纳入代码级分析，避免空泛推测 |
| **TradingAgents-AShare** | ❌ 本地缺失 | 以 TradingAgents-CN 覆盖其 A 股适配价值 |

> **诚实声明**：缺失的两个项目不做臆测性描述。下文所有机制均有代码依据。

---

## 1. 逐项目深度拆解

### 1.1 TradingAgents（TauricResearch）— 事件化 Agent 运行时

**定位**：多角色投研辩论的「运行时内核」，而非策略库。

**技术栈**：TypeScript + Node（`package.json`）、事件信封（EventEnvelope）+ 契约层 `src/contracts/`。

**架构与数据流**：

```text
用户问题
  → Engine（会话级）            src/engine/engine.ts
      ├─ 路由/采样/限制          src/contracts/limits.ts
      ├─ 主 Agent 运行           src/agents/loop.ts
      │    ├─ 模型步进（tool_calls 循环）
      │    ├─ 预算裁剪            src/agents/budget.ts
      │    ├─ 恢复/重试            src/agents/recovery.ts
      │    └─ 子 Agent 委派        src/engine/delegation.ts
      ├─ 证据库（引用溯源）        src/tools/evidence-store.ts
      ├─ 压缩（长会话）            src/engine/compaction.ts
      ├─ 记忆/结算/恢复            src/engine/{memory,settlement,restore}.ts
      └─ 事件信封（一次运行恰一个终态事件）
```

**关键实现（可复用）**：

1. **预算上限语义**（`src/agents/loop.ts:24-45`）：
   caps = `steps / tokens / turnTokens / timeMs / reserveMs`。原文写明「Caps end the work, **not the answer**」——触顶后停止工具调用，但保留一次**无工具的收尾作答**，并预留 `reserveMs`（默认 min(宽限, 时间预算 1/4)）。
   > 这是最容易做错的一点：多数实现触顶直接抛错，用户体验与可评测性双输。

2. **恰好一个终态事件**（`src/engine/engine.ts:1-15`）：每个 Agent run、每个 turn 只发一个 terminal event，其余为增量事件；客户端可据此做幂等收尾与断线重放。

3. **证据与引用分离**（`src/tools/evidence-store.ts`）：`EvidenceRecord` + `citationsOf()`，模型正文引用编号与证据库解耦，避免"引用即幻觉"。

4. **委派上限**（`src/engine/delegation.ts:148-191`）：主 Agent 通过 `run_agent` 工具调用子 Agent，参数含 brief/evidence，并以 `subagentsPerQuestion` 限制爆炸。

**短板**：
- 无内置量化回测与撮合（纯投研侧）。
- TS 版重写后与 Python 社区生态（qlib/backtrader）割裂，迁移成本高。

**适用场景**：投研问答/报告生成；作为「Agent 运行时」而非「交易系统」。

---

### 1.2 TradingAgents-CN — A 股适配的工程化分支（重点参考）

**定位**：把多空辩论机制落到 A 股数据与中文投研流程的完整实现。

**技术栈**：Python + LangChain（`langchain_core.messages`）+ MongoDB + mem0（向量记忆）+ Streamlit/FastAPI 双前端。

**核心模块**：

| 模块 | 位置 | 职责 |
|---|---|---|
| Agent 基类族 | `core/agents/{analyst,researcher,manager,base}.py` | 分析师/研究员/管理者分层 |
| 适配器 v1/v2 | `core/agents/adapters/*_v2.py` | 持仓侧（基本面/技术面/风险/操作建议）、复盘侧（归因/情绪/择时/持仓） |
| 反思节点 | `core/agents/reflection_helper.py` | LLM 提炼结构化教训 → mem0 |
| 执行轨迹 | `core/agents/execution_trace_recorder.py` | trace_id 级输入/断言记录 |
| 全局话术护栏 | `manager.py` `_apply_global_trading_advice_guard` | 注入合规话术约束 |

**关键实现**：

1. **管理者辩论编排**（`core/agents/manager.py:66-190`）：
   - 构造参数 `debate_rounds`（默认 1）+ `debate_history` 状态；
   - `execute()` 固定五步：提取 ticker/date → `_collect_inputs(state)` → `_conduct_debate()` → 合并 `_get_memory_context()` → 构建 prompt → LLM 决策；
   - `enable_debate=False` 时降级为直接综合（与「1 轮辩论降级」同构）。

2. **反思的降级链**（`core/agents/reflection_helper.py:124-230`）：
   ```text
   LLM 提炼 JSON（30s 超时）
     ├─ 成功 → mem0 写入结构化教训，reflection_success=True
     ├─ 失败/超时 → mem0 写入原文，reflection_success=False
     └─ mem0 不可用 → 跳过并返回 False（不伪造记忆）
   ```
   注意注释明确拒绝「硬编码 provider 兜底」，理由是**掩盖配置问题**——这是很成熟的运维判断。

3. **教训分型**：`memory_kind ∈ {trade_lesson, analysis_lesson, debate_lesson}`，让不同角色的经验互不污染。

4. **同步桥接的线程超时**（`reflection_helper.py:101-118`）：`threading.Thread + join(timeout)` 包装 async，超时降级返回 False，避免阻塞主链路。

**短板与陷阱**：
- 适配器 v1/v2 并存 + `legacy_adapter.py`，历史包袱重，新人上手成本高。
- 管理者单点串行，长辩论链路延迟高（实测类工程常见 30s+）。
- 强依赖 MongoDB + mem0，部署门槛高于课程/小团队场景。

**可移植到本项目的三点**：反思降级链、教训分型、辩论轮数可配置的「1 轮降级」路径。

---

### 1.3 ai-hedge-fund — AlphaModel 契约与确定性仓位合成（重点参考）

**定位**：把「LLM 投资大师」压缩成统一信号接口 + 确定性组合构建。

**技术栈**：Python + pydantic + 自研 pipeline（非 LangGraph 强绑定）。

**核心契约**（`hedge_fund/signals/base.py:17-47`）：

```python
class AlphaModel(ABC):
    investment_approach: ClassVar[Literal["long_only", "long_short"]]  # 权限元数据
    horizon_days: ClassVar[int]                                        # 观点兑现周期

    @abstractmethod
    def predict(self, ticker: str, date: str, data_client: DataClient) -> Signal: ...
```

设计要点（原文注释）：
- `investment_approach` 是**权限**而非观点：只有具备做空权限的模型，其负向观点才能生成空头证据；
- `horizon_days` 让评分卡按各自周期打分（价值投资者按年、盈余漂移按周）；
- 无法形成观点时置 `metadata["abstained"]=True`，**弃权而非伪装中性**。

**确定性仓位合成**（`hedge_fund/portfolio/construction.py:29-105`）：

```python
blend_signals(signals, model_weights, gross_target, mode, investment_approaches)
  → BlendResult(convictions, weights, eligible_scores, flat_reason)
```

规则（代码逐条可验）：
- 弃权票从加权平均中剔除，中性票仍参与投票；
- 空头证据 = Σ w·(value if long_short else max(value,0))；
- `dollar_neutral` 模式：多空各占 `gross_target/2`；任一侧缺失 → `flat_reason` 明确（`missing_long_side` / `missing_short_side`），**不静默生成半仓**；
- 输入校验：权重必须有限且 >0，signal 必须 ∈[-1,1]，否则 `ValueError`。

**LLM 落地**（`signals/llm_agent.py:46-205`）：快照构建 → prompt → 解析 `{signal, confidence, reasoning}` → 折叠为 `Signal(value∈[-1,1])` → 缓存；解析失败/调用失败/数据不足 → 返回 abstain 信号（`test_llm_agents.py:96-123` 有契约测试）。

**短板**：
- 单市场（美股）数据源强绑定，A 股需替换 DataClient；
- 组合层无交易成本/涨跌停/T+1 建模，不能直接当 A 股执行层。

**可移植**：`AlphaModel` 契约 + `blend_signals` 的权限语义 + abstain 纪律。这三者可直接作为本项目「信号层」的规范。

---

### 1.4 StockAgent — 多节点分布式 Agent 服务

**定位**：把 Agent 能力拆成可水平扩展的服务节点。

**技术栈**：Python asyncio + pydantic + gRPC + Web 前置 + MCP 节点。

**节点拓扑**（`AgentServer/main.py:1-93`）：

| NODE_TYPE | 职责 |
|---|---|
| `web` | Web 网关（对外 API/前端） |
| `data_sync` | 数据同步（定时/增量落库） |
| `mcp` | MCP 服务节点（工具面暴露） |
| `inference` | 分析智能体（可多开，`MAX_CONCURRENT_TASKS`） |
| `listener` | 实时监听（事件驱动） |
| `backtest` | 回测计算节点 |

**消息契约**（`core/protocols.py`）：
- `NodeInfo`：node_id / node_type / host / port / status / `current_tasks` / `max_tasks` / `rpc_address`，并提供 `load_ratio` 属性用于调度；
- `NodeHeartbeat`：心跳 + load；
- `AgentTask`：**强制** `task_id` 与 `trace_id`（默认 uuid），字段含 task_type / ts_codes / query / params / user_id。

> 这是本项目最值得借鉴的「分布式化预留」：即使单机部署，也先在数据结构里埋 `trace_id` 与 `node_type`，未来拆服务零改造。

**短板**：节点间一致性依赖外部存储（代码中未见事务/幂等实现细节）；运维复杂度对课程/小团队偏高。

---

### 1.5 Vibe-Trading — 生产化程度最高的交易研究平台（重点参考）

**定位**：从研究、因子、回测、组合到实盘通道的全链路工具箱（PyPI 包 + MCP + FastAPI）。

**规模指标**（`agent/SKILL.md` 自述）：13 个回测引擎、478 个预置 Alpha（qlib158 / alpha101 / gtja191 / academic / fundamental）、89 个技能、30 个多智能体 Swarm、Shadow Account 闭环、28 个行情源（tushare/akshare/baostock/eastmoney/sina/tencent/mootdx/ccxt/futu/mt5…）。

**模块地图**（`agent/src/`）：

```text
agent/          主 Agent 与会话
api/            REST/SSE 装配 + security.py（票据/CORS/回环白名单）
data_gateway/   多源行情网关
providers/      数据源适配
factors/ quantlib/ strategy_store/ strategy_discovery/ research_lab/
backtest/       引擎、约束、风险透视、再平衡掩码、regime
portfolio/      组合优化与约束
live/ trading/ 实盘通道与订单闸门
swarm/          多智能体（checkpoint/context/delivery/evidence_handoff/grounding/presets/runtime/worker）
governance/     账本与清单（ledger.py / manifest.py）
security/ memory/ scheduled_research/ shadow_account/ channels/
```

**关键实现**：

1. **可组合权重约束**（`agent/backtest/constraints.py:1-30`）：
   ```json
   "constraints": [
     {"type":"max_weight","cap":0.25},
     {"type":"min_weight","floor":0.02},
     {"type":"group_exposure","groups":{...},"caps":{"tech":0.6}}
   ]
   ```
   契约：作用于优化器输出的**带符号权重**；按配置顺序执行；只调整已激活标的的幅度，**绝不新开/平掉仓位**；默认关闭保持旧行为；最多 50 轮收敛。这是把「合规/风控约束」做成后置算子的正确姿势。

2. **API 安全面**（`agent/api_server.py` + `src/api/security.py`）：
   - `_mint_sse_ticket` / `_consume_sse_ticket`：SSE 一次性票据（解决 EventSource 无法带 Header 的鉴权难题）；
   - `_is_allowed_loopback_host` / `_reject_cross_site_browser_request` / `_origin_matches_request_host`：本地服务反 CSRF；
   - `_redact_query_secrets`：日志与错误响应脱敏；
   - `_apply_security_headers`：统一安全头。

3. **网关韧性探针**（`tools/gateway_*_probe.py`，20+ 个）：capacity / latency / memory pressure / history recovery / keepalive / crash restart / mixed client。把「行情网关稳定性」变成可回归的测试资产。

4. **治理账本**（`governance/ledger.py` + `manifest.py`）：决策与运行产物留痕，配合 `research_artifacts/` 形成可审计链。

**短板**：
- 体量巨大（3484 文件/106MB），学习曲线陡；
- 多引擎/多数据源带来强配置耦合，默认行为调参成本高；
- 商业闭源倾向（部分能力以 PyPI 分发）。

**可移植**：SSE 票据鉴权、可组合约束层、网关探针清单、governance/ledger 三件套。

---

### 1.6 openInvest — 委员会协议（多 Agent 治理范本，重点参考）

**定位**：把「多 Agent 投研会议」写成**可执行的流程契约**，而非提示词堆叠。

**核心资产**：`plugin/skills/invest/references/committee-protocol.md`

**协议六阶段**（逐条可落地）：

| 阶段 | 契约要点 |
|---|---|
| 0 证据新鲜度 | 检查 `quality_status`；同日 Markdown 只是历史记录，不等于输入/模型/策略仍有效；行情变化后禁止静默复用旧答案 |
| 1 不可变上下文 | `prepare_committee` 返回 `context_id` / `analysis_as_of` / `prompts` / 事实集；角色-输入映射表固定（宏观→Macro；行情/情绪/估值→Quant 与 Risk 每轮；持仓→Risk 与 CIO） |
| 2 独立首轮 | Macro / Quant R1 / Risk R1 在**隔离上下文**并行；保留真实模型身份与原文；**worker 失败必须可见，不得伪造中性意见** |
| 3 交叉质询 | Quant R2 / Risk R2 拿到 R1 全文 + 相同原始输入；任何改判必须解释「新证据/读数纠正/周期变化」；必须同时给出调整后的 signal 与 strength |
| 4 可选多轮 | 轮数由配置上限约束；**保留真实分歧**，不得为达成一致而调信号 |
| 5 CIO 汇总 | 逐字使用 `prompts.cio`；输出 schema 含 `exit_intent` / `hold_reason` / `review_plan` / `risk_review_plan`；模型自报置信度**不等于**校准概率 |
| 6 提交与校验 | 六段原文 + 精确 `context_id`；返回 `verification_required` 时，另派隔离 worker 做推理审查（只查来源/逻辑，不得选交易），且**禁止反复重生成直到出现偏好结论** |

**工程价值**：这是目前开源里对「LLM 多 Agent 决策可信性」最严谨的文本化契约，可直接作为本项目的**辩论层规范**：不可变上下文 + 隔离执行 + 原文留痕 + 禁止收敛伪造 + 事后审查。

**短板**：协议本身不提供撮合/风控执行；`src/openinvest/connectors/web_api/routers/committee.py` 的会话持久化需自建。

---

### 1.7 FinancialAI-Agent — 回测审计与数据源抽象（重点参考）

**定位**：工程纪律最强的单标的日频回测 + 前视偏差审计。

**技术栈**：Python + pydantic + MCP 工具面 + 多数据源 provider。

**回测引擎契约**（`src/finagent/core/backtest/engine.py:1-120`）：

| 设计点 | 实现 |
|---|---|
| 数据源注入 | `BacktestEngine(provider)`，引擎不绑定数据源 |
| A 股摩擦 | 100 股整手 `LOT_SIZE`；双边佣金 `DEFAULT_COMMISSION=0.0003`；卖出印花税 `DEFAULT_STAMP_DUTY=0.0005`；滑点 `DEFAULT_SLIPPAGE=0.0005`；往返成本下限 `MIN_ROUND_TRIP_COST=0.002` |
| 涨跌停 | 主板 10% / 创业板科创板 20% / 北交所 30% |
| 拒绝成交可视 | `skipped` 统计（涨跌停买不进/卖不出、停牌无行情） |
| 成本可复核 | `cost_model` 显式回传，不埋在实现里 |
| 上下文预算 | `equity_curve` 有长度上限（因为会进模型上下文） |
| **审计分离** | `trades` 仅为塞进上下文的最后 12 笔；`all_trades` 才是全量，注释明确禁止用截断数据做一致性判断 |
| 数据截止 | `data_asof`（最后一根 K 线）+ `data_start`（含预热段）证明指标预热充分 |
| 降级可见 | `warnings` + `provider` 字段区分「演示/真实」 |

> 这段 docstring 本身就是一份「回测结果可信性」清单，建议直接作为本项目回测模块的 Review Checklist。

**前视偏差截断检验**（`core/backtest/audit.py:1-110`）：

```text
1) 全区间跑一遍，记录 cut 之前的成交与权益曲线
2) 截断到 cut（默认窗口 70% 处）再跑一遍
3) 两段在 cut 之前必须逐笔一致
```

原理：若策略在 t 日只用 ≤t 数据，那么「之后还有没有数据」不应影响已做决策。不一致即存在泄漏。`LeakReport` 记录 `passed/trades_before_cut/curve_points_compared/leaks/skipped_reason`；多标的策略显式跳过并说明原因。

> 这比「静态扫描 shift(-1)」更强：它是**方法级**保证，对离线 fixture 与真实行情同样有效。建议本项目把它做成 CI 门禁。

**数据源抽象**（`src/finagent/providers/base.py:30-90`）：
- `MarketDataProvider` ABC + `CAPABILITIES` 能力标签 + `require()` 快速失败并给 hint；
- 列名统一小写与源无关：`ts_code/trade_date/open/high/low/close/vol/amount`；
- `OfflineProvider`（确定性 fixture，无密钥）与 `TushareProvider` 可互换——**离线可完整验证工具面**。

**短板**：单标的日频，无组合/多标的引擎（`requires_multi_asset` 策略被跳过）；实盘通道缺失。

---

### 1.8 multi-agent-robo-advisor — 投顾适当性与合规链

**定位**：智能投顾的「适当性 + 合规」最小闭环（Node.js 单文件实现，可读性极高）。

**技术栈**：Node 原生 http + 静态前端，无框架依赖。

**关键实现**：

1. **五档风险画像**（`server.js:8-14`）：保守 R2 / 稳健 R3 / 平衡 R4 / 成长 R4 / 进取 R5，含权益比例、回撤区间、期限、对标指数。

2. **加权评分**（`server.js:59-77`）：
   ```js
   score = round(倾向*0.35 + 亏损容忍*0.30 + 经验*0.20 + 期限*0.15)
   profileFor: ≤25 保守 / ≤45 稳健 / ≤65 平衡 / ≤82 成长 / >82 进取
   ```
   把「主观倾向」与「客观能力」分开加权，比单题直接映射更稳。

3. **合规审查四项**（`server.js:164-184`）：适当性匹配、行业集中度（>33% 报警）、收益表述（不承诺收益）、数据来源与免责声明。返回 `status: passed|attention` + 逐项明细。

4. **行情降级**（`server.js:144-149`）：腾讯 `qt.gtimg.cn`，GB18030 解码；失败返回 502 + 空数组 + 「行情接口暂不可用」，前端回退演示数据（`public/app.js:200-223`）。

**陷阱（务必规避）**：
- `execFile('/usr/bin/curl', ...)` 硬编码 POSIX 路径 → **Windows 直接不可用**；应改用原生 `https`/`fetch` 或跨平台 HTTP 客户端。
- Agent 对话为**静态权重渲染**（`public/app.js:118-134`），并非真实多 Agent 推理；对外表述需谨慎，避免夸大。

---

### 1.9 qlib — 数据表达式引擎与嵌套回测（重点参考）

**定位**：面向因子研究的工业级量化平台。

**技术栈**：Python + NumPy/Pandas + 自研二进制数据格式 + MLflow（实验管理）。

**分层**：

| 层 | 模块 | 关键点 |
|---|---|---|
| 数据 | `qlib/data/{expression_parser,dataset,storage}` | 表达式引擎：AST 运算符白名单（`expression_parser.py:21-94`），只允许注册过的 `Operators.*` |
| 数据集 | `dataset/handler.py:25-64,173-195` | `DataHandlerABC.fetch(selector, level, col_set, data_key)`；`setup_data` 统一索引与缓存 |
| 回测 | `backtest/exchange.py:38-175` | 撮合参数：`limit_threshold` / `volume_threshold` / `open_cost=0.0015` / `close_cost=0.0025` / `min_cost=5.0` / `impact_cost` / `trade_unit=100` |
| 执行 | `backtest/executor.py:310-387` | `NestedExecutor`：日频外层 + 日内内层嵌套决策，复制账户与策略状态 |
| 工作流 | `cli/run.py:85-148` + `workflow/{exp,expm,recorder,record_temp}.py` | `qrun` 渲染 YAML → `task_train` → 实验记录 |

**A 股适配细节（高价值）**：
- 停牌约定：`$close is None` 视为停牌（`exchange.py:32-33`）；
- 交易单位：`trade_unit` 由 `C.trade_unit` 提供，A 股为 100（`exchange.py:107-112,135`）；
- 涨跌停：`limit_threshold` 支持浮点或 `(买入限制表达式, 卖出限制表达式)` 元组，表达式返回 bool；未设置时在 `REG_CN/REG_TW` 下**主动 warning**（`exchange.py:150-155`）——把「静默错误」变成「显式告警」。

**短板**：
- 数据初始化重（需 dump 二进制），冷启动成本高；
- 表达式引擎与平台强耦合，脱离 qlib 复用困难；
- 学习曲线陡，配置驱动导致排障困难。

**可移植**：Exchange 的参数化撮合契约 + 停牌/涨跌停约定 + 未配置即告警的防御式设计。

---

### 1.10 FinRL — RL 交易环境

**定位**：把交易建模为 Gym 环境，算法交给 stable-baselines3。

**技术栈**：Python + gymnasium + stable-baselines3 +（数据）yfinance/alpaca/tushare/joinquant。

**环境契约**（`finrl/meta/env_stock_trading/env_stocktrading.py:19-114`）：

| 参数 | 语义 |
|---|---|
| `hmax` | 单标单单次最大交易现金 |
| `buy_cost_pct` / `sell_cost_pct` | 按标的数组化的双边成本 |
| `turbulence_threshold` | 市场动荡指数超阈值 → 强制清仓（**内建熔断**） |
| `reward_scaling` | 奖励缩放 |
| `state_space` / `action_space` | `Box(-1,1)` 连续动作（负卖正买） |

状态向量 = 现金 + 各标的收盘价 + 各标的持仓 + 技术指标（`tech_indicator_list`）。

**关键实现**：`_sell_stock` 中「价格 >0 才可卖」——用价格缺失隐式表示停牌/无行情（`env_stocktrading.py:115-125`）。

**短板**：
- `finrl/agents/` 仅有 `__init__.py`，算法层实际散落在 examples——**工程化程度低于宣传**；
- 数据源以美股为主，A 股需自接；
- 无交易日历/涨跌停/T+1 建模，直接用于 A 股会高估收益。

**可移植**：`turbulence_threshold` 式「组合级熔断开关」；state 向量的组成顺序（现金/价格/持仓/指标）可作为 RL 扩展的默认约定。

---

### 1.11 StockRL — 轻量 RL 环境与回撤惩罚

**定位**：教学级 RL 交易实验（28 文件）。

**关键实现**：

1. **奖励设计**（`utils/env.py:235-248`）：
   ```python
   reward = total_assets / initial_amount - 1     # 累计收益
   reward += (total_assets / max_total_assets - 1) # 回撤惩罚（负值）
   ```
   注意 `reward` 是**累计口径**而非增量，训练时需配合缩放；`retreat` 项为负，天然惩罚创新低。

2. **动作裁剪**（`utils/env.py:250-269`）：
   ```python
   actions = actions * hmax
   actions = np.where(closings > 0, actions, 0)   # 停牌不交易
   actions = actions / closings                    # 金额→股数
   actions = np.maximum(actions, -holdings)        # 不允许卖超（无做空）
   ```

3. **数据流水线**（`learn/data.py:35-64`）：`Pull_data` → `FeatureEngineer`（amount/change/daily_variance）→ 训练/交易时段切分。

**陷阱**：
- 奖励累计口径导致不同长度 episode 不可比；
- 无手续费/T+1/涨跌停建模；
- 时间切分需人工保证不重叠，否则隐性泄漏。

---

### 1.12 vnpy — 事件驱动实盘内核（重点参考）

**定位**：国内实盘交易基础设施的事实标准。

**技术栈**：Python + 自研事件引擎 + 多 Gateway + PySide 客户端。

**内核契约**：

1. **事件引擎**（`vnpy/event/engine.py:35-147`）：
   - `Queue` 为唯一事件通道，`_thread` 单消费者串行分发；
   - `_timer` 每 1s 生成 `EVENT_TIMER`（供策略定时逻辑）；
   - `register(type, handler)` 按类型注册，`register_general(handler)` 全局监听；handler 异常被捕获避免拖垮引擎。

2. **主引擎生命周期**（`vnpy/trader/engine.py:94-175`）：
   - 启动 EventEngine → 加载 `OmsEngine`（订单/成交/持仓/账户/合约/报价的本地缓存）；
   - `add_gateway` / `add_app` 动态装配；
   - `connect` / `subscribe` / `send_order` / `cancel_order` 统一路由到具体 gateway。

3. **Gateway 抽象**（`vnpy/trader/gateway.py:37-203`）：
   - 明确要求：**线程安全、非阻塞、断线重连**；
   - 抽象方法：`connect/close/subscribe/send_order/cancel_order/query_*`；
   - 推送入口：tick/trade/order/position/account/quote 事件。

**重要事实**：`vnpy_riskmanager`（风控应用）**不在核心仓库**，核心 `vnpy/` 目录仅含 `alpha/chart/event/rpc/trader`。因此「vnpy 自带风控」是常见误解，本项目需自建风控层（可参考其 App 模式）。

**可移植**：EventEngine 的「单队列 + 单消费者 + 类型注册」极简模型；Gateway 的线程/重连契约；OmsEngine 的本地缓存优先原则。

---

### 1.13 awesome-quant-ai — 选型与陷阱索引

**定位**：资料库（非代码库），提供领域地图与前沿索引。

**可提取结论**（`README.md` 目录结构）：
- 量化三支柱：EMH 与可利用无效性的张力、因子有效性与衰减（拥挤/regime shift）、执行成本；
- 章节覆盖：策略族、交易范式对比、2025/2026 前沿、工具平台、论文与书籍；
- 价值：作为本项目「相关工作/选型论证」的引用底稿，避免重复造轮子。

**短板**：不含可执行代码，不能替代工程验证。

---

## 2. 五轴横向对比

### 2.1 多 Agent 协作机制

| 项目 | 编排形态 | 隔离性 | 分歧处理 | 循环上限 | 可审计性 |
|---|---|---|---|---|---|
| TradingAgents (TS) | 主 Agent + 子 Agent 工具委派 | 子 Agent 独立上下文 | 保留原文 | `subagentsPerQuestion` + caps | 事件信封 + 证据库 |
| TradingAgents-CN | 角色类 + 管理者辩论 | 弱（共享 state） | 辩论历史 | `debate_rounds` | 执行轨迹 trace |
| ai-hedge-fund | 信号并行 + 确定性合成 | 强（无共享对话） | abstain 弃权 | 无（单轮） | Signal metadata |
| StockAgent | 多节点 RPC 任务 | 进程级 | N/A | 并发队列 | trace_id 全链路 |
| Vibe-Trading | Swarm（presets/runtime/worker） | 强 | 保留分歧 | round limit | governance ledger |
| openInvest | 委员会六阶段协议 | **强（明确隔离上下文）** | **显式保留 + 禁止收敛伪造** | 配置轮数 | context_id + 原文六段 |
| multi-agent-robo-advisor | 静态权重渲染 | 无 | 无 | 无 | 无 |

**结论**：
- **最佳编排范式** = openInvest 的「不可变上下文 + 隔离执行 + 原文留痕」；
- **最佳降级策略** = TradingAgents 的「caps 结束工作但不结束回答」；
- **最佳信号契约** = ai-hedge-fund 的 AlphaModel（权限 + 周期 + abstain）。

### 2.2 量化策略实现

| 项目 | 策略层形态 | 参数寻优 | 因子体系 | 组合优化 |
|---|---|---|---|---|
| qlib | 配置驱动 workflow | 内置（滚动/网格） | Alpha158/360 | 有（含约束） |
| Vibe-Trading | 478 Alpha + 13 引擎 | 有 | 多体系 | 有（约束层） |
| ai-hedge-fund | LLM 大师 + 量化因子 | 无 | 少量 | 有（确定性合成） |
| FinRL / StockRL | RL 端到端 | 无 | 技术指标 | 无 |
| FinancialAI-Agent | 策略注册表（趋势/震荡/通道/量能/多资产） | 无 | 技术指标 | 无 |
| 其余 | 无策略层 | — | — | — |

### 2.3 多源数据处理

| 项目 | 抽象层级 | 归一化 | 降级策略 | 离线可跑 |
|---|---|---|---|---|
| FinancialAI-Agent | `MarketDataProvider` + CAPABILITIES | 统一小写列名 | Offline↔Tushare 互换 | **是（确定性 fixture）** |
| Vibe-Trading | `data_gateway` + 28 源 | 有 | 多源回退 | 部分 |
| TradingAgents-CN | 工具层封装 AKShare/Tushare | 有 | 有限 | 否 |
| qlib | 二进制 + 表达式 | 强 | 无 | 是（本地 dump） |
| vnpy | Gateway + 数据库抽象 | 强 | 重连 | 是（回放） |
| multi-agent-robo-advisor | 腾讯行情单源 | 弱 | 前端回退 | 是（演示数据） |

### 2.4 回测系统设计

| 项目 | 撮合粒度 | A 股规则 | 成本模型 | 防未来函数 | 结果可信性 |
|---|---|---|---|---|---|
| qlib | 日/分钟 + 嵌套 | 涨跌停/停牌/整手 | 佣金+最低+冲击 | 表达式约束 | 高 |
| FinancialAI-Agent | 日频单标的 | **10/20/30% 涨跌停 + 整手 + 印花税** | 显式 `cost_model` | **截断检验（方法级）** | **最高** |
| vnpy | Tick/Bar 回放 | 需自建 | 佣金/滑点 | 事件时序 | 高（需配置） |
| Vibe-Trading | 13 引擎 | 依赖引擎 | 有 | 有 | 高 |
| FinRL / StockRL | 日频 step | 几乎无 | 百分比成本 | 弱 | 中低 |
| TradingAgents 系 | 无回测 | — | — | — | — |

**结论**：本项目回测层应 = qlib 的撮合参数化 + FinancialAI-Agent 的审计与成本可视 + vnpy 的 tick 回放能力（分阶段建设）。

### 2.5 实时风险控制

| 项目 | 盘前 | 盘中 | 盘后 | 确定性 |
|---|---|---|---|---|
| vnpy | 资金/持仓校验（需自建 App） | 事件驱动委托流 | OmsEngine 对账 | 代码 |
| Vibe-Trading | 权重约束层 | live 通道闸门 | governance ledger | 代码 |
| openInvest | CIO 约束 + `exit_intent` | 计划注册 | review_plan | 协议 + 代码 |
| TradingAgents-CN | 风控经理（LLM） | 无 | 反思 | LLM（弱） |
| FinRL | `turbulence_threshold` 清仓 | 环境内 | 无 | 代码 |
| multi-agent-robo-advisor | 适当性 + 集中度 33% | 无 | 免责校验 | 代码 |

**结论**：LLM 只能做「解释与建议」，**风控执行必须是确定性代码**（Vibe-Trading constraints + vnpy 事件流 + multi-agent-robo-advisor 合规项的合成）。

---

## 3. 可复用组件清单（按能力域）

| # | 能力 | 组件 | 来源（代码位置） | 移植改造点 |
|---|---|---|---|---|
| C1 | Agent 运行时 | Caps 触顶后保收尾作答 | TradingAgents `src/agents/loop.ts:24-45` | 增加「成本上限」维度 |
| C2 | Agent 运行时 | 一次运行恰一个终态事件 | TradingAgents `src/engine/engine.ts:1-15` | SSE 事件契约对齐 |
| C3 | 委派 | 子 Agent 上限 + brief/evidence | TradingAgents `engine/delegation.ts:148-191` | 与辩论角色表绑定 |
| C4 | 辩论 | 不可变上下文 + 六段原文提交 | openInvest `committee-protocol.md` | 落为 `ContextSnapshot` 表 |
| C5 | 辩论 | worker 失败可见、禁止收敛伪造 | 同上 Stage 2/4/6 | 断言 + CI 校验 |
| C6 | 信号 | AlphaModel（权限/周期/abstain） | ai-hedge-fund `signals/base.py:17-47` | 增加 A 股因子实现 |
| C7 | 组合 | 确定性信号合成 + flat_reason | ai-hedge-fund `portfolio/construction.py:29-105` | 接入涨跌停/整手 |
| C8 | 组合 | 可组合权重约束层 | Vibe-Trading `backtest/constraints.py` | 增加行业/板块映射 |
| C9 | 记忆 | 反思降级链（结构化→原文→跳过） | TradingAgents-CN `reflection_helper.py:124-230` | 换 SQLite 向量表 |
| C10 | 记忆 | 教训分型（交易/分析/辩论） | 同上 `memory_kind` | 三类分别注入 |
| C11 | 数据 | Provider + CAPABILITIES + 离线 fixture | FinancialAI-Agent `providers/base.py:30-90` | 接 AKShare/东财 |
| C12 | 数据 | 统一小写列名契约 | 同上 `PRICE_COLUMNS` | 全链路强制 |
| C13 | 回测 | 撮合参数化（涨跌停/整手/最低佣金） | qlib `backtest/exchange.py:38-175` | 复用参数语义 |
| C14 | 回测 | 截断检验（前视偏差方法级审计） | FinancialAI-Agent `backtest/audit.py` | 做成 CI 门禁 |
| C15 | 回测 | 成本/跳过/截止时间可复核 | FinancialAI-Agent `backtest/engine.py:41-119` | 报告字段标准化 |
| C16 | 执行 | EventEngine 单队列单消费者 | vnpy `event/engine.py:35-147` | 与 asyncio 桥接 |
| C17 | 执行 | Gateway 线程/重连契约 | vnpy `trader/gateway.py:37-203` | 先做仿真 Gateway |
| C18 | 风控 | 组合级熔断（turbulence 清仓） | FinRL `env_stocktrading.py:19-35` | 换 A 股流动性指标 |
| C19 | 风控 | 合规四项（适当性/集中度/表述/免责） | multi-agent-robo-advisor `server.js:164-184` | 规则外置为 YAML |
| C20 | 投顾 | 加权评分 + 五档画像 | 同上 `server.js:8-14,59-77` | 题库与权重可配 |
| C21 | API | SSE 一次性票据 | Vibe-Trading `src/api/security.py` | 复用票据模型 |
| C22 | 可观测 | 节点信息 + trace_id 强约束 | StockAgent `core/protocols.py` | 单机也先埋字段 |
| C23 | 治理 | 决策账本 + 产物 manifest | Vibe-Trading `governance/{ledger,manifest}.py` | 哈希链留痕 |
| C24 | 测试 | 网关探针清单 | Vibe-Trading `tools/gateway_*_probe.py` | 抽象为通用 harness |

---

## 4. 已知问题与技术陷阱清单

### 4.1 致命级（会直接导致错误决策或资损）

| # | 陷阱 | 来源证据 | 规避方案 |
|---|---|---|---|
| T1 | 前视偏差（`shift(-1)` / `center=True` / 全样本 z-score） | FinancialAI-Agent `audit.py:1-18` 明示 | 截断检验 CI 门禁（C14）+ 信号 t 收盘算、t+1 成交 |
| T2 | A 股规则缺失导致回测虚高 | FinRL/StockRL 环境无涨跌停/T+1 | 强制 Exchange 参数化（C13），未配置即告警（qlib 模式） |
| T3 | LLM 风控替代确定性风控 | TradingAgents-CN 风控经理为 LLM | 风控执行下沉为代码，LLM 只解释（C19） |
| T4 | 截断数据用于一致性判断 | FinancialAI-Agent `engine.py:75-77` 注释 | 审计用 `all_trades`，上下文用截断 |
| T5 | 行情缺失被当作 0 价成交 | FinRL `_sell_stock` 价格>0 守卫；qlib `$close=None` 即停牌 | 统一「缺失=不可交易」语义 |

### 4.2 严重级（影响稳定性/可维护性）

| # | 陷阱 | 来源证据 | 规避方案 |
|---|---|---|---|
| T6 | 硬编码平台路径 | multi-agent-robo-advisor `server.js:98` `/usr/bin/curl` | 跨平台 HTTP 客户端 |
| T7 | 静默降级掩盖配置错误 | TradingAgents-CN `reflection_helper.py:172-179` 明确拒绝硬编码兜底 | 降级必须留痕 + 健康检查暴露 |
| T8 | 奖励累计口径导致 episode 不可比 | StockRL `env.py:235-248` | 用增量收益或按步归一 |
| T9 | 辩论无限循环 / token 爆炸 | 各项目均设 round limit | 双层上限：轮次 + token + 墙钟 |
| T10 | 适配器 v1/v2 并存的技术债 | TradingAgents-CN `adapters/*_v2.py` + `legacy_adapter.py` | 新项目单一契约 + 版本字段 |
| T11 | 重依赖外部服务（MongoDB/mem0） | TradingAgents-CN | 默认 SQLite，向量能力可选 |
| T12 | 事件引擎 handler 阻塞拖垮全链路 | vnpy `event/engine.py` 单消费者模型 | 慢任务丢线程池，handler 内禁止 IO |

### 4.3 一般级（体验/成本）

| # | 陷阱 | 来源 | 规避 |
|---|---|---|---|
| T13 | 数据初始化成本高（qlib dump） | qlib 二进制格式 | 增量 dump + 缓存分层 |
| T14 | 多源列名漂移 | AKShare 生态通病 | 归一化层 + 版本锁 + 适配单测 |
| T15 | 上下文膨胀 | 各项目 compaction | 结果压缩器 + 结构化摘要 |
| T16 | 依赖 Yahoo/Tushare 限流 | FinRL/多项目 | 多源熔断 + TTL 缓存 |
| T17 | 静态演示被误认为真实推理 | multi-agent-robo-advisor `public/app.js:118-134` | 明确标注演示/模拟 |

---

## 5. 生产级系统技术方案

### 5.1 设计原则

| # | 原则 | 依据 |
|---|---|---|
| P1 | **数据必须来自工具，数字必须可溯源** | TradingAgents 证据库；本项目铁律 |
| P2 | **决策可辩论，执行不可辩论** | openInvest 协议 + vnpy/constraints 确定性执行 |
| P3 | **一切降级可见** | TradingAgents-CN 拒绝静默兜底；FinancialAI-Agent `warnings/provider` |
| P4 | **回测可信性优先于收益率** | FinancialAI-Agent 截断检验；qlib 未配置即告警 |
| P5 | **离线可完整验证** | FinancialAI-Agent OfflineProvider |
| P6 | **单机可跑，分布式可拆** | StockAgent node/trace_id 预留 |
| P7 | **合规是代码，不是提示词** | multi-agent-robo-advisor 合规四项 |

### 5.2 分层架构

```text
┌───────────────────────────────────────────────────────────────┐
│ L5 交互层  Desktop(pywebview) / Mobile(Capacitor) / Web        │
│     SSE v2：message/thinking/tool_call/chart/ask_user/done/…   │
├───────────────────────────────────────────────────────────────┤
│ L4 决策层  DebateOrchestrator（不可变上下文 + 隔离角色 + 审计）  │
│     ResearchAgent · SignalAgent · AdvisorAgent · RiskExplainer │
├───────────────────────────────────────────────────────────────┤
│ L3 策略层  AlphaModel 契约 · 组合合成 · 约束层 · 策略注册表       │
│     Walk-forward · 截断审计 · 策略晋升门禁                       │
├───────────────────────────────────────────────────────────────┤
│ L2 执行层  OrderRouter → RiskGate(三道闸) → ExecutionGateway    │
│     PaperBroker（仿真）· LiveBroker（券商）· OMS 本地缓存        │
├───────────────────────────────────────────────────────────────┤
│ L1 数据层  Provider 抽象 · 多源熔断 · 归一化 · 快照/预计算       │
│     Tick/Bar 缓存 · 交易日历 · 停牌/涨跌停质检                  │
├───────────────────────────────────────────────────────────────┤
│ L0 基础设施 EventEngine · SQLite(WAL) · 日志脱敏 · 成本账本      │
│     治理账本(ledger) · 密钥三级读取 · 健康检查                   │
└───────────────────────────────────────────────────────────────┘
```

### 5.3 模块定义（M0–M12）

| 模块 | 职责 | 依赖 | 关键产出 |
|---|---|---|---|
| M0 基础设施 | 事件总线、DB、日志、密钥、成本账本、治理账本 | — | `EventEngine` / `Ledger` |
| M1 数据接入 | Provider 抽象、多源熔断、归一化、限速 | M0 | `MarketDataProvider` 实现族 |
| M2 数据质检 | 停牌/涨跌停/缺列/新鲜度 | M1 | `QualityReport` |
| M3 快照与预计算 | 全市场快照、指标预计算、缓存分层 | M1,M2 | `SnapshotService` / `IndicatorStore` |
| M4 工具层 | 工具注册、参数沙箱、结果压缩 | M1–M3 | `ToolRegistry` |
| M5 Agent 运行时 | 主循环、caps、委派、压缩、记忆 | M4 | `AgentRuntime` |
| M6 辩论编排 | 不可变上下文、隔离角色、六段审计 | M5 | `DebateRun` |
| M7 信号与组合 | AlphaModel、信号合成、约束层 | M3,M4 | `Signal` / `PortfolioTarget` |
| M8 回测引擎 | 撮合、成本、A 股规则、嵌套执行 | M3,M7 | `BacktestResult` |
| M9 审计与晋升 | 截断检验、walk-forward、晋升门禁 | M8 | `LeakReport` / `PromotionDecision` |
| M10 执行与风控 | 订单路由、三道闸、仿真/实盘网关、OMS | M2,M7,M9 | `Order` / `RiskDecision` |
| M11 投顾与合规 | 适当性测评、组合建议、合规链 | M1,M7 | `AdviceReport` |
| M12 API 与前端 | SSE v2、鉴权、看板、三端 | M4–M11 | 事件流 / UI |

### 5.4 核心接口规范

**5.4.1 数据源（移植 FinancialAI-Agent + 本项目现有实现）**

```python
class MarketDataProvider(Protocol):
    name: str
    CAPABILITIES: frozenset[str]

    def supports(self, cap: str) -> bool: ...
    def require(self, cap: str) -> None: ...          # 不支持即抛 ProviderUnavailable(hint=...)

    def stock_basic(self, symbol: str | None = None) -> pd.DataFrame: ...
    def trade_calendar(self, start: str, end: str) -> pd.DataFrame: ...
    def daily_bar(self, symbol: str, start: str, end: str, adj: str = "hfq") -> pd.DataFrame: ...
    def realtime_quote(self, symbols: list[str]) -> list[Quote]: ...
    def fundamentals(self, symbol: str, periods: int = 8) -> pd.DataFrame: ...

# 列名契约（全链路强制，禁止中文列名下沉）
PRICE_COLUMNS = ["symbol", "trade_date", "open", "high", "low", "close", "volume", "amount"]
```

**5.4.2 信号（移植 ai-hedge-fund，A 股化）**

```python
class AlphaSignal(BaseModel):
    model_name: str
    symbol: str
    as_of: datetime
    value: float                     # [-1, 1]
    confidence: float                # [0, 1]，仅作排序权重，不代表概率
    horizon_days: int
    evidence: list[EvidenceRef]      # 每条含 tool/args/ts/excerpt
    abstained: bool = False          # 弃权而非伪装中性

class AlphaModel(Protocol):
    name: str
    investment_approach: Literal["long_only", "long_short"]
    default_horizon_days: int
    async def predict(self, symbol: str, as_of: date, ctx: DataContext) -> AlphaSignal: ...
```

**5.4.3 组合合成（确定性，禁止 LLM 参与）**

```python
def blend_signals(
    signals: list[AlphaSignal],
    weights: Mapping[str, float],          # 模型权重，必须有限且 >0
    gross_target: float,                   # 总仓位
    mode: Literal["long_only", "long_short", "dollar_neutral"],
    approaches: Mapping[str, str],
) -> BlendResult:                          # convictions / weights / eligible / flat_reason
```

**5.4.4 约束层（移植 Vibe-Trading constraints）**

```python
class WeightConstraint(Protocol):
    def apply(self, weights: pd.DataFrame) -> pd.DataFrame: ...

# 内置：MaxWeight(cap) / MinWeight(floor) / GroupExposure(groups, caps) / TurnoverCap(max)
# 契约：保持符号；只调整已激活标的；按配置顺序；最多 N 轮；不新开/平仓
```

**5.4.5 回测（qlib 参数语义 + FinancialAI-Agent 审计字段）**

```python
@dataclass
class ExchangeConfig:
    deal_price: str = "close"
    limit_threshold: float | tuple[str, str] | None = 0.10   # 主板；创业板 0.20；北交所 0.30
    trade_unit: int | None = 100
    commission: float = 0.00025
    min_commission: float = 5.0
    stamp_duty: float = 0.0005          # 仅卖出
    slippage: float = 0.0005
    impact_cost: float = 0.0

@dataclass
class BacktestResult:
    metrics: dict                       # 收益/年化/夏普/回撤/胜率/盈亏比
    cost_model: dict                    # 成本可复核
    skipped: dict[str, int]             # 涨跌停/停牌拒绝成交
    equity_curve: list[dict]            # 有长度上限
    trades_tail: list[dict]             # 仅供上下文（截断）
    all_trades: list[dict]              # 审计全量
    data_asof: str
    data_start: str
    provider: str
    warnings: list[str]
```

**5.4.6 执行与风控（vnpy Gateway + 三道闸）**

```python
class ExecutionGateway(Protocol):
    """线程安全、非阻塞、可重连。"""
    def connect(self, setting: dict) -> None: ...
    def subscribe(self, symbols: list[str]) -> None: ...
    def send_order(self, req: OrderRequest) -> str: ...
    def cancel_order(self, order_id: str) -> None: ...
    def query_position(self) -> list[Position]: ...
    def query_account(self) -> Account: ...

class RiskGate(Protocol):
    def pre_trade(self, order: OrderRequest, ctx: AccountContext) -> RiskDecision: ...
    def on_tick(self, tick: Tick) -> list[RiskAction]: ...   # 熔断/减仓/告警
    
@dataclass
class RiskDecision:
    allowed: bool
    adjusted_qty: int
    reasons: list[str]
    severity: Literal["block", "warn", "pass"]
```

**三道闸定义**：

| 闸 | 触发点 | 规则（确定性代码） |
|---|---|---|
| G1 适当性闸 | 下单前 | 风险等级 vs 权益上限；单一标的/行业上限；禁止 R1 配置股票 |
| G2 资金与仓位闸 | 下单前 | 可用资金、整手、单笔上限、总仓位上限、当日亏损阈值 |
| G3 市场状态闸 | 盘中 tick | 涨跌停不可成交、停牌、流动性不足、组合级熔断（turbulence 类） |

**5.4.7 研究闭环（策略自动迭代）**

```python
class StrategySpec(BaseModel):
    id: str; version: str
    universe: list[str]
    alpha_models: list[str]
    params: dict
    rebalance: str                  # daily|weekly|monthly
    exchange: ExchangeConfig
    constraints: list[dict]

class ResearchPipeline(Protocol):
    def walk_forward(self, spec: StrategySpec, folds: int) -> WfReport: ...
    def leak_audit(self, spec: StrategySpec) -> LeakReport: ...        # 截断检验
    def evaluate(self, spec: StrategySpec) -> EvalReport: ...          # OOS 夏普/IC/换手
    def promote(self, spec, report) -> PromotionDecision: ...          # 门禁
```

**晋升门禁（建议阈值，可按风控偏好调整）**：

| 指标 | 门槛 |
|---|---|
| 截断检验 | 必须 `passed=True`，否则一票否决 |
| 样本外夏普 | > 0.5 且 > 样本内 30%（衰减不过快） |
| IC 显著性 | \|t\| > 2（多期） |
| 换手率 | 日均 < 30%（控成本） |
| 最大回撤 | < 15%（组合级） |
| 数据覆盖 | 回测区间 ≥ 3 年，含至少 1 次熊市 |

### 5.5 A 股特性适配矩阵

| 特性 | 规则 | 落点 |
|---|---|---|
| 涨跌停 | 主板 10% / 创业板科创板 20% / ST 5% / 北交所 30% | ExchangeConfig 分板块查表 |
| T+1 | 当日买入不可卖 | 持仓账本记录买入日，卖出前校验 |
| 整手 | 100 股 | 下单前 `qty // 100 * 100` |
| 印花税 | 0.05% 仅卖出 | 佣金策略分买卖分支 |
| 最低佣金 | 5 元 | `max(turnover*rate, 5)` |
| 停牌 | 缺失/量 0 → 不可交易 | 质检层统一语义 |
| 集合竞价 | 09:15–09:25；连续竞价 09:30–11:30 / 13:00–15:00 | 交易日历 + 时段判断 |
| 复权 | 回测统一后复权 hfq | Provider 参数 |
| 交易日历 | 跨年补抓 + 本地缓存 | Calendar 服务 |
| 时区 | Asia/Shanghai 单一事实源 | 全局时钟 |
| 北交所 | 30% 涨跌幅、代码 8/4 开头 | 板块识别 |

### 5.6 实时行情接入方案

```text
多源适配（东财/新浪/腾讯/Level-2 券商）
  → 归一化（统一 tick schema）
  → 质检（时间戳单调、价格>0、量非负、跳变检测）
  → 分发（进程内 EventEngine + 对外 SSE/WebSocket）
  → 缓存（最新 tick + N 分钟 bar 聚合）
  → 降级（源 A 失败 3 次 → 熔断 5min → 源 B；全断 → 标记 stale）
```

**Tick 契约**：

```python
@dataclass(frozen=True)
class Tick:
    symbol: str
    ts: datetime          # 交易所时间
    last: float
    volume: int
    amount: float
    bid1: float; ask1: float
    bid1_vol: int; ask1_vol: int
    source: str
    stale: bool = False
```

**关键约束**：
- 快照/全市场轮询在**盘中固定频率**（如 3s），避免被风控；
- Level-2 仅在实盘阶段接入，仿真阶段用快照 + 合成盘口；
- 所有下游必须处理 `stale=True`（不可用于下单决策）。

### 5.7 模拟交易验证

| 阶段 | 形态 | 数据 | 目的 |
|---|---|---|---|
| S1 离线回放 | Bar 回放 | 历史 hfq | 策略有效性与成本 |
| S2 仿真撮合 | PaperBroker（撮合队列 + 滑点模型） | 实时快照 | 验证执行链路与时延 |
| S3 影子账户 | 真实行情 + 虚拟资金 + 完整风控 | 实时 tick | 验证风控闸与告警 |
| S4 小资金实盘 | LiveBroker + 人工确认 | 实时 tick | 验证券商通道与对账 |

**PaperBroker 撮合规则（建议）**：
- 市价单：以对手价 + 滑点成交；涨停不可买、跌停不可卖；
- 限价单：价格穿越才成交；部分成交按可成交量；
- 停牌：挂单不成交，收盘撤单；
- 每日对账：`现金 + 持仓市值 = 期初 + 已实现盈亏 - 费用`。

### 5.8 技术选型决策表

| 层 | 选型 | 决策依据 | 淘汰项与理由 |
|---|---|---|---|
| 语言 | Python 3.12（后端）+ TypeScript（前端） | 量化生态 + 前端类型安全 | Go/Rust：生态不足 |
| Web | FastAPI + uvicorn | SSE 原生友好、异步 | Flask：无原生异步流 |
| Agent 编排 | 自研轻量状态机 + 工具注册表 | 避免 LangGraph 版本锁定；可控 caps | LangGraph：抽象重、调试难（TradingAgents-CN 体感） |
| LLM | 双供应商 tier 路由（主/经济） | 成本 + 可用性 | 单供应商：单点风险 |
| 数据 | AKShare/东财为主，Tushare 备，腾讯/新浪兜底 | 免费 + A 股覆盖 | 单一源：限流/停服 |
| 存储 | SQLite(WAL) 起步，PostgreSQL 演进 | 零运维 → 可扩展 | MongoDB：部署重（T11） |
| 向量记忆 | 可选（先结构化表 + 关键词） | 降低依赖 | mem0 强绑：部署门槛 |
| 回测 | 自研事件驱动 + Backtrader 兼容层 | A 股规则可控 + 生态复用 | 纯 Backtrader：规则改造受限 |
| 组合优化 | PyPortfolioOpt（Ledoit-Wolf） | 教科书正确性 | 手写协方差：易错 |
| 因子 | 自研 + qlib Alpha 参考 | 避免 qlib 重初始化 | 全量 qlib：冷启动成本（T13） |
| RL（可选） | stable-baselines3 | 成熟稳定 | 自研 RL：成本高 |
| 实盘 | 券商 Gateway（仿真优先） | 风险可控 | 直接实盘：资损风险 |
| 部署 | 单机 onedir + 局域网；云端后置 | 课程/小团队现实 | K8s：过度设计 |

### 5.9 数据模型（核心表）

```sql
-- 决策与辩论
CREATE TABLE debate_runs(
  id TEXT PRIMARY KEY, symbol TEXT, as_of TEXT, context_id TEXT,
  status TEXT, verdict TEXT, created_at TEXT);
CREATE TABLE debate_sections(
  run_id TEXT, role TEXT, round INTEGER, raw_text TEXT, model TEXT,
  signal REAL, strength REAL, created_at TEXT,
  PRIMARY KEY(run_id, role, round));

-- 信号与组合
CREATE TABLE alpha_signals(
  id INTEGER PRIMARY KEY, model_name TEXT, symbol TEXT, as_of TEXT,
  value REAL, confidence REAL, horizon_days INTEGER, abstained INTEGER,
  evidence_json TEXT);
CREATE TABLE portfolio_targets(
  id INTEGER PRIMARY KEY, strategy_id TEXT, as_of TEXT,
  weights_json TEXT, constraints_json TEXT, flat_reason TEXT);

-- 回测与审计
CREATE TABLE backtest_runs(
  id TEXT PRIMARY KEY, strategy_id TEXT, spec_hash TEXT,
  start_date TEXT, end_date TEXT, metrics_json TEXT, cost_model_json TEXT,
  skipped_json TEXT, data_asof TEXT, provider TEXT, warnings_json TEXT);
CREATE TABLE leak_reports(
  id INTEGER PRIMARY KEY, backtest_id TEXT, cut_date TEXT, passed INTEGER,
  trades_before_cut INTEGER, leaks_json TEXT, skipped_reason TEXT);

-- 执行与风控
CREATE TABLE orders(
  id TEXT PRIMARY KEY, strategy_id TEXT, symbol TEXT, side TEXT, qty INTEGER,
  price REAL, order_type TEXT, status TEXT, risk_decision_json TEXT,
  broker_order_id TEXT, created_at TEXT, updated_at TEXT);
CREATE TABLE risk_events(
  id INTEGER PRIMARY KEY, level TEXT, gate TEXT, rule TEXT, detail TEXT,
  symbol TEXT, order_id TEXT, created_at TEXT);

-- 治理
CREATE TABLE ledger(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, ref_id TEXT,
  payload_hash TEXT, prev_hash TEXT, created_at TEXT);
```

---

## 6. 开发路线

### 6.1 模块依赖 DAG

```text
M0 基础设施
 ├─→ M1 数据接入 ─→ M2 质检 ─→ M3 快照/预计算
 │                              ├─→ M4 工具层 ─→ M5 Agent 运行时 ─→ M6 辩论
 │                              └─→ M7 信号/组合
 │                                     ├─→ M8 回测 ─→ M9 审计/晋升
 │                                     └─→ M10 执行/风控
 └─→ M12 API/前端（依赖 M4–M11 的事件契约）
M11 投顾/合规 ← M1, M7
```

**关键路径**：`M0 → M1 → M3 → M4 → M5 → M6`（投研主线）；`M0 → M1 → M7 → M8 → M9 → M10`（交易主线）。

### 6.2 优先级排序

| 优先级 | 模块 | 理由 |
|---|---|---|
| **P0** | M0, M1, M2, M3, M4 | 数据链是所有上层的地基；50ms 链路先通 |
| **P0** | M8 回测（基础版）+ M9 截断审计 | 无审计的回测等于自欺（T1） |
| **P1** | M5, M6 辩论 | 核心差异化能力 |
| **P1** | M7 信号/组合 + 约束层 | 从"会说"到"会配" |
| **P1** | M10 仿真执行 + G1/G2 闸 | 交易闭环最小可用 |
| **P2** | M10 实盘 Gateway + G3 盘中闸 | 资损风险最高，最后上 |
| **P2** | M11 投顾/合规 | 体验线，可并行 |
| **P3** | M9 walk-forward 全量 + 策略自动迭代 | 依赖历史数据积累 |
| **P3** | RL/多引擎扩展 | 锦上添花 |

### 6.3 五阶段里程碑与验收标准

#### 阶段一：可信数据链（P0）

| 项 | 内容 |
|---|---|
| 交付 | M0–M4；Provider 三源（东财/新浪/离线 fixture）；快照服务；工具注册 + 沙箱 |
| 验收 | ① 快照命中工具 P50 < 50ms（×100 次）；② 离线 fixture 可完整跑通全部工具；③ 停牌/涨跌停三档板单测覆盖；④ 多源熔断状态机单测；⑤ 50 个畸形参数 100% 拒绝 |
| 依赖 | 无 |

#### 阶段二：可信决策（P0/P1）

| 项 | 内容 |
|---|---|
| 交付 | M5 Agent 运行时（caps/委派/压缩）+ M6 辩论（不可变上下文/隔离角色/六段审计）+ M9 截断审计（CI 门禁） |
| 验收 | ① 一次 run 恰一个终态事件（协议测试）；② caps 触顶仍有收尾作答；③ 辩论六段原文可回放且 `context_id` 一致；④ 截断检验在 3 个基准策略上 `passed=True`；⑤ 引用编号与证据 1:1 交叉校验 |
| 依赖 | 阶段一 |

#### 阶段三：策略与组合（P1）

| 项 | 内容 |
|---|---|
| 交付 | M7 AlphaModel 契约 + blend_signals + 约束层（max/min/group/turnover）+ M8 回测（A 股四规则 + 成本可视） |
| 验收 | ① 佣金/印花税/T+1/涨跌停四规则注入用例全绿；② `cost_model`/`skipped`/`data_asof` 字段齐全；③ 约束层保持符号且不新开仓（单测）；④ 回测数字可复现（同输入同输出）；⑤ 样本外夏普与样本内对照报告生成 |
| 依赖 | 阶段一、二 |

#### 阶段四：仿真交易与风控（P1/P2）

| 项 | 内容 |
|---|---|
| 交付 | M10 PaperBroker + OrderRouter + G1/G2/G3 三闸 + OMS + 风控事件看板 |
| 验收 | ① 涨停买不进/跌停卖不出/停牌不成交（注入用例）；② 单笔/单标的/行业/总仓位四类限额触发即拒；③ 当日亏损阈值触发熔断并告警；④ 每日对账恒等式成立；⑤ 风控事件 100% 落库可回溯 |
| 依赖 | 阶段三 |

#### 阶段五：实盘接入与迭代（P2/P3）

| 项 | 内容 |
|---|---|
| 交付 | 券商 Gateway（仿真→小资金）+ 策略自动迭代流水线 + 治理账本 + 多端 UI |
| 验收 | ① Gateway 断线重连 10 次无丢单（探针）；② 影子账户连续 20 交易日无风控穿透；③ 策略晋升门禁自动执行（截断/夏普/IC/换手/回撤五项）；④ 治理账本哈希链可验证；⑤ 全链路 P95 延迟 < 1s（下单到回报） |
| 依赖 | 阶段四 |

### 6.4 风险登记

| 风险 | 概率 | 影响 | 对策 |
|---|---|---|---|
| 数据源限流/封禁 | 高 | 高 | 多源熔断 + 固定频率轮询 + 离线 fixture 兜底 |
| LLM 输出不稳定 | 高 | 中 | 结构化 Schema + 重试 1 次 + 降级模板 |
| 回测过拟合 | 高 | 高 | 截断审计 + walk-forward + 晋升门禁 |
| 实盘通道故障 | 中 | 高 | 仿真先行 + 断线重连探针 + 人工确认开关 |
| 成本超支 | 中 | 中 | tier 路由 + 缓存 + 日成本熔断 |
| 合规风险 | 中 | 高 | 确定性合规链 + 三层免责 + 教学定位声明 |

---

## 7. 结论

1. **最值得直接移植的三个契约**：ai-hedge-fund 的 `AlphaModel`（信号层）、FinancialAI-Agent 的截断检验（审计层）、openInvest 的委员会协议（辩论层）。
2. **最值得警惕的三个陷阱**：前视偏差（T1）、A 股规则缺失（T2）、LLM 替代确定性风控（T3）。
3. **架构取舍**：不做「大而全的 Agent 框架」，做「窄而深的可信链路」——数据可信 → 决策可审计 → 执行可风控。
4. **落地节奏**：先让回测说真话（阶段一/三），再让 Agent 说人话（阶段二），最后让资金动起来（阶段四/五）。

> 本报告所有机制均可在附录 A 的代码位置复核；推断性结论已在正文标注。

---

## 附录 A：代码证据索引

| 编号 | 项目 | 文件:行 | 结论 |
|---|---|---|---|
| A1 | TradingAgents | `src/agents/loop.ts:24-45` | caps 触顶后保留无工具收尾作答 + reserveMs |
| A2 | TradingAgents | `src/engine/engine.ts:1-15` | 一次 run/turn 恰一个终态事件 |
| A3 | TradingAgents | `src/engine/delegation.ts:148-191` | run_agent 委派 + subagentsPerQuestion 上限 |
| A4 | TradingAgents | `src/tools/evidence-store.ts` | EvidenceRecord + citationsOf 证据溯源 |
| A5 | TradingAgents-CN | `core/agents/manager.py:66-190` | 管理者辩论：debate_rounds/collect/debate/memory/prompt |
| A6 | TradingAgents-CN | `core/agents/reflection_helper.py:124-230` | 反思降级链 + memory_kind 分型 + 拒绝硬编码兜底 |
| A7 | TradingAgents-CN | `core/agents/adapters/*_v2.py` | 持仓/复盘适配器 v1/v2 并存（技术债） |
| A8 | ai-hedge-fund | `hedge_fund/signals/base.py:17-47` | AlphaModel：investment_approach/horizon_days/abstain |
| A9 | ai-hedge-fund | `hedge_fund/portfolio/construction.py:29-105` | blend_signals 权限语义 + flat_reason + 输入校验 |
| A10 | ai-hedge-fund | `signals/llm_agent.py:46-205` | LLM 折叠为 Signal + 失败 abstain |
| A11 | StockAgent | `AgentServer/main.py:1-93` | NODE_TYPE 六类节点 |
| A12 | StockAgent | `core/protocols.py` | NodeInfo.load_ratio / AgentTask 强制 trace_id |
| A13 | Vibe-Trading | `agent/backtest/constraints.py:1-30` | 可组合权重约束（保符号/不新开仓/50 轮） |
| A14 | Vibe-Trading | `agent/src/api/security.py` + `api_server.py` | SSE 票据/CORS/回环白名单/脱敏 |
| A15 | Vibe-Trading | `tools/gateway_*_probe.py` | 网关韧性探针清单 |
| A16 | Vibe-Trading | `agent/src/governance/{ledger,manifest}.py` | 治理账本与产物清单 |
| A17 | Vibe-Trading | `agent/SKILL.md` | 13 引擎/478 Alpha/89 技能/30 Swarm/28 数据源 |
| A18 | openInvest | `plugin/skills/invest/references/committee-protocol.md:1-72` | 六阶段委员会协议（不可变上下文/隔离/禁止收敛） |
| A19 | FinancialAI-Agent | `core/backtest/engine.py:1-120` | A 股摩擦/成本可视/skipped/all_trades/data_asof |
| A20 | FinancialAI-Agent | `core/backtest/audit.py:1-110` | 截断检验（前视偏差方法级审计） |
| A21 | FinancialAI-Agent | `providers/base.py:30-90` | Provider + CAPABILITIES + 统一列名 + 离线 fixture |
| A22 | multi-agent-robo-advisor | `server.js:8-14,59-77` | 五档画像 + 加权评分 35/30/20/15 |
| A23 | multi-agent-robo-advisor | `server.js:164-184` | 合规四项（适当性/集中度 33%/表述/免责） |
| A24 | multi-agent-robo-advisor | `server.js:93-111` | 腾讯行情 GB18030 + 硬编码 curl 陷阱 |
| A25 | qlib | `backtest/exchange.py:38-175` | Exchange 参数化 + 停牌/整手/涨跌停 + 未配置告警 |
| A26 | qlib | `data/expression_parser.py:21-94` | 表达式运算符白名单 |
| A27 | qlib | `data/dataset/handler.py:25-64` | DataHandlerABC.fetch 统一契约 |
| A28 | qlib | `backtest/executor.py:310-387` | NestedExecutor 嵌套决策 |
| A29 | qlib | `cli/run.py:85-148` + `workflow/` | qrun 配置化工作流 |
| A30 | FinRL | `meta/env_stock_trading/env_stocktrading.py:19-114` | Gym 环境 + turbulence 熔断 |
| A31 | FinRL | `finrl/agents/__init__.py` | 算法层实际缺失（工程化低于宣传） |
| A32 | StockRL | `utils/env.py:235-269` | 累计收益 + 回撤惩罚；动作裁剪防卖超 |
| A33 | StockRL | `learn/data.py:35-64` | 数据流水线（下载→特征→切分） |
| A34 | vnpy | `event/engine.py:35-147` | 单队列单消费者 + timer 事件 + 类型注册 |
| A35 | vnpy | `trader/engine.py:94-175` | MainEngine 生命周期 + OmsEngine 缓存 |
| A36 | vnpy | `trader/gateway.py:37-203` | Gateway 线程安全/非阻塞/重连契约 |
| A37 | vnpy | `vnpy/` 目录结构 | 风控 App 不在核心仓库（需自建） |
| A38 | awesome-quant-ai | `README.md:1-45` | 量化三支柱与资料地图 |
