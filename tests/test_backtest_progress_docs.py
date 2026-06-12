from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_doc(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_backtest_docs_reflect_09_completion() -> None:
    """09 方案收尾后，README 和进度文档应同步轻量回测证据链状态。"""

    readme = read_doc("README.md")
    project_progress = read_doc("docs/项目进度跟踪表.md")
    optimization_progress = read_doc("docs/优化版本进度跟踪表.md")
    overall_plan = read_doc("docs/开发方案/00-总体规划与执行约定.md")
    backtest_plan = read_doc("docs/开发方案/09-轻量回测与绩效验证.md")

    assert "轻量回测已接入推荐证据链和中文报告" in readme
    assert "`finance-agent backtest run` 和 `analytics.backtest.weekly`" in readme
    assert "真实 A 股主板数据冒烟按跳过策略待联调" in readme

    assert "| 量化模型研究 | 轻量回测证据链已接入，完整实验室后置 |" in project_progress
    assert "推荐 payload/run payload/中文报告中的 `backtest_evidence`" in project_progress

    assert "| O6 | 因子和评分增强 | 推荐链路质量收尾已完成，回测证据链已接入 |" in optimization_progress
    assert "真实 A 股主板数据冒烟按跳过策略待联调" in optimization_progress

    assert "| 09 轻量回测与绩效验证 | 已完成，T7 待联调 |" in overall_plan
    assert "| T8 文档同步 | 已完成 |" in backtest_plan
