"""内置分析模块（M4）：确定性统计 + 可复现图表规格。"""

from backend.analysis.models import AnalysisError, AnalysisRunRequest
from backend.analysis.service import AnalysisService

__all__ = ["AnalysisError", "AnalysisRunRequest", "AnalysisService"]
