# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike Google Ads MCP Server
==========================

Servidor MCP para a Google Ads API v16 (REST).

Recursos implementados:
    - Campanhas, ad groups, anuncios com metricas
    - Resumo da conta e metricas diarias (dashboard)
    - Pesquisa de keywords (Keyword Plan Idea Service)
    - Search terms que dispararam anuncios
    - Informacao de orcamento e previsao de gasto

Configuracao:
    OAuth2 Desktop (mesmo client ID dos outros servicos Google do Mike).
    O token OAuth2 PRECISA incluir o scope:
        https://www.googleapis.com/auth/adwords

    Variaveis de ambiente obrigatorias:
        GOOGLE_ADS_ACCOUNT_ID       CID da conta sem hifen (ex: 1234567890)
        GOOGLE_ADS_DEVELOPER_TOKEN  Developer token da Google Ads API

    Variavel de ambiente opcional (contas MCC):
        GOOGLE_ADS_LOGIN_CUSTOMER_ID  CID do manager (MCC)
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger("mike.ads_mcp")

# ---------------------------------------------------------------------------
# Path resolution (same pattern as calendar / drive / email MCPs)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(
    os.getenv("MIKE_HOME") or Path(__file__).resolve().parents[2]
).resolve()
_integration_dir = PROJECT_ROOT / "core" / "integrations"
if str(_integration_dir) not in sys.path:
    sys.path.insert(0, str(_integration_dir))

from mike_google_auth import load_google_credentials  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GOOGLE_ADS_SCOPES = ["https://www.googleapis.com/auth/adwords"]

GOOGLE_ADS_ACCOUNT_ID = os.getenv("GOOGLE_ADS_ACCOUNT_ID", "").strip().replace("-", "")
GOOGLE_ADS_DEVELOPER_TOKEN = os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN", "").strip()
GOOGLE_ADS_LOGIN_CUSTOMER_ID = os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "").strip()

GOOGLE_ADS_TOKEN_ENV_NAMES = ["MIKE_ADS_TOKEN", "MIKE_GOOGLE_TOKEN"]
GOOGLE_ADS_TOKEN_DEFAULTS = [
    "config/google_workspace_token.json",
    "config/ads_token.json",
    "google_workspace_token.json",
    "ads_token.json",
]

ADS_API_BASE = "https://googleads.googleapis.com/v16"

# Rate limiting: 15 req/s (Google Ads basic access)
# Ref: https://developers.google.com/google-ads/api/docs/best-practices/rate-limits
_MIN_REQUEST_INTERVAL = 1.0 / 15
_last_request_time = 0.0

# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------
mcp = FastMCP("Mike Google Ads MCP", json_response=True)


# ===================================================================
# Helpers
# ===================================================================


def _get_credentials():
    """Load and refresh OAuth2 credentials for Google Ads.

    Uses the same ``mike_google_auth.load_google_credentials`` path as
    Calendar / Gmail / Drive, but with the ``adwords`` scope.
    """
    creds, _token_path = load_google_credentials(
        GOOGLE_ADS_SCOPES,
        token_env_names=GOOGLE_ADS_TOKEN_ENV_NAMES,
        token_defaults=GOOGLE_ADS_TOKEN_DEFAULTS,
    )
    return creds


def _check_config():
    """Raise if required env vars are missing."""
    missing = []
    if not GOOGLE_ADS_ACCOUNT_ID:
        missing.append("GOOGLE_ADS_ACCOUNT_ID (sem hifen, ex: 1234567890)")
    if not GOOGLE_ADS_DEVELOPER_TOKEN:
        missing.append("GOOGLE_ADS_DEVELOPER_TOKEN")
    if missing:
        raise RuntimeError(
            "Configuracao Google Ads incompleta. "
            "Defina as variaveis de ambiente: " + ", ".join(missing)
        )


def _rate_limit():
    """Sleep if necessary to stay within Google Ads basic rate limit (15 req/s)."""
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < _MIN_REQUEST_INTERVAL:
        time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
    _last_request_time = time.monotonic()


def _auth_headers(creds) -> dict[str, str]:
    """Return HTTP headers needed for every Google Ads REST request."""
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "developer-token": GOOGLE_ADS_DEVELOPER_TOKEN,
        "Content-Type": "application/json",
    }
    if GOOGLE_ADS_LOGIN_CUSTOMER_ID:
        headers["login-customer-id"] = GOOGLE_ADS_LOGIN_CUSTOMER_ID
    return headers


