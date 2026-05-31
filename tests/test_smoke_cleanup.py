from finance_agent.storage.smoke_cleanup import DEFAULT_CLEANUP_SPECS, build_where_clause


def _spec(table_name: str):
    return next(spec for spec in DEFAULT_CLEANUP_SPECS if spec.table == table_name)


def test_assets_cleanup_does_not_match_payload_source_only() -> None:
    """资产主表只按测试资产标识清理，避免误删真实股票主数据。"""

    where_clause = build_where_clause(
        _spec("assets"),
        existing_columns={"asset_id", "symbol", "name", "payload"},
    )

    assert "asset_id" in where_clause
    assert "payload" not in where_clause


def test_chat_message_cleanup_avoids_free_text_content() -> None:
    """聊天正文提到 smoke 时不能被当成测试数据删除。"""

    where_clause = build_where_clause(
        _spec("assistant_chat_messages"),
        existing_columns={"chat_message_id", "chat_session_id", "owner_id", "content", "payload"},
    )

    assert "chat_message_id" in where_clause
    assert "content" not in where_clause


def test_cleanup_order_removes_children_before_parents() -> None:
    """有引用关系的测试记录先删子表，最后再删资产主数据。"""

    table_order = [spec.table for spec in DEFAULT_CLEANUP_SPECS]

    assert table_order.index("asset_recommendations") < table_order.index("recommendation_runs")
    assert table_order.index("asset_universe_members") < table_order.index("asset_universes")
    assert table_order[-1] == "assets"


def test_model_config_cleanup_includes_provider_and_model_keys() -> None:
    """模型配置测试数据按 provider/model key 精准清理。"""

    provider_where = build_where_clause(
        _spec("model_providers"),
        existing_columns={"provider_id", "provider_key", "provider_name", "payload"},
    )
    model_where = build_where_clause(
        _spec("model_instances"),
        existing_columns={"model_instance_id", "provider_key", "model_key", "model_name", "payload"},
    )

    assert "provider_key" in provider_where
    assert "model_key" in model_where
