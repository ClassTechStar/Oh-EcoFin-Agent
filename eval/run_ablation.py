"""消融矩阵一键执行（14.1）：5 组 × 20 任务 × 3 重复（seed 固定）→ Markdown 结果表。

| 组 | 配置 | 验证命题 |
|---|---|---|
| A 基线 | GLM + 工具 + ReAct + 压缩 + 辩论 | 全家桶参照系 |
| B 换 DeepSeek | 主力换 deepseek-flash | 双模型横向对比 |
| C 无工具直出 | 移除全部工具 | 数字幻觉率（招牌结论） |
| D 单轮无辩论 | 一次调用直出 | 多轮与辩论的提升 |
| E 无压缩器 | 工具结果原文进上下文 | 压缩对准确率/成本影响 |

执行纪律：报告均值±标准差；C 组另记录"编造数据"定性样例。
真实冒烟 = 本地手动脚本 + 答辩前全量跑一次，不进 CI（CI 用卡带回放）。

用法：
  python eval/run_ablation.py --configs A,B,C --repeat 3 --out eval/results/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app import AppContext, _extract_symbol
from backend.config import Settings
from backend.engine.events import SessionStream
from backend.engine.react import ChatTask
from eval.metrics import TaskResult, aggregate, numeric_support_rate, render_markdown, tool_recall

TASKS_FILE = Path(__file__).resolve().parent / "tasks.json"


# ---------- 配置切换器（消融开关） ----------

class AblationConfig:
    def __init__(self, name: str, ctx: AppContext):
        self.name = name
        self.ctx = ctx

    @property
    def use_tools(self) -> bool:
        return self.name != "C"

    @property
    def model_tier(self) -> str:
        return "economy" if self.name == "B" else "primary"

    @property
    def use_debate(self) -> bool:
        return self.name in ("A", "B")

    @property
    def use_compactor(self) -> bool:
        return self.name != "E"

    def schemas(self):
        reg = self.ctx.registry
        if not self.use_tools:
            return []
        return reg.schemas()


async def run_task(cfg: AblationConfig, task: dict, repeat: int,
                   stream: SessionStream) -> TaskResult:
    result = TaskResult(task_id=task["id"], group=task.get("group_id", ""),
                        config=cfg.name, repeat=repeat)
    prompt = task["prompt"]
    mode = {"research": "debate" if cfg.use_debate else "chat",
            "advisor": "advisor", "quant": "chat"}[task["group_id"]]
    called: list[str] = []

    # 捕获工具调用与原始返回（溯源比对语料）
    orig_execute = cfg.ctx.registry.execute
    raw_results: list[dict] = []

    async def spy_execute(name, raw_args, **kw):
        called.append(name)
        res = await orig_execute(name, raw_args, **kw)
        raw_results.append({"tool": name, "args": raw_args, "result": res})
        return res

    cfg.ctx.registry.execute = spy_execute  # type: ignore

    # 投顾测评自动作答（挂起 Future 注入 profile 答案）
    auto_answers = list(task.get("profile_answers", []))
    if auto_answers:
        orig_wait = cfg.ctx.ask_manager.wait

        async def auto_wait(pa):
            if auto_answers:
                a = auto_answers.pop(0)
                return {"qid": pa.qid, "choice": a["choice"], "timeout": False}
            return await orig_wait(pa)

        cfg.ctx.ask_manager.wait = auto_wait  # type: ignore

    try:
        if not cfg.use_tools:
            # C 组：无工具直出（一次调用）
            text = await cfg.ctx.gateway.simple(
                [{"role": "user", "content": prompt}], cfg.model_tier, role="ablation")
            raw_results = []
            report_text = text
        else:
            # 投研辩论任务走 DebateOrchestrator，其余走 ReAct
            symbol = _extract_symbol(prompt) or ""
            if mode == "debate" and cfg.use_debate and symbol:
                out = await cfg.ctx.debate.run(
                    f"ablation-{cfg.name}", [symbol], prompt, stream)
                report_text = " ".join(
                    s.get("text", "") for s in out["report"].get("sections", []))
                for r in out.get("raw_tool_results", []):
                    called.append(r["tool"])
                    raw_results.append(r)
            else:
                t = ChatTask(session_id=f"ablation-{cfg.name}", user_message=prompt,
                             history=[], mode=mode)
                done = await cfg.ctx.react.run(t, stream)
                report = done.get("final_report") or {}
                if report:
                    report_text = " ".join(
                        s.get("text", "") for s in report.get("sections", []))
                else:
                    report_text = done.get("final_text", "")
    finally:
        cfg.ctx.registry.execute = orig_execute  # type: ignore
        if auto_answers:
            cfg.ctx.ask_manager.wait = orig_wait  # type: ignore

    # 指标
    result.tool_recall, result.tool_missing, result.tool_extra = tool_recall(
        called, task.get("expected_tools", []))
    rate, sup, total = numeric_support_rate(report_text, raw_results)
    result.numeric_support_rate = rate
    if cfg.name == "C" and rate < 0.6:
        result.fabricated_sample = report_text[:400]
    result.rubric_score = await llm_judge(prompt, report_text, task.get("rubric", []),
                                          cfg.ctx.gateway)
    result.rubric_pass = result.rubric_score >= 0.6
    return result


JUDGE_PROMPT = """你是评分器。对以下回答按 rubric 逐条打分，输出 JSON：
{{"score": 0到1之间的小数, "pass": true/false}}