def _gaql_query(
    query: str,
    customer_id: Optional[str] = None,
    *,
    page_size: int = 10000,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    """Execute a GAQL query against the Google Ads REST API.

    Automatically paginates through ``nextPageToken`` until exhausted or
    ``max_pages`` is reached (safety limit).  Caller is responsible for
    including ``LIMIT`` in the GAQL query to cap total results.

    Args:
        query: GAQL query string.
        customer_id: Override the default ``GOOGLE_ADS_ACCOUNT_ID``.
        page_size: Results per page (max 10000).
        max_pages: Hard safety limit on number of pages fetched.

    Returns:
        List of raw result rows (each a dict with resource + metrics keys).

    Raises:
        RuntimeError: If configuration is missing or the API returns an error.
    """
    _check_config()
    creds = _get_credentials()
    cid = str(customer_id or GOOGLE_ADS_ACCOUNT_ID)

    url = f"{ADS_API_BASE}/customers/{cid}/googleAds:search"
    headers = _auth_headers(creds)

    all_results: list[dict[str, Any]] = []
    page_token: Optional[str] = None
    pages_fetched = 0

    while pages_fetched < max_pages:
        _rate_limit()

        body: dict[str, Any] = {"query": query, "pageSize": min(page_size, 10000)}
        if page_token:
            body["pageToken"] = page_token

        try:
            resp = httpx.post(url, json=body, headers=headers, timeout=45.0)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500] if exc.response is not None else str(exc)
            log.error(
                "GAQL query failed [HTTP %s]: %s",
                exc.response.status_code if exc.response is not None else "?",
                detail,
            )
            raise RuntimeError(
                f"Google Ads API error {exc.response.status_code if exc.response else '?'}: "
                f"{detail}"
            ) from exc

        data: dict[str, Any] = resp.json()
        batch = data.get("results", [])
        all_results.extend(batch)
        pages_fetched += 1

        page_token = data.get("nextPageToken")
        if not page_token or len(batch) == 0:
            break

    return all_results


def _make_request(
    endpoint: str,
    body: Optional[dict[str, Any]] = None,
    customer_id: Optional[str] = None,
) -> dict[str, Any]:
    """Make a single (non-GAQL) REST request to Google Ads (e.g. keyword ideas).

    Args:
        endpoint: Path after ``/customers/{cid}``, e.g. ``:generateKeywordIdeas``.
        body: JSON body for POST.
        customer_id: Override default account ID.

    Returns:
        Parsed JSON response dict.
    """
    _check_config()
    creds = _get_credentials()
    cid = str(customer_id or GOOGLE_ADS_ACCOUNT_ID)

    url = f"{ADS_API_BASE}/customers/{cid}{endpoint}"
    headers = _auth_headers(creds)

    _rate_limit()

    try:
        resp = httpx.post(url, json=body or {}, headers=headers, timeout=45.0)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500] if exc.response is not None else str(exc)
        log.error(
            "Ads REST request failed [HTTP %s] %s => %s",
            exc.response.status_code if exc.response is not None else "?",
            endpoint,
            detail,
        )
        raise RuntimeError(
            f"Google Ads API error {exc.response.status_code if exc.response else '?'}: "
            f"{detail}"
        ) from exc

    return resp.json()


# ------------------------------------------------------------------
# Value conversion helpers
# ------------------------------------------------------------------


def _micros(value: Any, default: float = 0.0) -> float:
    """Convert micros (1,000,000 micros = 1 currency unit) to float."""
    if value is None or value == "":
        return default
    try:
        return int(value) / 1_000_000
    except (ValueError, TypeError):
        return default


def _pct(value: Any, default: float = 0.0) -> float:
    """Convert a GAQL ratio to percentage (e.g. 0.05 -> 5.0)."""
    if value is None or value == "":
        return default
    try:
        return round(float(value) * 100, 2)
    except (ValueError, TypeError):
        return default


