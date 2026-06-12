from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_doc(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_data_layer_progress_docs_reflect_06_completion() -> None:
    """06 方案收尾后，关键进度文档应同步数据层尾项状态。"""

    fund_plan = read_doc("docs/基金行情同步任务方案.md")
    project_progress = read_doc("docs/项目进度跟踪表.md")
    optimization_progress = read_doc("docs/优化版本进度跟踪表.md")

    assert "| FUND-013 | 小批量 LOF 10 年日 K 验证 | 已完成 |" in fund_plan
    assert "| FUND-015 | 全量基金初始化执行 | 进行中 |" in fund_plan
    assert "ETF 1507、LOF 390、开放式基金 20604" in fund_plan

    assert "| A 股数据 | 06 数据层尾项已完成 |" in project_progress
    assert "北向资金、限售解禁和股权质押已接入" in project_progress
    assert "部署模板和运维手册已补齐" in project_progress

    assert "| O5 | 数据层生产化 | 已完成 |" in optimization_progress
    assert "LOF 备源、北向资金、限售解禁、股权质押、Windows/Docker 部署模板和运维手册均已完成" in optimization_progress
