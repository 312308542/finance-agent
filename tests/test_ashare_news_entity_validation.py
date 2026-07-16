from finance_agent.data.news_entity_validation import validate_ashare_news_entity


def test_rejects_conflicting_exchange_index_entity() -> None:
    """深市股票遇到同代码沪市指数时必须拒绝。"""

    decision = validate_ashare_news_entity(
        symbol="000685",
        asset_name="中山公用",
        title="科创芯片 ETF 跟踪上证科创板芯片指数 000685.SH 上涨",
        summary=None,
    )

    assert decision.status == "failed"
    assert decision.reason == "conflicting_exchange_suffix"
    assert decision.matched_by is None


def test_accepts_canonical_company_name() -> None:
    """标题出现规范公司名时应通过实体门控。"""

    decision = validate_ashare_news_entity(
        symbol="000685",
        asset_name="中山公用",
        title="中山公用发布经营数据",
        summary=None,
    )

    assert decision.status == "passed"
    assert decision.reason == "canonical_name"
    assert decision.matched_by == "canonical_name"


def test_accepts_matching_exchange_qualified_symbol() -> None:
    """交易所后缀与目标股票一致时可作为强实体证据。"""

    decision = validate_ashare_news_entity(
        symbol="000685",
        asset_name="中山公用",
        title="000685.SZ 发布最新公告",
        summary=None,
    )

    assert decision.status == "passed"
    assert decision.reason == "matching_exchange_suffix"
    assert decision.matched_by == "matching_exchange_suffix"


def test_code_without_exchange_or_name_is_ambiguous() -> None:
    """只有裸六位代码时无法排除指数同号，必须按歧义过滤。"""

    decision = validate_ashare_news_entity(
        symbol="000685",
        asset_name="中山公用",
        title="000685 今日上涨",
        summary=None,
    )

    assert decision.status == "ambiguous"
    assert decision.reason == "bare_symbol_only"


def test_unrelated_text_without_target_entity_is_ambiguous() -> None:
    """没有任何目标实体证据的搜索命中不得绑定给请求股票。"""

    decision = validate_ashare_news_entity(
        symbol="000685",
        asset_name="中山公用",
        title="芯片板块午后走强",
        summary="多只半导体股票上涨",
    )

    assert decision.status == "ambiguous"
    assert decision.reason == "target_entity_not_proven"


def test_explicit_index_context_without_company_name_is_failed() -> None:
    """裸代码被明确描述为指数时应拒绝而不是仅标记歧义。"""

    decision = validate_ashare_news_entity(
        symbol="000685",
        asset_name="中山公用",
        title="上证科创板芯片指数 000685 今日上涨",
        summary=None,
    )

    assert decision.status == "failed"
    assert decision.reason == "non_target_security_context"


def test_accepts_core_name_derived_from_legal_company_suffix() -> None:
    """规范全称的法律形态后缀可移除为可追溯核心简称。"""

    decision = validate_ashare_news_entity(
        symbol="000001",
        asset_name="平安银行股份有限公司",
        title="平安银行发布一季报",
        summary=None,
    )

    assert decision.status == "passed"
    assert decision.reason == "company_alias"
    assert decision.matched_by == "company_alias"


def test_empty_title_and_summary_are_failed() -> None:
    """标题和摘要均为空时没有形成事件的基本条件。"""

    decision = validate_ashare_news_entity(
        symbol="000685",
        asset_name="中山公用",
        title=None,
        summary=None,
    )

    assert decision.status == "failed"
    assert decision.reason == "empty_text"
