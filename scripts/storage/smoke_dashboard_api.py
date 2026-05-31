"""Dashboard FastAPI 冒烟验证。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def main() -> None:
    """验证 Dashboard API 基础路由可用。"""

    from fastapi.testclient import TestClient

    from finance_agent.api.app import create_app

    app = create_app()
    client = TestClient(app)

    health = client.get("/api/health")
    assert health.status_code == 200, health.text
    health_payload = health.json()
    assert health_payload["status"] in {"ok", "partial", "unavailable"}
    assert "api" in health_payload

    summary = client.get("/api/dashboard/summary", params={"owner_id": "demo-owner"})
    assert summary.status_code == 200, summary.text
    summary_payload = summary.json()
    assert summary_payload["owner_id"] == "demo-owner"
    assert summary_payload["status"] in {"ok", "empty", "partial", "unavailable"}
    assert "sections" in summary_payload

    workflows = client.get("/api/workflows")
    assert workflows.status_code == 200, workflows.text
    workflow_payload = workflows.json()
    assert workflow_payload["status"] == "ok"
    assert workflow_payload["data"]["workflows"]

    provider = client.put(
        "/api/models/providers/smoke-web-provider",
        json={
            "provider_vendor": "openai_compatible",
            "provider_name": "Smoke Web Provider",
            "base_url": "https://example.invalid/v1",
            "api_key": "smoke-secret",
            "timeout_seconds": 12,
            "is_enabled": True,
        },
    )
    assert provider.status_code == 200, provider.text
    provider_payload = provider.json()
    assert provider_payload["status"] == "ok"
    assert provider_payload["data"]["provider_key"] == "smoke-web-provider"
    assert provider_payload["data"]["api_key"] != "smoke-secret"
    masked_api_key = provider_payload["data"]["api_key"]

    provider_without_key = client.put(
        "/api/models/providers/smoke-web-provider",
        json={
            "provider_vendor": "openai_compatible",
            "provider_name": "Smoke Web Provider",
            "base_url": "https://example.invalid/v1",
            "timeout_seconds": 12,
            "is_enabled": True,
        },
    )
    assert provider_without_key.status_code == 200, provider_without_key.text
    provider_without_key_payload = provider_without_key.json()
    assert provider_without_key_payload["status"] == "ok"
    assert provider_without_key_payload["data"]["api_key"] == masked_api_key

    model = client.put(
        "/api/models/instances/smoke-web-primary",
        json={
            "provider_key": "smoke-web-provider",
            "model_name": "Smoke Web Primary",
            "role": "primary_financial_analyst",
            "route_priority": 120,
            "is_enabled": True,
        },
    )
    assert model.status_code == 200, model.text
    model_payload = model.json()
    assert model_payload["status"] == "ok"
    assert model_payload["data"]["model_key"] == "smoke-web-primary"

    route = client.put(
        "/api/models/routes/primary_financial_analyst",
        json={
            "workflow_type": "*",
            "task": "*",
            "model_key": "smoke-web-primary",
            "reason": "Web 控制台切换主分析 Agent 模型。",
            "priority": 200,
            "is_enabled": True,
        },
    )
    assert route.status_code == 200, route.text
    route_payload = route.json()
    assert route_payload["status"] == "ok"
    assert route_payload["data"]["model_key"] == "smoke-web-primary"

    preview = client.get(
        "/api/models/routes/preview",
        params={
            "workflow_type": "portfolio_monitoring",
            "task": "agent_loop_planning",
        },
    )
    assert preview.status_code == 200, preview.text
    preview_payload = preview.json()
    assert preview_payload["status"] == "ok"
    routes = preview_payload["data"]["routes"]
    assert routes[0]["model_key"] == "smoke-web-primary", routes

    sync_config = client.get("/api/data/sync/config")
    assert sync_config.status_code == 200, sync_config.text
    sync_config_payload = sync_config.json()
    assert sync_config_payload["status"] == "ok"
    assert sync_config_payload["data"]["validation"]["valid"] is True
    assert sync_config_payload["data"]["preview"]["tasks"]

    print("Dashboard API smoke passed")


if __name__ == "__main__":
    main()
