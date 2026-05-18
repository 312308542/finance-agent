"""验证 CLI 聊天窗口具备可恢复的聊天记忆。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from finance_agent.storage.db import create_session_factory, session_scope
from finance_agent.storage.repositories import ChatMemoryRepository

OWNER_ID = "owner:smoke:chat-memory"


def run_chat(*args: str) -> dict:
    """运行一次聊天 CLI 并返回 JSON。"""

    process = subprocess.run(
        [sys.executable, "-m", "finance_agent.cli", "chat", *args],
        check=True,
        capture_output=True,
        encoding="utf-8",
        text=True,
    )
    return json.loads(process.stdout)


def main() -> None:
    """执行聊天记忆冒烟验证。"""

    config = {
        "models": {
            "deepseek-v4-pro": {
                "provider": "deepseek",
                "model_name": "DeepSeek V4 Pro",
                "base_url": "https://api.deepseek.example/v1",
                "api_key": "deepseek-smoke-key",
                "role": "primary_financial_analyst",
                "enabled": True,
            }
        }
    }
    with tempfile.TemporaryDirectory() as temp_dir:
        config_file = Path(temp_dir) / "models.json"
        config_file.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

        first = run_chat(
            "--owner-id",
            OWNER_ID,
            "--config-file",
            str(config_file),
            "--new-session",
            "--message",
            "有哪些工作流可以用？",
            "--message",
            "看一下模型配置",
        )
        if first["status"] != "ok":
            raise AssertionError(f"首次聊天必须成功：{first}")
        session_id = first["data"].get("chat_session_id")
        if not session_id:
            raise AssertionError("聊天结果必须返回 chat_session_id。")

        second = run_chat(
            "--owner-id",
            OWNER_ID,
            "--config-file",
            str(config_file),
            "--session-id",
            session_id,
            "--message",
            "查看历史",
        )
        if second["status"] != "ok":
            raise AssertionError(f"续聊必须成功：{second}")
        history_answer = second["data"]["turns"][0]["assistant_message"]["content"]
        if "有哪些工作流可以用" not in history_answer or "看一下模型配置" not in history_answer:
            raise AssertionError(f"续聊必须能读取上一轮历史：{history_answer}")

    session_factory = create_session_factory()
    with session_scope(session_factory) as session:
        repository = ChatMemoryRepository(session)
        messages = repository.list_messages(
            owner_id=OWNER_ID,
            chat_session_id=session_id,
            limit=20,
        )
        if len(messages) < 6:
            raise AssertionError(f"两次脚本化聊天至少应落库 6 条消息，实际={len(messages)}")
        stored_session = repository.get_session(owner_id=OWNER_ID, chat_session_id=session_id)
        if stored_session is None:
            raise AssertionError("聊天会话必须可从数据库恢复。")
        if stored_session.message_count < 6:
            raise AssertionError(
                f"会话 message_count 必须更新，实际={stored_session.message_count}"
            )

    print(
        {
            "chat_session_id": session_id,
            "stored_message_count": stored_session.message_count,
            "history_intent": second["data"]["turns"][0]["assistant_message"]["intent"],
        }
    )


if __name__ == "__main__":
    main()