def _safe(value: Any, default: Any = None) -> Any:
    """Return *value* unless it is ``None`` or empty-string, then *default*."""
    if value is None or value == "":
        return default
    return value


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _nullable_int(value: Any) -> Optional[int]:
    """Return an int or None (for ID fields that may be absent)."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


# ===================================================================
# Tool 1: ads_list_campaigns
# ===================================================================


@mcp.tool(description=(
    "Lista campanhas Google Ads com metricas principais. "
    "Filtra por status (ENABLED, PAUSED, REMOVED). "
    "Usa 'ALL' para listar todos os status."
))
def ads_list_campaigns(status: str = "ENABLED", limit: int = 50) -> list[dict[str, Any]]:
    status_clause = ""
    if status.upper() != "ALL":
        status_clause = f"WHERE campaign.status = '{status.upper()}'"

    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign.status,
            campaign.advertising_channel_type,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.ctr,
            metrics.average_cpc
        FROM campaign
        {status_clause}
        ORDER BY metrics.cost_micros DESC
        LIMIT {max(1, min(int(limit), 200))}
    """

    rows = _gaql_query(query)

    results: list[dict[str, Any]] = []
    for row in rows:
        c = row.get("campaign", {})
        m = row.get("metrics", {})
        results.append({
            "id": _nullable_int(c.get("id")),
            "name": _safe(c.get("name"), ""),
            "status": _safe(c.get("status"), ""),
            "channel": _safe(c.get("advertisingChannelType"), ""),
            "cost": round(_micros(m.get("costMicros")), 2),
            "impressions": _safe_int(m.get("impressions")),
            "clicks": _safe_int(m.get("clicks")),
            "conversions": _safe_float(m.get("conversions")),
            "ctr_pct": _pct(m.get("ctr")),
            "avg_cpc": round(_micros(m.get("averageCpc")), 2),
        })

    return results


# ===================================================================
# Tool 2: ads_get_campaign
# ===================================================================


