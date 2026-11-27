# 哦经金智能体 · Oh EcoFin Agent

> **教学演示用途：所有输出不构成任何投资建议，不提供任何形式的投资建议服务。**
> 全栈国产化 A 股投研智能体：国产模型（GLM-5.3-Flash / DeepSeek）+ 国产数据源（AKShare/东财）+ A 股 + 国产新闻源。
> 平台：Windows 桌面端（pywebview）+ Android（Capacitor）+ 浏览器，**一套前端三端可达**。

## 核心功能

| 功能 | 内容 |
|---|---|
| **智能投研** | 单股四维分析 / 2–5 股对比（相关性矩阵）/ 新闻情绪汇总 / **多空辩论式综合研判** / 决策记忆与复盘 |
| **智能投顾** | 对话式风险测评（5 题 ask_user 挂起）→ Markowitz 优化（Ledoit-Wolf）→ 确定性合规审查链 → 有效前沿图 |
| **量化分析** | Backtrader 双均线回测（A 股四规则完整建模）+ Walk-forward 滚动验证 + 多因子 IC 验证 |
| **数据分析** | 数据集导入 → 描述/缺失/相关/趋势/分组/漏斗/A-B 七个确定性模块 → 可复现 ECharts 图表 → PNG/HTML 导出 |
| **报告导出** | 分析/SQL/文本块 → 可复现 report spec → HTML/Word/Excel，图表与数字均带数据集 sha 和结果哈希 |
| **可信审计** | 证据账本 + 数字抽取/比对 + G1–G5 闸门 + 重跑对比；缺证据报告直接阻断 |

## 数据基座（M1–M3）

| 层 | 能力 | 入口 |
|---|---|---|
| 标的目录 | FinanceDatabase 30 万+ 标的版本化快照，按代码/名称/行业/交易所检索 | `python scripts/import_catalog.py --source <FinanceDatabase 根目录>`；`GET /api/catalog/search` |
| 数据集 | CSV/Excel/Parquet → 不可变 Parquet 快照 + manifest（sha256/行/列/schema） | `python scripts/import_dataset.py --path <文件>`；`POST /api/datasets`；`GET /api/datasets/{id}/profile` |
| 查询 | DuckDB 只读 + SQL Guard（仅 SELECT/WITH）+ 行数/超时限制 + 查询审计 | `POST /api/query`；`GET /api/query/audit` |

Agent 工具面同步新增：`catalog_search` / `dataset_list` / `dataset_profile` / `sql_query`。

## 分析工作台（M4）

- 七个内置模块：描述统计 / 缺失分析 / 相关性 / 趋势 / 分组聚合 / 漏斗 / A-B 测试；
- 图表是**纯 JSON 的 ECharts option**，与 `result_hash`、`chart.spec_hash` 一起返回，同一数据集与参数重跑哈希一致；
- 前端「数据分析」页支持数据集选择、参数表单、结果表、图表与 **PNG/HTML 导出自包含**（HTML 内嵌 PNG，无外部 CDN）；
- API：`GET /api/analysis/modules`、`POST /api/analysis/run`；Agent 工具：`analysis_modules`、`analysis_run`。

## 报告与导出（M5）

- 报告由分析/SQL/文本块组成；服务端**重新执行**分析/SQL 并记录每块的数据集版本、sha256、`result_hash`、图表 `spec_hash`；
- `POST /api/reports` 一次生成 report spec + HTML + Word + Excel；`GET /api/reports`、`GET /api/reports/{id}`、`GET /api/reports/{id}/export/{html|docx|xlsx}`；
- HTML 为单文件（内嵌 PNG 图表，无外部 CDN）；Word 嵌入 PNG；Excel 使用原生图表与条件格式，均可离线打开；
- 前端「数据分析」页可将多次分析加入报告草稿并生成；「报告」页集中下载三种格式；
- Agent 工具：`report_compose`（仅在用户明确要求生成/导出报告时调用）。

## Agent 编排（M6）

- 显式状态机：`planning → acting → observing → planning … → synthesizing → done`，ask_user 挂起为 `waiting_user`，异常/预算耗尽为 `failed`；
- **一次 run 恰一个 `done` 终态事件**；`error`/`budget` 只是过程告警，前端以 `done` 收口；
- ToolRegistry 增加副作用元数据（`none/session/workspace/external`）与显式授权要求：`submit_paper_order`、`report_compose` 只有在用户消息含明确交易/报告意图时才执行，被拦截的调用不执行、不自动重试；
- 上下文预算两级压缩：先压缩工具结果（表格/列表截断、只留哈希与摘要），再丢弃最老对话单元；仍超预算则确定性收尾（`stop_reason=context_budget`）；
- 原始工具参数/结果落 `tool_calls` 表（上下文压缩只影响模型输入）；`/api/runs` 与 `agent_runs` 表记录 run_id、状态、停止原因、轮次、工具错误、token/成本，为 M7 证据与重跑提供基础；
- 工具失败通过 `tool_call.error`、`tool_stats.errors` 与 `done` 三处可见；Mock LLM 可完整回归。