任务：{prompt}
Rubric：{rubric}
回答：
{answer}

评分标准：全部满足=1.0；满足大半≥0.6；编造数据/缺关键要素<0.6。"""


async def llm_judge(prompt: str, answer: str, rubric: list, gateway) -> float:
    if gateway is None or not answer:
        return 0.0
    text = await gateway.simple(
        [{"role": "user", "content": JUDGE_PROMPT.format(
            prompt=prompt[:500], rubric=json.dumps(rubric, ensure_ascii=False),
            answer=answer[:3000])}],
        "economy", role="judge", max_tokens=120)
    try:
        import re

        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            data = json.loads(m.group(0))
            return float(data.get("score", 0.0))
    except (json.JSONDecodeError, ValueError, KeyError):
        pass
    return 0.0


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--configs", default="A,B,C,D,E")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default="eval/results")
    args = parser.parse_args()

    random.seed(args.seed)
    settings = Settings()
    ctx = AppContext(settings)
    ctx.wire()

    tasks_spec = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    all_tasks = []
    for g in tasks_spec["groups"]:
        for t in g["tasks"]:
            t["group_id"] = g["id"]
            all_tasks.append(t)

    results: list[TaskResult] = []
    for name in args.configs.split(","):
        cfg = AblationConfig(name.strip(), ctx)
        for rep in range(args.repeat):
            for task in all_tasks:
                t0 = time.perf_counter()
                stream = SessionStream(f"abl-{cfg.name}-{rep}")
                try:
                    r = await run_task(cfg, task, rep, stream)
                    results.append(r)
                    print(f"[{cfg.name}#{rep}] {task['id']} ok={r.rubric_pass} "
                          f"数字命中={r.numeric_support_rate:.2f} "
                          f"({time.perf_counter() - t0:.1f}s)")
                except Exception as e:
                    print(f"[{cfg.name}#{rep}] {task['id']} 异常: {e}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    table = aggregate(results)
    md = render_markdown(table)
    (out_dir / f"ablation_{time.strftime('%Y%m%d_%H%M%S')}.md").write_text(
        "# 消融矩阵结果\n\n" + md + "\n", encoding="utf-8")
    with open(out_dir / "ablation_results.jsonl", "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r.__dict__, ensure_ascii=False, default=str) + "\n")
    print("\n" + md)
    print(f"\n结果已写入 {out_dir}")


if __name__ == "__main__":
    asyncio.run(main())