@mcp.tool(description="Detalhes completos de uma campanha especifica pelo ID numerico.")
def ads_get_campaign(campaign_id: int) -> dict[str, Any]:
    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign.status,
            campaign.advertising_channel_type,
            campaign.start_date,
            campaign.end_date,
            campaign.serving_status,
            campaign.optimization_score,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.average_cpc,
            metrics.video_views,
            metrics.interactions,
            metrics.interaction_rate
        FROM campaign
        WHERE campaign.id = {int(campaign_id)}
        LIMIT 1
    """

    rows = _gaql_query(query)
    if not rows:
        return {"error": f"Campanha {campaign_id} nao encontrada ou sem metricas."}

    row = rows[0]
    c = row.get("campaign", {})
    m = row.get("metrics", {})

    cost = _micros(m.get("costMicros"))
    conv_value = _micros(m.get("conversionsValue"))
    roas = round(conv_value / cost, 2) if cost > 0 else 0.0

    return {
        "id": _nullable_int(c.get("id")),
        "name": _safe(c.get("name"), ""),
        "status": _safe(c.get("status"), ""),
        "channel": _safe(c.get("advertisingChannelType"), ""),
        "serving_status": _safe(c.get("servingStatus"), ""),
        "start_date": _safe(c.get("startDate"), ""),
        "end_date": _safe(c.get("endDate"), ""),
        "optimization_score": _safe_float(c.get("optimizationScore")),
        "metrics": {
            "cost": round(cost, 2),
            "impressions": _safe_int(m.get("impressions")),
            "clicks": _safe_int(m.get("clicks")),
            "conversions": _safe_float(m.get("conversions")),
            "conversions_value": round(conv_value, 2),
            "roas": roas,
            "ctr_pct": _pct(m.get("ctr")),
            "avg_cpc": round(_micros(m.get("averageCpc")), 2),
            "video_views": _safe_int(m.get("videoViews")),
            "interactions": _safe_int(m.get("interactions")),
            "interaction_rate_pct": _pct(m.get("interactionRate")),
        },
    }


# ===================================================================
# Tool 3: ads_list_ad_groups
# ===================================================================


@mcp.tool(description="Lista ad groups de uma campanha com metricas.")
def ads_list_ad_groups(campaign_id: int, limit: int = 50) -> list[dict[str, Any]]:
    query = f"""
        SELECT
            ad_group.id,
            ad_group.name,
            ad_group.status,
            ad_group.type,
            campaign.id,
            campaign.name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.ctr,
            metrics.average_cpc
        FROM ad_group
        WHERE campaign.id = {int(campaign_id)}
        ORDER BY metrics.cost_micros DESC
        LIMIT {max(1, min(int(limit), 200))}
    """

    rows = _gaql_query(query)

    results: list[dict[str, Any]] = []
    for row in rows:
        ag = row.get("adGroup", {})
        m = row.get("metrics", {})
        results.append({
            "ad_group_id": _nullable_int(ag.get("id")),
            "name": _safe(ag.get("name"), ""),
            "status": _safe(ag.get("status"), ""),
            "type": _safe(ag.get("type"), ""),
            "campaign_id": _nullable_int(row.get("campaign", {}).get("id")),
            "cost": round(_micros(m.get("costMicros")), 2),
            "impressions": _safe_int(m.get("impressions")),
            "clicks": _safe_int(m.get("clicks")),
            "conversions": _safe_float(m.get("conversions")),
            "ctr_pct": _pct(m.get("ctr")),
            "avg_cpc": round(_micros(m.get("averageCpc")), 2),
        })

    return results


# ===================================================================
# Tool 4: ads_list_ads
# ===================================================================


def _extract_ad_headline(ad: dict[str, Any]) -> str:
    """Best-effort headline extraction for various ad types."""
    rsa = ad.get("responsiveSearchAd")
    if rsa:
        headlines = rsa.get("headlines", [])
        if headlines:
            return headlines[0].get("text", "") if isinstance(headlines[0], dict) else str(headlines[0])
    eta = ad.get("expandedTextAd")
    if eta:
        return eta.get("headlinePart1", "") or ""
    ta = ad.get("textAd")
    if ta:
        return ta.get("headline", "") or ""
    return ad.get("name", "") or ""


def _extract_ad_description(ad: dict[str, Any]) -> str:
    """Best-effort description extraction."""
    rsa = ad.get("responsiveSearchAd")
    if rsa:
        descs = rsa.get("descriptions", [])
        if descs:
            return descs[0].get("text", "") if isinstance(descs[0], dict) else str(descs[0])
    eta = ad.get("expandedTextAd")
    if eta:
        return eta.get("description", "") or ""
    ta = ad.get("textAd")
    if ta:
        return ta.get("description1", "") or ""
    return ""


@mcp.tool(description=(
    "Lista anuncios de uma campanha com headlines/descriptions e metricas. "
    "Suporta Responsive Search Ads, Expanded Text Ads e Text Ads."
))
def ads_list_ads(campaign_id: int, limit: int = 50) -> list[dict[str, Any]]:
    query = f"""
        SELECT
            ad_group_ad.ad.id,
            ad_group_ad.ad.name,
            ad_group_ad.ad.type,
            ad_group_ad.ad.final_urls,
            ad_group_ad.ad.responsive_search_ad.headlines,
            ad_group_ad.ad.responsive_search_ad.descriptions,
            ad_group_ad.ad.expanded_text_ad.headline_part1,
            ad_group_ad.ad.expanded_text_ad.headline_part2,
            ad_group_ad.ad.expanded_text_ad.description,
            ad_group_ad.ad.text_ad.headline,
            ad_group_ad.ad.text_ad.description1,
            ad_group_ad.status,
            ad_group.id,
            ad_group.name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.ctr
        FROM ad_group_ad
        WHERE campaign.id = {int(campaign_id)}
        ORDER BY metrics.impressions DESC
        LIMIT {max(1, min(int(limit), 200))}
    """

    rows = _gaql_query(query)

    results: list[dict[str, Any]] = []
    for row in rows:
        aga = row.get("adGroupAd", {})
        ad = aga.get("ad", {}) if isinstance(aga, dict) else {}
        m = row.get("metrics", {})
        ag = row.get("adGroup", {})

        final_urls = ad.get("finalUrls", [])
        if final_urls is None:
            final_urls = []

        results.append({
            "ad_id": _nullable_int(ad.get("id")),
            "name": _safe(ad.get("name"), ""),
            "type": _safe(ad.get("type"), ""),
            "status": _safe(aga.get("status"), "") if isinstance(aga, dict) else "",
            "ad_group_id": _nullable_int(ag.get("id")),
            "ad_group_name": _safe(ag.get("name"), ""),
            "headline": _extract_ad_headline(ad),
            "description": _extract_ad_description(ad),
            "final_url": final_urls[0] if final_urls else "",
            "all_final_urls": final_urls,
            "impressions": _safe_int(m.get("impressions")),
            "clicks": _safe_int(m.get("clicks")),
            "cost": round(_micros(m.get("costMicros")), 2),
            "conversions": _safe_float(m.get("conversions")),
            "ctr_pct": _pct(m.get("ctr")),
        })

    return results


# ===================================================================
# Tool 5: ads_get_account_summary
# ===================================================================


@mcp.tool(description="Resumo da conta Google Ads: custo, impressoes, cliques, CTR, CPC, conversoes, ROAS nos ultimos N dias.")
def ads_get_account_summary(since_days: int = 30) -> dict[str, Any]:
    days = max(1, min(int(since_days), 365))

    # Get customer info
    customer_rows = _gaql_query("""
        SELECT
            customer.id,
            customer.descriptive_name,
            customer.currency_code,
            customer.time_zone
        FROM customer
        LIMIT 1
    """)
    customer = customer_rows[0].get("customer", {}) if customer_rows else {}

    # Get aggregated metrics across campaigns
    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign.status,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.average_cpc
        FROM campaign
        WHERE campaign.status != 'REMOVED'
          AND segments.date DURING LAST_{days}_DAYS
    """

    rows = _gaql_query(query)

    total_cost = 0.0
    total_impressions = 0
    total_clicks = 0
    total_conversions = 0.0
    total_conv_value = 0.0

    campaigns_summary: list[dict[str, Any]] = []
    for row in rows:
        c = row.get("campaign", {})
        m = row.get("metrics", {})
        cost = _micros(m.get("costMicros"))
        total_cost += cost
        total_impressions += _safe_int(m.get("impressions"))
        total_clicks += _safe_int(m.get("clicks"))
        total_conversions += _safe_float(m.get("conversions"))
        total_conv_value += _micros(m.get("conversionsValue"))
        campaigns_summary.append({
            "id": _nullable_int(c.get("id")),
            "name": _safe(c.get("name"), ""),
            "status": _safe(c.get("status"), ""),
            "cost": round(cost, 2),
            "impressions": _safe_int(m.get("impressions")),
            "clicks": _safe_int(m.get("clicks")),
            "conversions": _safe_float(m.get("conversions")),
        })

    ctr = (total_clicks / total_impressions * 100) if total_impressions > 0 else 0.0
    avg_cpc = total_cost / total_clicks if total_clicks > 0 else 0.0
    roas = (total_conv_value / total_cost) if total_cost > 0 else 0.0

    return {
        "account": {
            "id": _nullable_int(customer.get("id")),
            "name": _safe(customer.get("descriptiveName"), ""),
            "currency": _safe(customer.get("currencyCode"), ""),
            "timezone": _safe(customer.get("timeZone"), ""),
        },
        "period_days": days,
        "totals": {
            "cost": round(total_cost, 2),
            "impressions": total_impressions,
            "clicks": total_clicks,
            "conversions": round(total_conversions, 2),
            "conversions_value": round(total_conv_value, 2),
            "ctr_pct": round(ctr, 2),
            "avg_cpc": round(avg_cpc, 2),
            "roas": round(roas, 2),
        },
        "campaigns": campaigns_summary[:50],
    }