## 可信审计（M7）

- **证据账本**：分析/SQL 表格的每个数值单元格、分析结果数值标量、图表 spec 都登记为 evidence（来源/定位/摘录/value_hash/value_num）；
- **数字抽取**：支持百分比、千分位、万/亿/千；屏蔽日期、时间、股票代码、年份、列表序号与 HTML 脚本；
- **质量闸门**：G1_SOURCE / G2_EVIDENCE / G3_NUMBERS / G4_CHART / G5_MANIFEST，任一失败即 `blocked`；
- **缺证据阻断**：报告数字（如 99999）找不到证据时，报告保存为 `blocked`，不生成 HTML/Word/Excel；
- **run manifest 与重跑**：报告内置 manifest（数据集 sha、块 result_hash/chart_spec_hash/sql_hash）；`POST /api/reports/{id}/rerun` 重跑并比较 spec_hash/哈希；
- API：`GET /api/audit/reports`、`GET /api/audit/reports/{id}`；前端「审计」页可查看闸门、缺证据数字、证据样例并触发重跑。

## 前端工作台（M8）

- 页面：投研对话 / 数据分析 / 数据（数据集 + 标的目录）/ 报告 / 审计（报告审计 + 运行记录）/ 交易 / 投顾 / 成本 / 决策 / 设置；
- 全链路：上传数据集 → 运行分析 → 图表 → 加入报告草稿 → HTML/Word/Excel → 审计与重跑；
- SSE：`Last-Event-ID` 断线重连，ChatView 在流中断后自动 `resume`（最多 3 次）并续接事件；
- 响应式：桌面左侧 dock 导航；移动端底部导航横向滚动，窄屏卡片/表格自动换行或横向滚动；
- 构建预算：首屏 JS/CSS gzip < 300KB（ECharts/lightweight-charts 按需异步 chunk）。

## 扩展与交付（M9）

- **容器化**：`Dockerfile` 多阶段构建（Node 20 构建前端 → Python 3.12 运行时），`docker compose up -d --build` 后访问 `http://127.0.0.1:8765`；数据卷 `./data:/app/data`，健康检查打 `/api/health`；
- **备份/恢复**：`python scripts/backup.py` 生成含 SQLite 一致性快照、catalog/datasets/reports 与 sha256 manifest 的 zip；`python scripts/restore.py <zip> <目标目录>` 默认写入新目录，校验路径穿越/哈希，`--force` 才覆盖；`--verify-only` 只验包；
- **许可证扫描**：`python scripts/check_licenses.py` 输出 allowed/review/exception/conflict；GPL 依赖（backtrader/ecos）进入显式例外清单，CI 以 conflict 为失败门禁；
- **性能基准**：`python scripts/bench_core.py` 用 5 万行数据集与 2 万目录记录测核心接口 P50/P95（默认 P95 上限 2000ms），结果落 JSON 便于答辩留档；
- **MCP 适配**：`python scripts/mcp_server.py` 以 stdio JSON-RPC 暴露只读工具（目录检索/数据集列表与画像/分析模块/SQL），不启动 LLM、不暴露密钥。

## 自由代码隔离沙箱（P3）

自由 Python 默认关闭（`sandbox.enabled: false`）。开启后：

- 每次运行由独立 `python -I backend/sandbox/worker.py` 子进程执行，不与 API 进程共享内存与密钥；
- 环境变量按白名单重建（不继承 `FINAGENT_*`）；Windows 用 Job Object 限制进程内存/进程数，POSIX 用 `resource.setrlimit` 限制内存/CPU/文件大小；
- 用户代码只能拿到 `datasets`（只读 DataFrame 字典）、`pd`/`np` 与 `save_output(filename, content)`，最终结论赋给 `result`；
- AST 仅作防御层：拒绝 `os/socket/subprocess/open/read_csv/to_csv/__class__` 与路径字符串；worker 内再用安全 builtins 与导入白名单兜底；
- 超时终止 worker，stdout/stderr/输出文件数/总量均有限额；每次运行的启停、退出码、耗时、峰值内存、代码哈希与输出清单落 `sandbox_runs` 表；
- API：`GET /api/sandbox/status`、`POST /api/sandbox/run`、`GET /api/sandbox/runs`、`GET /api/sandbox/runs/{id}`、`GET /api/sandbox/runs/{id}/files/{name}`；Agent 工具 `run_sandbox_code` 仅在用户消息含明确代码意图时放行；
- **残余风险**：Windows 进程级无法真正断网；如需强隔离，请把 worker 放入独立容器/虚拟机，或在部署层关闭该功能。

## 四条铁律

