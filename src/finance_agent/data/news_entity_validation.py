"""A 股关键词新闻的确定性实体相关性判定。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

NEWS_ENTITY_RULE_VERSION = "ashare_news_entity_v1"

NewsEntityStatus = Literal["passed", "failed", "ambiguous"]

_LEGAL_SUFFIXES = (
    "集团股份有限公司",
    "股份有限责任公司",
    "股份有限公司",
    "集团有限公司",
    "有限责任公司",
    "有限公司",
)
_GENERIC_ALIASES = {
    "集团",
    "股份",
    "控股",
    "科技",
    "发展",
    "实业",
    "投资",
}
_NON_TARGET_SECURITY_TERMS = (
    "指数",
    "ETF",
    "交易型开放式指数基金",
)


@dataclass(frozen=True)
class NewsEntityDecision:
    """单条关键词新闻的实体判定结果。"""

    status: NewsEntityStatus
    reason: str
    matched_by: str | None
    expected_exchange: str
    aliases: tuple[str, ...]


def validate_ashare_news_entity(
    *,
    symbol: str,
    asset_name: str,
    title: str | None,
    summary: str | None,
) -> NewsEntityDecision:
    """判断标题和摘要能否证明新闻属于目标 A 股股票。"""

    clean_symbol = normalize_ashare_symbol(symbol)
    expected_exchange = ashare_exchange_suffix(clean_symbol)
    canonical_name = normalize_entity_text(asset_name)
    aliases = company_name_aliases(asset_name)
    text = normalize_entity_text(" ".join(value for value in (title, summary) if value))

    if not text:
        return NewsEntityDecision(
            status="failed",
            reason="empty_text",
            matched_by=None,
            expected_exchange=expected_exchange,
            aliases=aliases,
        )
    if not canonical_name or canonical_name == clean_symbol:
        return NewsEntityDecision(
            status="failed",
            reason="missing_asset_name",
            matched_by=None,
            expected_exchange=expected_exchange,
            aliases=aliases,
        )
    if conflicting_exchange_reference(text, clean_symbol, expected_exchange):
        return NewsEntityDecision(
            status="failed",
            reason="conflicting_exchange_suffix",
            matched_by=None,
            expected_exchange=expected_exchange,
            aliases=aliases,
        )
    if canonical_name in text:
        return NewsEntityDecision(
            status="passed",
            reason="canonical_name",
            matched_by="canonical_name",
            expected_exchange=expected_exchange,
            aliases=aliases,
        )
    if any(alias in text for alias in aliases):
        return NewsEntityDecision(
            status="passed",
            reason="company_alias",
            matched_by="company_alias",
            expected_exchange=expected_exchange,
            aliases=aliases,
        )
    if exchange_qualified_reference(text, clean_symbol, expected_exchange):
        return NewsEntityDecision(
            status="passed",
            reason="matching_exchange_suffix",
            matched_by="matching_exchange_suffix",
            expected_exchange=expected_exchange,
            aliases=aliases,
        )

    has_bare_symbol = bool(
        re.search(rf"(?<!\d){re.escape(clean_symbol)}(?!\d)", text)
    )
    if has_bare_symbol and any(term in text for term in _NON_TARGET_SECURITY_TERMS):
        return NewsEntityDecision(
            status="failed",
            reason="non_target_security_context",
            matched_by=None,
            expected_exchange=expected_exchange,
            aliases=aliases,
        )
    if has_bare_symbol:
        return NewsEntityDecision(
            status="ambiguous",
            reason="bare_symbol_only",
            matched_by=None,
            expected_exchange=expected_exchange,
            aliases=aliases,
        )
    return NewsEntityDecision(
        status="ambiguous",
        reason="target_entity_not_proven",
        matched_by=None,
        expected_exchange=expected_exchange,
        aliases=aliases,
    )


def normalize_entity_text(value: str | None) -> str:
    """统一全半角、大小写和空白，便于确定性文本匹配。"""

    normalized = unicodedata.normalize("NFKC", str(value or "")).upper()
    return re.sub(r"\s+", "", normalized)


def company_name_aliases(asset_name: str) -> tuple[str, ...]:
    """只从规范公司名法律形态后缀派生可追溯核心简称。"""

    canonical = normalize_entity_text(asset_name)
    aliases: list[str] = []
    for suffix in _LEGAL_SUFFIXES:
        normalized_suffix = normalize_entity_text(suffix)
        if not canonical.endswith(normalized_suffix):
            continue
        alias = canonical[: -len(normalized_suffix)]
        if _is_usable_alias(alias) and alias != canonical:
            aliases.append(alias)
        break
    return tuple(dict.fromkeys(aliases))


def ashare_exchange_suffix(symbol: str) -> str:
    """按股票代码推导预期交易所后缀。"""

    clean_symbol = normalize_ashare_symbol(symbol)
    if clean_symbol.startswith(("000", "001", "002", "003", "300", "301")):
        return "SZ"
    if clean_symbol.startswith(("600", "601", "603", "605", "688", "689")):
        return "SH"
    if clean_symbol.startswith(("4", "8", "920")):
        return "BJ"
    return ""


def conflicting_exchange_reference(
    text: str,
    symbol: str,
    expected_exchange: str,
) -> bool:
    """判断文本是否把同一代码明确指向其他交易所实体。"""

    references = exchange_references(text, symbol)
    return any(reference != expected_exchange for reference in references)


def exchange_qualified_reference(
    text: str,
    symbol: str,
    expected_exchange: str,
) -> bool:
    """判断文本是否包含与目标市场一致的完整证券代码。"""

    return bool(expected_exchange) and expected_exchange in exchange_references(text, symbol)


def exchange_references(text: str, symbol: str) -> tuple[str, ...]:
    """提取目标六位代码后显式标注的交易所后缀。"""

    clean_symbol = normalize_ashare_symbol(symbol)
    pattern = re.compile(
        rf"(?<!\d){re.escape(clean_symbol)}[._-]?(SH|SZ|BJ)(?![A-Z])"
    )
    return tuple(match.group(1) for match in pattern.finditer(text))


def normalize_ashare_symbol(symbol: str) -> str:
    """移除常见交易所前后缀，返回纯股票代码。"""

    value = normalize_entity_text(symbol)
    value = re.sub(r"^(SH|SZ|BJ)[._-]?", "", value)
    value = re.sub(r"[._-]?(SH|SZ|BJ)$", "", value)
    return value


def _is_usable_alias(alias: str) -> bool:
    if alias in _GENERIC_ALIASES:
        return False
    chinese_count = len(re.findall(r"[\u4e00-\u9fff]", alias))
    return chinese_count >= 2 or len(alias) >= 4
