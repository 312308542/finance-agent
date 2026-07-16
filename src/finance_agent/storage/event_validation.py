"""事件与证据活动查询的实体校验门控。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_
from sqlalchemy.sql.elements import ColumnElement

STOCK_NEWS_SOURCE = "akshare:stock_news_em"


def _active_news_entity_predicate(model: type[Any]) -> ColumnElement[bool]:
    """关键词新闻必须通过实体校验，其他来源保持原有可见性。"""

    validation_status = model.payload["entity_validation"]["status"].astext
    return or_(
        model.source != STOCK_NEWS_SOURCE,
        validation_status == "passed",
    )


def active_event_predicate(model: type[Any]) -> ColumnElement[bool]:
    """返回活动事件查询使用的实体校验谓词。"""

    return _active_news_entity_predicate(model)


def active_evidence_predicate(model: type[Any]) -> ColumnElement[bool]:
    """返回活动证据查询使用的实体校验谓词。"""

    return _active_news_entity_predicate(model)