1. **数据必须来自工具**——模型凭记忆报数即幻觉（消融 C 组量化验证）；
2. **每个数字可溯源**——报告数字强制 `[n]` 编号引用，前端可点验，幻觉率自动度量；
3. **重大决策需辩论**——分析师 → 多/空研究员 → 风控经理 → 综合（TradingAgents 课程级复现）；
4. **决策有记忆有反思**——研判落库，T+N 自动结算生成 ≤80 字教训，注入后续分析。

## 五分钟复现

```bash
# 0) 前置：Python 3.12（>=3.12,<3.13）+ Node 20+
cd Oh-EcoFin-Agent

# 1) 后端依赖（版本锁死）
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt

# 2) 前端构建
cd frontend
npm ci --no-audit --no-fund
npm run build
cd ..

# 3) 密钥（三级任一：环境变量 > Windows 凭据管理器 > .env）
copy .env.example .env
# 在 .env 填入 FINAGENT_GLM_KEY / FINAGENT_DEEPSEEK_KEY（或用 keyring：
# 服务名 oh-ecofin-agent，字段 glm_key / deepseek_key / bocha_key）

# 4) 启动
.\.venv\Scripts\python.exe -m backend.app
# → 浏览器打开 http://127.0.0.1:8765
#   桌面壳：.\.venv\Scripts\python.exe desktop\main.py

# 5) 测试（CI 同款：零密钥零成本卡带回放）
.\.venv\Scripts\python.exe -m pytest tests/ -q

# 6) 端到端联调验证（真实启动服务：静态托管/会话/SSE 协议/成本看板）
.\.venv\Scripts\python.exe scripts\smoke_e2e.py
```

**答辩离线兜底（DEMO_MODE）**：提前一天 `python scripts/freeze_demo.py` 预抓冻结卡带，
现场 `DEMO_MODE=1` 启动即断网可跑。

## 局域网联动（手机演示）

1. 桌面启动时 `FINAGENT_APP__LAN_MODE=true`（或 config.yaml `app.lan_mode: true`）；
2. 启动日志打印随机访问令牌，手机浏览器访问 `http://<LAN-IP>:8765/?t=<token>`；
3. 移动 APK 构建见 `mobile/README.md`（受阻即走扫码降级预案，口径已对齐）。

## 成本看板与熔断

- 每次调用落库 `llm_calls` 表；`GET /api/cost/summary` 汇总；
- 日成本超 `llm.daily_cost_cap_cny`（默认 ¥20）自动熔断拒绝新调用；
- 全周期导出：`python scripts/export_cost.py` → `docs/cost_report/`。

## 仓库结构

```
backend/    FastAPI + 引擎(gateway/react/debate/askuser/compactor) + 工具 + 数据 + 量化 + 记忆 + 安全
            catalog/(标的目录) + datasets/(不可变快照) + query/(DuckDB 只读 SQL Guard)
            analysis/(7 个确定性分析模块 + 可序列化 ECharts chart spec)
            reports/(report spec + HTML/Word/Excel 导出 + 图表 PNG)
            engine/(ReAct + run_state 状态机 + context 预算 + runs/tool_calls 记录)
            audit/(证据账本 + 数字比对 + G1-G5 质量闸门 + 重跑对比)
            sandbox/(P3 独立 worker + AST 校验 + Job Object/rlimit 限额)
            ops/(备份/恢复) + mcp/(只读 MCP 适配)
frontend/   Vite + Vue3 + lightweight-charts + ECharts（按需注册，首屏 <300KB gzip）
desktop/    pywebview 壳 + PyInstaller onedir spec
mobile/     Capacitor 工程 + 明文 HTTP 白名单模板
eval/       tasks.json(20 标准任务) + run_ablation.py(A–E 消融) + metrics.py
tests/      单测 + 卡带回放集成（CI 零密钥）
scripts/    freeze_demo / bench_cold / bench_e2e / export_cost / 未来函数扫描
docs/       开发方案 + 报告草稿 + 成本清单导出
```

## 关键红线（验收口径）

| 红线 | 目标 | 测量 |
|---|---|---|
| 桌面冷启动 | <2s | `scripts/bench_cold.py` ×10 中位 |
| 快照命中工具响应 | <50ms | 埋点 ×100 中位 |
| K 线 500 根渲染 | <100ms | 前端 performance.mark |
| 移动 bundle | <300KB gzip | CI 构建断言 |
| 3 股对比端到端 | <4s | `scripts/bench_e2e.py` ×10 中位 |
| 沙箱违例参数 | 100% 拒绝 | 50 用例单测 |
| 未来函数扫描 | 零报警 | CI 强制 |

## 免责声明（三层）

页面 footer 常驻 + 每份报告 `risk_disclosure` 字段 + API 响应体附带：
本报告由教学演示智能体生成，仅供课程学习使用，不构成任何投资建议；数据可能存在延迟或误差，请以交易所官方信息为准。

## License

见 [LICENSE](LICENSE)。
