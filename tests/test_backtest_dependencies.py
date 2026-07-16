from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_backtest_dependencies_declared_and_importable() -> None:
    """09-T1：回测依赖应在项目依赖中声明，并能在当前 venv 中导入。"""

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]
    normalized_dependencies = {dependency.lower().split(">=")[0] for dependency in dependencies}

    assert "bt" in normalized_dependencies
    assert "quantstats" in normalized_dependencies
    assert importlib.util.find_spec("bt") is not None
    assert importlib.util.find_spec("quantstats") is not None
