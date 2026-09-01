"""调度配置身份。

配置身份只描述 scheduler 实际成功加载的文件，不负责解析业务调度配置。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SchedulerConfigIdentity:
    """可供状态文件和 API 比对的不可变配置身份。"""

    path: str
    digest: str
    loaded_at: datetime
    mtime_ns: int
    version: str

    def to_status_dict(self) -> dict[str, Any]:
        """转换为 scheduler 状态文件字段。"""

        return {
            "config_path": self.path,
            "config_digest": self.digest,
            "config_loaded_at": self.loaded_at.isoformat(),
            "config_mtime_ns": self.mtime_ns,
            "config_version": self.version,
        }


def load_config_identity(path: str | Path) -> SchedulerConfigIdentity:
    """读取文件并基于原始字节生成 SHA-256 身份。"""

    config_path = Path(path).expanduser().resolve(strict=True)
    content = config_path.read_bytes()
    payload = json.loads(content.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("调度配置根节点必须是 JSON 对象")
    digest = hashlib.sha256(content).hexdigest()
    version = str(
        payload.get("schema_version")
        or payload.get("config_version")
        or payload.get("version")
        or digest[:12]
    )
    return SchedulerConfigIdentity(
        path=str(config_path),
        digest=digest,
        loaded_at=datetime.now(tz=UTC),
        mtime_ns=config_path.stat().st_mtime_ns,
        version=version,
    )
