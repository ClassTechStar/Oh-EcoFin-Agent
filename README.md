# 哦经金智能体 · Oh EcoFin Agent

> **教学演示用途：所有输出不构成任何投资建议，不提供任何形式的投资建议服务。**
> 全栈国产化 A 股投研智能体：国产模型（GLM-5.3-Flash / DeepSeek）+ 国产数据源（AKShare/东财）+ A 股 + 国产新闻源。
> 平台：Windows 桌面端（pywebview）+ Android（Capacitor）+ 浏览器，**一套前端三端可达**。

## 三大功能

| 功能 | 内容 |
|---|---|
| **智能投研** | 单股四维分析 / 2–5 股对比（相关性矩阵）/ 新闻情绪汇总 / **多空辩论式综合研判** / 决策记忆与复盘 |
| **智能投顾** | 对话式风险测评（5 题 ask_user 挂起）→ Markowitz 优化（Ledoit-Wolf）→ 确定性合规审查链 → 有效前沿图 |
| **量化分析** | Backtrader 双均线回测（A 股四规则完整建模）+ Walk-forward 滚动验证 + 多因子 IC 验证 |

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
