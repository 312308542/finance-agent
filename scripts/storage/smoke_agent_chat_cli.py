"""验证 CLI 聊天窗口可以和内部金融 Agent 对话。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    """执行聊天 CLI 冒烟验证。"""

    config = {
        "models": {
            "deepseek-v4-pro": {
                "provider": "deepseek",
                "model_name": "DeepSeek V4 Pro",
                "base_url": "https://api.deepseek.example/v1",
                "api_key": "deepseek-smoke-key",
                "role": "primary_financial_analyst",
                "enabled": True,
            },
            "gpt-5.5-pro": {
                "provider": "openai",
                "model_name": "GPT-5.5 Pro",
                "base_url": "https://api.openai.example/v1",
                "api_key": "openai-smoke-key",
                "role": "high_risk_reviewer",
                "enabled": True,
            },
        }
    }
    with tempfile.TemporaryDirectory() as temp_dir:
        config_file = Path(temp_dir) / "models.json"
        config_file.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "finance_agent.cli",
                "chat",
                "--owner-id",
                "owner:smoke:chat",
                "--config-file",
                str(config_file),
                "--message",
                "有哪些工作流可以用？",
                "--message",
                "看一下模型配置",
                "--message",
                "/exit",
            ],
            check=True,
            capture_output=True,
            encoding="utf-8",
            text=True,
        )
    payload = json.loads(process.stdout)
    if payload["status"] != "ok":
        raise AssertionError(f"聊天 CLI 必须返回 ok：{payload}")
    turns = payload["data"]["turns"]
    if len(turns) != 3:
        raise AssertionError(f"三条输入应形成三轮对话，实际={len(turns)}")

    workflow_answer = turns[0]["assistant_message"]["content"]
    if "recommendation_decision" not in workflow_answer:
        raise AssertionError("聊天窗口必须能调用接口层列出 Workflow。")
    model_answer = turns[1]["assistant_message"]["content"]
    if "deepseek-v4-pro" not in model_answer or "gpt-5.5-pro" not in model_answer:
        raise AssertionError("聊天窗口必须能读取模型配置。")
    if "deepseek-smoke-key" in json.dumps(payload, ensure_ascii=False):
        raise AssertionError("聊天窗口输出不能泄露模型 API key。")
    if turns[-1]["assistant_message"]["intent"] != "exit":
        raise AssertionError("聊天窗口必须识别退出意图。")

    print(
        {
            "turn_count": len(turns),
            "first_intent": turns[0]["assistant_message"]["intent"],
            "second_intent": turns[1]["assistant_message"]["intent"],
            "exit_intent": turns[-1]["assistant_message"]["intent"],
        }
    )


if __name__ == "__main__":
    main()
