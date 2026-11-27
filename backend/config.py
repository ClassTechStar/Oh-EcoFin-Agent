"""统一配置：环境变量 > config.yaml > 代码默认值（pydantic-settings）。

关键项：模型名/tier 路由、快照间隔、限速、成本日上限、SSE 心跳、缓存 TTL、DEMO_MODE。
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config.yaml"


class LLMConfig(BaseSettings):
    primary_model: str = "glm-5.3-flash"
    economy_model: str = "deepseek-flash"
    primary_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    economy_base_url: str = "https://api.deepseek.com/v1"
    timeout_seconds: int = 60
    max_rounds: int = 12
    debate_rounds: int = 2
    daily_cost_cap_cny: float = 20.0


class DataConfig(BaseSettings):
    snapshot_interval_intraday: int = 60
    snapshot_interval_idle: int = 1800
    rate_limit_rps: float = 1.0
    rate_limit_burst: int = 3
    kline_cache_ttl: int = 3600
    news_cache_ttl: int = 1800
    notice_cache_ttl: int = 600


class SearchConfig(BaseSettings):
    enabled_sources: list[str] = Field(default_factory=lambda: ["bocha", "zhipu", "deepseek"])
    circuit_failure_threshold: int = 3
    circuit_cooldown_seconds: int = 300
    news_max_hours: int = 24
    fallback_hours: int = 72


class AdvisorConfig(BaseSettings):
    risk_levels: list[str] = Field(default_factory=lambda: ["R1", "R2", "R3", "R4", "R5"])
    equity_caps: dict[str, float] = Field(
        default_factory=lambda: {"R1": 0.0, "R2": 0.10, "R3": 0.35, "R4": 0.45, "R5": 0.50}
    )
    single_caps: dict[str, float] = Field(
        default_factory=lambda: {"R1": 0.0, "R2": 0.10, "R3": 0.20, "R4": 0.25, "R5": 0.30}
    )


class ServerConfig(BaseSettings):
    host: str = "127.0.0.1"
    port: int = 8765
    sse_heartbeat_seconds: int = 15
    replay_buffer_size: int = 500
    ask_user_timeout_seconds: int = 600


class MemoryConfig(BaseSettings):
    summarize_trigger_tokens: int = 20000
    summarize_trigger_rounds: int = 20
    keep_recent_rounds: int = 6


class CatalogConfig(BaseSettings):
    """标的目录（M1）：版本化快照落盘 + 检索上限。"""

    dir: str = str(PROJECT_ROOT / "data" / "catalog")
    import_batch_size: int = 1000
    search_default_limit: int = 50
    search_max_limit: int = 200


class DatasetConfig(BaseSettings):
    """数据集导入与查询（M2/M3）：不可变 Parquet 快照 + DuckDB 只读查询。"""

    dir: str = str(PROJECT_ROOT / "data" / "datasets")
    workspace_dir: str = str(PROJECT_ROOT / "data" / "workspaces")
    max_file_mb: int = 200
    max_rows: int = 2_000_000
    max_columns: int = 512
    query_max_rows: int = 100_000
    query_timeout_seconds: int = 30
    profile_sample_rows: int = 5


class AnalysisConfig(BaseSettings):
    """分析模块（M4）：超时与展示上限。"""

    timeout_seconds: int = 30
    max_describe_columns: int = 50
    max_correlation_columns: int = 12
    max_trend_points: int = 500
    max_groups: int = 10


class ReportsConfig(BaseSettings):
    """报告与导出（M5）：落盘目录、块数与表格展示上限。"""

    dir: str = str(PROJECT_ROOT / "data" / "reports")
    max_blocks: int = 30
    max_table_rows: int = 200
    chart_dpi: int = 150


class AgentConfig(BaseSettings):
    """Agent 编排（M6）：上下文预算与工具结果预算（轮次沿用 llm.max_rounds）。"""

    context_token_budget: int = 32000
    keep_recent_messages: int = 12
    tool_result_token_budget: int = 2000
    total_tool_token_budget: int = 12000


class AuditConfig(BaseSettings):
    """可信审计（M7）：证据规模与数值匹配容差。"""

    max_evidence: int = 10000
    max_claims: int = 20000
    tolerance: float = 1e-6


class SandboxConfig(BaseSettings):
    """自由代码隔离 worker（P3）：默认关闭，显式开启才注册 API/工具。"""

    enabled: bool = False
    dir: str = str(PROJECT_ROOT / "data" / "sandbox")
    timeout_seconds: int = 10
    memory_mb: int = 1024
    max_output_mb: int = 10
    max_files: int = 20
    max_code_chars: int = 20000


class AppConfig(BaseSettings):
    demo_mode: bool = False
    lan_mode: bool = False


class ExecutionConfig(BaseSettings):
    """实盘券商通道配置（默认关闭；开启也必须显式注入适配器与账户）。"""

    live_enabled: bool = False
    broker_kind: str = "qmt"          # qmt | http
    account_id: str = ""
    qmt_path: str = ""                # miniQMT userdata_mini 路径
    qmt_session_id: int = 0
    http_base_url: str = ""           # 券商 OpenAPI 网关
    http_token: str = ""
    timeout_seconds: float = 10.0


class Settings(BaseSettings):
    """根配置对象。优先级：config.yaml（显式实参）> 环境变量 > .env > 默认值。"""

    model_config = SettingsConfigDict(
        env_prefix="FINAGENT_",
        env_nested_delimiter="__",
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm: LLMConfig = Field(default_factory=LLMConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    advisor: AdvisorConfig = Field(default_factory=AdvisorConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    app: AppConfig = Field(default_factory=AppConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)
    datasets: DatasetConfig = Field(default_factory=DatasetConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    reports: ReportsConfig = Field(default_factory=ReportsConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)

    db_path: str = str(PROJECT_ROOT / "data" / "finagent.db")
    cassette_dir: str = str(PROJECT_ROOT / "data" / "cassettes_frozen")
    log_level: str = "INFO"


def _deep_update(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


def load_settings() -> Settings:
    """按优先级合并：环境变量 > config.yaml > 默认值。"""
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            data = {}
    # DEMO_MODE 环境变量快捷通道（答辩日 DEMO_MODE=1）
    if os.environ.get("DEMO_MODE", "").strip() in {"1", "true", "True"}:
        data.setdefault("app", {})["demo_mode"] = True
    merged = Settings(**data)
    # 环境变量优先：pydantic-settings 已处理 FINAGENT_ 前缀，
    # 这里仅覆盖根级标量（嵌套字段由 init_kwargs 传入时已具最高优先级）。
    return merged


def get_settings() -> Settings:
    """进程级单例入口（测试可自行构造 Settings 覆盖）。"""
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = load_settings()
    return _SETTINGS


_SETTINGS: Settings | None = None