# ===================================================================
# Tool 6: ads_get_campaign_performance
# ===================================================================


@mcp.tool(description="Metricas detalhadas de performance de uma campanha nos ultimos N dias.")
def ads_get_campaign_performance(campaign_id: int, since_days: int = 30) -> dict[str, Any]:
    days = max(1, min(int(since_days), 365))
    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign.status,
            campaign.advertising_channel_type,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.average_cpc,
            metrics.average_cpm,
            metrics.video_views,
            metrics.interactions,
            metrics.interaction_rate,
            metrics.invalid_clicks,
            metrics.invalid_click_rate,
            metrics.impression_share,
            metrics.search_impression_share,
            metrics.search_top_impression_share,
            metrics.search_absolute_top_impression_share
        FROM campaign
        WHERE campaign.id = {int(campaign_id)}
          AND segments.date DURING LAST_{days}_DAYS
        LIMIT 1
    """

    rows = _gaql_query(query)
    if not rows:
        return {"error": f"Sem dados de performance para campanha {campaign_id} nos ultimos {days} dias."}

    row = rows[0]
    c = row.get("campaign", {})
    m = row.get("metrics", {})
    cost = _micros(m.get("costMicros"))
    conv_value = _micros(m.get("conversionsValue"))
    roas = round(conv_value / cost, 2) if cost > 0 else 0.0

    return {
        "campaign_id": _nullable_int(c.get("id")),
        "name": _safe(c.get("name"), ""),
        "status": _safe(c.get("status"), ""),
        "channel": _safe(c.get("advertisingChannelType"), ""),
        "period_days": days,
        "cost": round(cost, 2),
        "impressions": _safe_int(m.get("impressions")),
        "clicks": _safe_int(m.get("clicks")),
        "conversions": _safe_float(m.get("conversions")),
        "conversions_value": round(conv_value, 2),
        "roas": roas,
        "ctr_pct": _pct(m.get("ctr")),
        "avg_cpc": round(_micros(m.get("averageCpc")), 2),
        "avg_cpm": round(_micros(m.get("averageCpm")), 2),
        "invalid_clicks": _safe_int(m.get("invalidClicks")),
        "invalid_click_rate_pct": _pct(m.get("invalidClickRate")),
        "impression_share_pct": _pct(m.get("impressionShare")),
        "search_impression_share_pct": _pct(m.get("searchImpressionShare")),
        "search_top_impression_share_pct": _pct(m.get("searchTopImpressionShare")),
        "search_absolute_top_impression_share_pct": _pct(m.get("searchAbsoluteTopImpressionShare")),
        "video_views": _safe_int(m.get("videoViews")),
        "interactions": _safe_int(m.get("interactions")),
        "interaction_rate_pct": _pct(m.get("interactionRate")),
    }


# ===================================================================
# Tool 7: ads_get_daily_metrics
# ===================================================================


@mcp.tool(description=(
    "Metricas diarias agregadas da conta para grafico de tendencia. "
    "Retorna custo, impressoes, cliques, conversoes e CTR por dia."
))
def ads_get_daily_metrics(since_days: int = 14) -> list[dict[str, Any]]:
    days = max(1, min(int(since_days), 90))
    query = f"""
        SELECT
            segments.date,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.ctr,
            metrics.average_cpc
        FROM campaign
        WHERE segments.date DURING LAST_{days}_DAYS
          AND campaign.status != 'REMOVED'
        ORDER BY segments.date ASC
    """

    rows = _gaql_query(query)

    # Aggregate by date (multiple campaigns per day)
    daily: dict[str, dict[str, Any]] = {}
    for row in rows:
        seg = row.get("segments", {})
        m = row.get("metrics", {})
        date = _safe(seg.get("date"), "")
        if not date:
            continue

        if date not in daily:
            daily[date] = {
                "date": date,
                "cost": 0.0,
                "impressions": 0,
                "clicks": 0,
                "conversions": 0.0,
            }

        daily[date]["cost"] += _micros(m.get("costMicros"))
        daily[date]["impressions"] += _safe_int(m.get("impressions"))
        daily[date]["clicks"] += _safe_int(m.get("clicks"))
        daily[date]["conversions"] += _safe_float(m.get("conversions"))

    # Final pass: compute CTR per day
    result = []
    for date_key in sorted(daily.keys()):
        entry = daily[date_key]
        ctr = (entry["clicks"] / entry["impressions"] * 100) if entry["impressions"] > 0 else 0.0
        avg_cpc = entry["cost"] / entry["clicks"] if entry["clicks"] > 0 else 0.0
        result.append({
            "date": entry["date"],
            "cost": round(entry["cost"], 2),
            "impressions": entry["impressions"],
            "clicks": entry["clicks"],
            "conversions": round(entry["conversions"], 2),
            "ctr_pct": round(ctr, 2),
            "avg_cpc": round(avg_cpc, 2),
        })

    return result


# ===================================================================
# Tool 8: ads_search_keywords
# ===================================================================


@mcp.tool(description=(
    "Pesquisa de keywords via Google Ads Keyword Planner (KeywordPlanIdeaService). "
    "Retorna ideias de keywords com volume mensal, competicao e CPC estimado. "
    "Suporta multiplas palavras-chave e localizacao opcional."
))
def ads_search_keywords(
    keyword_text: str,
    limit: int = 20,
    language_id: int = 1000,
    location_id: int = 2840,
) -> list[dict[str, Any]]:
    """
    Args:
        keyword_text: Palavra(s)-chave separadas por virgula (ex: "cafe, cafe gourmet").
        limit: Maximo de ideias a retornar (1-100).
        language_id: Language constant ID. Default 1000 = English.
                     Portugues = 1014, Espanhol = 1003.
        location_id: Geo target constant ID. Default 2840 = US.
                     Portugal = 2620, Brasil = 2076, Canada = 2124.

    Google Ads location/language IDs reference:
        https://developers.google.com/google-ads/api/data/geotargets
        https://developers.google.com/google-ads/api/data/language-codes
    """
    keywords = [kw.strip() for kw in str(keyword_text).split(",") if kw.strip()]
    if not keywords:
        return [{"error": "Informe pelo menos uma keyword para pesquisar."}]

    _max = max(1, min(int(limit), 100))
    body = {
        "keywordSeed": {"keywords": keywords},
        "language": f"languageConstants/{language_id}",
        "geoTargetConstants": [f"geoTargetConstants/{location_id}"],
        "includeAdultKeywords": False,
        "pageSize": _max,
    }

    data = _make_request(":generateKeywordIdeas", body=body)
    results_data = data.get("results", [])

    output: list[dict[str, Any]] = []
    for item in results_data:
        metrics = item.get("keywordIdeaMetrics", {})
        output.append({
            "keyword": _safe(item.get("text"), ""),
            "avg_monthly_searches": _safe_int(metrics.get("avgMonthlySearches")),
            "competition": _safe(metrics.get("competition"), "UNKNOWN"),
            "competition_index": _safe_int(metrics.get("competitionIndex")),
            "low_top_of_page_bid": round(_micros(metrics.get("lowTopOfPageBidMicros")), 2),
            "high_top_of_page_bid": round(_micros(metrics.get("highTopOfPageBidMicros")), 2),
        })

    return output


# ===================================================================
# Tool 9: ads_get_search_terms
# ===================================================================


@mcp.tool(description=(
    "Termos de pesquisa (search terms) que dispararam anuncios numa campanha, "
    "com metricas de conversao. Mostra o que os utilizadores realmente pesquisaram."
))
def ads_get_search_terms(campaign_id: int, since_days: int = 30) -> list[dict[str, Any]]:
    days = max(1, min(int(since_days), 365))
    query = f"""
        SELECT
            search_term_view.search_term,
            campaign.id,
            campaign.name,
            ad_group.id,
            ad_group.name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.average_cpc
        FROM search_term_view
        WHERE campaign.id = {int(campaign_id)}
          AND segments.date DURING LAST_{days}_DAYS
        ORDER BY metrics.impressions DESC
        LIMIT 200
    """

    rows = _gaql_query(query)

    results: list[dict[str, Any]] = []
    for row in rows:
        st = row.get("searchTermView", {})
        m = row.get("metrics", {})
        results.append({
            "search_term": _safe(st.get("searchTerm"), ""),
            "campaign_id": _nullable_int(row.get("campaign", {}).get("id")),
            "ad_group_id": _nullable_int(row.get("adGroup", {}).get("id")),
            "ad_group_name": _safe(row.get("adGroup", {}).get("name"), ""),
            "impressions": _safe_int(m.get("impressions")),
            "clicks": _safe_int(m.get("clicks")),
            "cost": round(_micros(m.get("costMicros")), 2),
            "conversions": _safe_float(m.get("conversions")),
            "conversions_value": round(_micros(m.get("conversionsValue")), 2),
            "ctr_pct": _pct(m.get("ctr")),
            "avg_cpc": round(_micros(m.get("averageCpc")), 2),
        })

    return results


# ===================================================================
# Tool 10: ads_get_budget_info
# ===================================================================


@mcp.tool(description=(
    "Informacao de orcamento: budgets configurados, gasto do mes atual "
    "e previsao de gasto (extrapolacao linear)."
))
def ads_get_budget_info() -> dict[str, Any]:
    import datetime as _dt

    # 1. Get budget configurations
    budget_rows = _gaql_query("""
        SELECT
            campaign_budget.id,
            campaign_budget.name,
            campaign_budget.amount_micros,
            campaign_budget.total_amount_micros,
            campaign_budget.status,
            campaign_budget.delivery_method,
            campaign_budget.type,
            campaign_budget.explicitly_shared,
            campaign_budget.reference_count
        FROM campaign_budget
    """)

    # 2. Get current month spend per campaign (to link campaigns -> budgets)
    spend_rows = _gaql_query("""
        SELECT
            campaign.id,
            campaign.name,
            campaign.status,
            campaign_budget.id,
            metrics.cost_micros
        FROM campaign
        WHERE segments.date DURING THIS_MONTH
          AND campaign.status != 'REMOVED'
    """)

    # Map budget_id -> campaign list + aggregated spend
    budget_map: dict[str, dict[str, Any]] = {}
    for row in budget_rows:
        cb = row.get("campaignBudget", {})
        budget_id = _safe(cb.get("id"), "")
        if not budget_id:
            continue
        budget_map[budget_id] = {
            "budget_id": _nullable_int(cb.get("id")),
            "name": _safe(cb.get("name"), ""),
            "daily_amount": round(_micros(cb.get("amountMicros")), 2),
            "total_amount": round(_micros(cb.get("totalAmountMicros")), 2)
            if cb.get("totalAmountMicros")
            else None,
            "status": _safe(cb.get("status"), ""),
            "delivery_method": _safe(cb.get("deliveryMethod"), ""),
            "type": _safe(cb.get("type"), ""),
            "reference_count": _safe_int(cb.get("referenceCount")),
            "campaigns": [],
            "spent_this_month": 0.0,
        }

    for row in spend_rows:
        cb = row.get("campaignBudget", {})
        budget_id = _safe(cb.get("id"), "")
        c = row.get("campaign", {})
        m = row.get("metrics", {})

        entry = budget_map.get(budget_id)
        if entry is None:
            continue

        cost = _micros(m.get("costMicros"))
        entry["spent_this_month"] += cost
        entry["campaigns"].append({
            "id": _nullable_int(c.get("id")),
            "name": _safe(c.get("name"), ""),
            "status": _safe(c.get("status"), ""),
            "spent": round(cost, 2),
        })

    # Compute projections
    now = _dt.datetime.now()
    days_in_month = _dt.datetime(now.year, now.month, 1).replace(
        month=now.month % 12 + 1 if now.month < 12 else 1
    ) - _dt.datetime(now.year, now.month, 1)
    # Handle year boundary
    import calendar as _cal
    days_in_month = _cal.monthrange(now.year, now.month)[1]
    day_of_month = now.day
    ratio = (day_of_month / max(days_in_month, 1)) if days_in_month > 0 else 0

    total_spent = 0.0
    total_daily_budget = 0.0
    budgets_output = []

    for entry in budget_map.values():
        total_spent += entry["spent_this_month"]
        daily = entry.get("daily_amount") or 0
        total_daily_budget += daily

        forecast = round(entry["spent_this_month"] / ratio, 2) if ratio > 0 else entry["spent_this_month"]
        budgets_output.append({
            "budget_id": entry["budget_id"],
            "name": entry["name"],
            "daily_amount": entry["daily_amount"],
            "delivery_method": entry["delivery_method"],
            "status": entry["status"],
            "spent_this_month": round(entry["spent_this_month"], 2),
            "forecast_month_end": forecast,
            "campaign_count": len(entry["campaigns"]),
            "campaigns": entry["campaigns"],
        })

    total_forecast = round(total_spent / ratio, 2) if ratio > 0 else total_spent

    return {
        "month": now.strftime("%Y-%m"),
        "day_of_month": day_of_month,
        "days_in_month": days_in_month,
        "total_budgets": len(budgets_output),
        "total_daily_budget": round(total_daily_budget, 2),
        "total_spent_this_month": round(total_spent, 2),
        "forecast_month_end": total_forecast,
        "spent_pct": (
            round((total_spent / total_daily_budget * 100 / days_in_month) * days_in_month, 1)
            if total_daily_budget > 0 else 0.0
        ),
        "budgets": budgets_output,
    }


# ===================================================================
# Tool 11 (bonus): ads_get_status
# ===================================================================


@mcp.tool(description=(
    "Verifica o estado da configuracao Google Ads: conta, token OAuth, developer token. "
    "Util para diagnostico inicial."
))
def ads_get_status() -> dict[str, Any]:
    status = {
        "configured": False,
        "account_id": None,
        "account_id_valid": False,
        "developer_token": "configured" if GOOGLE_ADS_DEVELOPER_TOKEN else "missing",
        "login_customer_id": GOOGLE_ADS_LOGIN_CUSTOMER_ID or None,
        "oauth": "unknown",
        "api_reachable": False,
        "errors": [],
    }

    # Check account ID format (10 digits, no hyphen)
    if GOOGLE_ADS_ACCOUNT_ID:
        clean = GOOGLE_ADS_ACCOUNT_ID.replace("-", "")
        if clean.isdigit() and len(clean) >= 10:
            status["account_id_valid"] = True
            status["account_id"] = clean
        else:
            status["errors"].append(
                f"GOOGLE_ADS_ACCOUNT_ID invalido: '{GOOGLE_ADS_ACCOUNT_ID}'. "
                "Deve ter 10 digitos (ex: 1234567890)."
            )
    else:
        status["errors"].append("GOOGLE_ADS_ACCOUNT_ID nao definido.")

    if not GOOGLE_ADS_DEVELOPER_TOKEN:
        status["errors"].append("GOOGLE_ADS_DEVELOPER_TOKEN nao definido.")

    # Check OAuth
    try:
        creds = _get_credentials()
        status["oauth"] = "ready" if creds.valid else "expired_but_refreshable"
    except RuntimeError as exc:
        status["oauth"] = "missing"
        status["errors"].append(f"OAuth2: {exc}")

    # Quick API connectivity test
    if status["account_id_valid"] and status["oauth"] in ("ready", "expired_but_refreshable"):
        try:
            rows = _gaql_query("SELECT customer.id FROM customer LIMIT 1")
            if rows:
                status["api_reachable"] = True
                status["configured"] = True
        except Exception as exc:
            status["errors"].append(f"API connectivity: {exc}")

    return status


# ===================================================================
# Entry point
# ===================================================================

if __name__ == "__main__":
    mcp.run(transport="stdio")
