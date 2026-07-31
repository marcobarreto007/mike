# Copyright (c) 2025-2026 Marco Barreto. All rights reserved.
# Proprietary software - see LICENSE file in project root.

"""
Mike GA4 Analytics MCP Server
==============================
Servidor MCP para Google Analytics 4 (GA4) Data API v1beta.
Fornece trafego, conversoes, ecommerce, audiencia, real-time e site speed.

Configuracao:
    1. Crie um client OAuth Desktop no Google Cloud com a API Analytics Data habilitada
    2. Defina GA4_PROPERTY_ID (ex: "properties/123456789" ou so "123456789")
    3. Token OAuth2 partilhado com google_workspace_token.json
    4. Rode: python setup_google_workspace_oauth.py (se necessario)
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

log = logging.getLogger("mike.ga4_mcp")

# ---------------------------------------------------------------------------
# Path setup -- mirror the pattern from mike_calendar_mcp / mike_drive_mcp
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(
    os.getenv("MIKE_HOME") or Path(__file__).resolve().parents[2]
).resolve()
integration_dir = PROJECT_ROOT / "core" / "integrations"
if str(integration_dir) not in sys.path:
    sys.path.insert(0, str(integration_dir))

from mike_google_auth import GA4_SCOPES, GA4_TOKEN_DEFAULTS, GA4_TOKEN_ENV_NAMES, ga4_service, oauth_token_status  # noqa: E402

# ---------------------------------------------------------------------------
# MCP instance
# ---------------------------------------------------------------------------
mcp = FastMCP("Mike GA4 Analytics MCP", json_response=True)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_GA4_PROPERTY_ID_RAW = os.getenv("GA4_PROPERTY_ID", "").strip()
_MAX_RETRIES = 3
_BASE_BACKOFF = 1.5  # seconds, exponential multiplier

# ---------------------------------------------------------------------------
# Service & property helpers
# ---------------------------------------------------------------------------

_service_cache: Optional[Any] = None


def _service():
    """Return a cached GA4 Data API service object (analyticsdata v1beta)."""
    global _service_cache
    if _service_cache is not None:
        return _service_cache
    svc, _token_path = ga4_service()
    _service_cache = svc
    return svc


def _property_name() -> str:
    """Normalize GA4_PROPERTY_ID into the full 'properties/XXXXX' resource name."""
    if not _GA4_PROPERTY_ID_RAW:
        raise RuntimeError(
            "GA4_PROPERTY_ID nao definido. "
            "Defina a env var com o ID da propriedade GA4 (ex: properties/123456789 ou 123456789)."
        )
    if _GA4_PROPERTY_ID_RAW.startswith("properties/"):
        return _GA4_PROPERTY_ID_RAW
    return f"properties/{_GA4_PROPERTY_ID_RAW}"


def _safe_float(value: Optional[str]) -> float:
    """Convert a metric string value to float, returning 0.0 on failure."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _safe_int(value: Optional[str]) -> int:
    """Convert a metric string value to int, returning 0 on failure."""
    if value is None:
        return 0
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return 0


def _optional_int(value: Any) -> Optional[int]:
    """Like _safe_int but returns None for empty/missing values."""
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Core GA4 report runner
# ---------------------------------------------------------------------------

def _ga4_run_report(
    dimensions: list[dict[str, str]],
    metrics: list[dict[str, str]],
    since_days: int = 30,
    limit: int = 20,
    *,
    dimension_filter: Optional[dict[str, Any]] = None,
    metric_filter: Optional[dict[str, Any]] = None,
    order_bys: Optional[list[dict[str, Any]]] = None,
    metric_aggregations: Optional[list[str]] = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Execute a GA4 RunReportRequest and return parsed rows.

    Handles rate limiting with exponential backoff (up to _MAX_RETRIES).
    Returns a list of dicts, one per row, with dimension values mapped by name.
    """
    property_name = _property_name()
    body: dict[str, Any] = {
        "dimensions": dimensions,
        "metrics": metrics,
        "dateRanges": [
            {
                "startDate": f"{max(1, int(since_days))}daysAgo",
                "endDate": "today",
            }
        ],
    }
    if limit > 0:
        body["limit"] = str(int(limit))
    if offset > 0:
        body["offset"] = str(int(offset))
    if dimension_filter:
        body["dimensionFilter"] = dimension_filter
    if metric_filter:
        body["metricFilter"] = metric_filter
    if order_bys:
        body["orderBys"] = order_bys
    if metric_aggregations:
        body["metricAggregations"] = metric_aggregations

    svc = _service()

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = (
                svc.properties()
                .runReport(property=property_name, body=body)
                .execute()
            )
            break
        except Exception as exc:
            err_msg = str(exc).lower()
            is_rate = "quota" in err_msg or "rate" in err_msg or "429" in str(exc)
            if is_rate and attempt < _MAX_RETRIES:
                wait = _BASE_BACKOFF ** attempt
                log.warning(
                    "GA4 rate-limit hit (attempt %d/%d), sleeping %.1fs",
                    attempt,
                    _MAX_RETRIES,
                    wait,
                )
                time.sleep(wait)
            else:
                raise

    # Build lookup of dimension names
    dim_names = [d["name"] for d in response.get("dimensionHeaders", [])]
    metric_names = [m["name"] for m in response.get("metricHeaders", [])]

    rows: list[dict[str, Any]] = []
    for row in response.get("rows", []):
        entry: dict[str, Any] = {}
        # Map dimension values
        for idx, dv in enumerate(row.get("dimensionValues", [])):
            if idx < len(dim_names):
                entry[dim_names[idx]] = dv.get("value", "")
        # Map metric values
        for idx, mv in enumerate(row.get("metricValues", [])):
            if idx < len(metric_names):
                entry[metric_names[idx]] = mv.get("value", "0")
        rows.append(entry)

    return rows


def _ga4_run_aggregate(
    metrics: list[dict[str, str]],
    since_days: int = 30,
    *,
    dimension_filter: Optional[dict[str, Any]] = None,
    metric_filter: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Run a GA4 report with metricAggregations=['TOTAL'] and return the totals row."""
    property_name = _property_name()
    body: dict[str, Any] = {
        "dimensions": [],  # no dimensions -- aggregated
        "metrics": metrics,
        "dateRanges": [
            {
                "startDate": f"{max(1, int(since_days))}daysAgo",
                "endDate": "today",
            }
        ],
        "metricAggregations": ["TOTAL"],
    }
    if dimension_filter:
        body["dimensionFilter"] = dimension_filter
    if metric_filter:
        body["metricFilter"] = metric_filter

    svc = _service()

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = (
                svc.properties()
                .runReport(property=property_name, body=body)
                .execute()
            )
            break
        except Exception as exc:
            err_msg = str(exc).lower()
            is_rate = "quota" in err_msg or "rate" in err_msg or "429" in str(exc)
            if is_rate and attempt < _MAX_RETRIES:
                wait = _BASE_BACKOFF ** attempt
                log.warning(
                    "GA4 rate-limit hit (attempt %d/%d), sleeping %.1fs",
                    attempt,
                    _MAX_RETRIES,
                    wait,
                )
                time.sleep(wait)
            else:
                raise

    metric_names = [m["name"] for m in response.get("metricHeaders", [])]
    totals = response.get("totals", [])
    entry: dict[str, Any] = {}
    if totals:
        for idx, mv in enumerate(totals[0].get("metricValues", [])):
            if idx < len(metric_names):
                entry[metric_names[idx]] = mv.get("value", "0")
    return entry


# ---------------------------------------------------------------------------
# Tool 1 -- Traffic Overview (aggregate)
# ---------------------------------------------------------------------------

@mcp.tool(description=(
    "Visao geral de trafego GA4: total users, sessions, pageviews, "
    "bounce rate e avg session duration nos ultimos N dias (padrao 30)."
))
def ga4_get_traffic_overview(since_days: int = 30) -> dict[str, Any]:
    metrics = [
        {"name": "totalUsers"},
        {"name": "sessions"},
        {"name": "screenPageViews"},
        {"name": "bounceRate"},
        {"name": "averageSessionDuration"},
        {"name": "engagedSessions"},
        {"name": "engagementRate"},
    ]
    totals = _ga4_run_aggregate(metrics, since_days=since_days)
    return {
        "period_days": since_days,
        "total_users": _safe_int(totals.get("totalUsers")),
        "total_sessions": _safe_int(totals.get("sessions")),
        "total_pageviews": _safe_int(totals.get("screenPageViews")),
        "bounce_rate": round(_safe_float(totals.get("bounceRate")), 2),
        "avg_session_duration_sec": round(_safe_float(totals.get("averageSessionDuration")), 1),
        "engaged_sessions": _safe_int(totals.get("engagedSessions")),
        "engagement_rate": round(_safe_float(totals.get("engagementRate")), 4),
    }


# ---------------------------------------------------------------------------
# Tool 2 -- Traffic by Source / Medium
# ---------------------------------------------------------------------------

@mcp.tool(description=(
    "Trafego GA4 por origem (source/medium): Google, direct, social, etc. "
    "Retorna users e sessoes por source/medium nos ultimos N dias."
))
def ga4_get_traffic_by_source(since_days: int = 30, limit: int = 15) -> list[dict[str, Any]]:
    dimensions = [
        {"name": "sessionSource"},
        {"name": "sessionMedium"},
    ]
    metrics = [
        {"name": "totalUsers"},
        {"name": "sessions"},
        {"name": "engagedSessions"},
        {"name": "engagementRate"},
        {"name": "screenPageViews"},
    ]
    order_bys = [
        {"metric": {"metricName": "sessions"}, "desc": True}
    ]
    rows = _ga4_run_report(
        dimensions, metrics, since_days=since_days, limit=limit, order_bys=order_bys,
    )
    return [
        {
            "source": row.get("sessionSource", "unknown"),
            "medium": row.get("sessionMedium", "unknown"),
            "source_medium": f"{row.get('sessionSource', 'unknown')} / {row.get('sessionMedium', 'unknown')}",
            "total_users": _safe_int(row.get("totalUsers")),
            "sessions": _safe_int(row.get("sessions")),
            "engaged_sessions": _safe_int(row.get("engagedSessions")),
            "engagement_rate": round(_safe_float(row.get("engagementRate")), 4),
            "pageviews": _safe_int(row.get("screenPageViews")),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Tool 3 -- Daily Traffic (trend)
# ---------------------------------------------------------------------------

@mcp.tool(description=(
    "Trafego diario GA4: users e sessions por dia nos ultimos N dias. "
    "Ideal para graficos de tendencia."
))
def ga4_get_daily_traffic(since_days: int = 30) -> list[dict[str, Any]]:
    dimensions = [{"name": "date"}]
    metrics = [
        {"name": "totalUsers"},
        {"name": "newUsers"},
        {"name": "sessions"},
        {"name": "screenPageViews"},
    ]
    order_bys = [{"dimension": {"dimensionName": "date"}, "desc": False}]
    rows = _ga4_run_report(
        dimensions, metrics, since_days=since_days, limit=since_days + 1, order_bys=order_bys,
    )
    return [
        {
            "date": row.get("date", ""),
            "total_users": _safe_int(row.get("totalUsers")),
            "new_users": _safe_int(row.get("newUsers")),
            "sessions": _safe_int(row.get("sessions")),
            "pageviews": _safe_int(row.get("screenPageViews")),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Tool 4 -- Top Pages
# ---------------------------------------------------------------------------

@mcp.tool(description=(
    "Top paginas GA4 por pageviews, users e tempo medio de engagement. "
    "Retorna ate 'limit' paginas nos ultimos N dias."
))
def ga4_get_top_pages(since_days: int = 30, limit: int = 20) -> list[dict[str, Any]]:
    dimensions = [
        {"name": "pagePath"},
        {"name": "pageTitle"},
    ]
    metrics = [
        {"name": "screenPageViews"},
        {"name": "totalUsers"},
        {"name": "userEngagementDuration"},
        {"name": "sessions"},
    ]
    order_bys = [
        {"metric": {"metricName": "screenPageViews"}, "desc": True}
    ]
    rows = _ga4_run_report(
        dimensions, metrics, since_days=since_days, limit=limit, order_bys=order_bys,
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        views = _safe_int(row.get("screenPageViews"))
        eng_duration = _safe_float(row.get("userEngagementDuration"))
        result.append({
            "page_path": row.get("pagePath", ""),
            "page_title": row.get("pageTitle", ""),
            "pageviews": views,
            "total_users": _safe_int(row.get("totalUsers")),
            "avg_engagement_sec": round(eng_duration / max(views, 1), 1),
            "sessions": _safe_int(row.get("sessions")),
        })
    return result


# ---------------------------------------------------------------------------
# Tool 5 -- Device Breakdown
# ---------------------------------------------------------------------------

@mcp.tool(description=(
    "Distribuicao de trafego GA4 por dispositivo: Desktop vs Mobile vs Tablet."
))
def ga4_get_device_breakdown(since_days: int = 30) -> list[dict[str, Any]]:
    dimensions = [{"name": "deviceCategory"}]
    metrics = [
        {"name": "totalUsers"},
        {"name": "sessions"},
        {"name": "screenPageViews"},
        {"name": "engagementRate"},
        {"name": "bounceRate"},
        {"name": "averageSessionDuration"},
    ]
    order_bys = [
        {"metric": {"metricName": "sessions"}, "desc": True}
    ]
    rows = _ga4_run_report(
        dimensions, metrics, since_days=since_days, limit=5, order_bys=order_bys,
    )
    return [
        {
            "device": row.get("deviceCategory", "unknown"),
            "total_users": _safe_int(row.get("totalUsers")),
            "sessions": _safe_int(row.get("sessions")),
            "pageviews": _safe_int(row.get("screenPageViews")),
            "engagement_rate": round(_safe_float(row.get("engagementRate")), 4),
            "bounce_rate": round(_safe_float(row.get("bounceRate")), 2),
            "avg_session_duration_sec": round(_safe_float(row.get("averageSessionDuration")), 1),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Tool 6 -- Conversions
# ---------------------------------------------------------------------------

@mcp.tool(description=(
    "Eventos de conversao GA4: todos os eventos marcados como conversao "
    "(purchase, sign_up, generate_lead, etc). "
    "Retorna contagem de cada evento de conversao nos ultimos N dias."
))
def ga4_get_conversions(since_days: int = 30) -> list[dict[str, Any]]:
    dimensions = [{"name": "eventName"}]
    metrics = [
        {"name": "eventCount"},
        {"name": "totalUsers"},
        {"name": "eventCountPerUser"},
    ]
    dimension_filter = {
        "filter": {
            "fieldName": "isConversionEvent",
            "stringFilter": {
                "matchType": "EXACT",
                "value": "true",
            },
        }
    }
    try:
        rows = _ga4_run_report(
            dimensions, metrics,
            since_days=since_days, limit=50,
            dimension_filter=dimension_filter,
            order_bys=[{"metric": {"metricName": "eventCount"}, "desc": True}],
        )
    except Exception as exc:
        log.error("ga4_get_conversions failed: %s", exc)
        return [{"error": f"Falha ao obter conversoes: {exc}"}]

    return [
        {
            "event_name": row.get("eventName", ""),
            "event_count": _safe_int(row.get("eventCount")),
            "total_users": _safe_int(row.get("totalUsers")),
            "events_per_user": round(_safe_float(row.get("eventCountPerUser")), 2),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Tool 7 -- Ecommerce Overview
# ---------------------------------------------------------------------------

@mcp.tool(description=(
    "Visao geral de ecommerce GA4: revenue total, transactions, AOV, "
    "ecommerce conversion rate nos ultimos N dias."
))
def ga4_get_ecommerce_overview(since_days: int = 30) -> dict[str, Any]:
    metrics = [
        {"name": "totalPurchasers"},
        {"name": "purchaseRevenue"},
        {"name": "transactions"},
        {"name": "sessions"},
        {"name": "itemViews"},
        {"name": "addToCarts"},
        {"name": "ecommercePurchases"},
    ]
    totals = _ga4_run_aggregate(metrics, since_days=since_days)
    sessions = max(_safe_int(totals.get("sessions")), 1)
    transactions = _safe_int(totals.get("transactions"))
    revenue = _safe_float(totals.get("purchaseRevenue"))
    return {
        "period_days": since_days,
        "total_revenue": round(revenue, 2),
        "total_revenue_formatted": f"${revenue:,.2f}",
        "transactions": transactions,
        "total_purchasers": _safe_int(totals.get("totalPurchasers")),
        "average_order_value": round(revenue / max(transactions, 1), 2),
        "average_order_value_formatted": f"${revenue / max(transactions, 1):,.2f}",
        "ecommerce_conversion_rate_pct": round(transactions / sessions * 100, 2),
        "item_views": _safe_int(totals.get("itemViews")),
        "add_to_carts": _safe_int(totals.get("addToCarts")),
        "ecommerce_purchases": _safe_int(totals.get("ecommercePurchases")),
    }


# ---------------------------------------------------------------------------
# Tool 8 -- Top Products
# ---------------------------------------------------------------------------

@mcp.tool(description=(
    "Top produtos GA4: mais vendidos por item views, add to cart, purchases e revenue."
))
def ga4_get_top_products(since_days: int = 30, limit: int = 20) -> list[dict[str, Any]]:
    dimensions = [
        {"name": "itemName"},
        {"name": "itemId"},
    ]
    metrics = [
        {"name": "itemViews"},
        {"name": "addToCarts"},
        {"name": "itemPurchaseQuantity"},
        {"name": "itemRevenue"},
    ]
    order_bys = [
        {"metric": {"metricName": "itemRevenue"}, "desc": True}
    ]
    rows = _ga4_run_report(
        dimensions, metrics, since_days=since_days, limit=limit, order_bys=order_bys,
    )
    return [
        {
            "item_name": row.get("itemName", ""),
            "item_id": row.get("itemId", ""),
            "item_views": _safe_int(row.get("itemViews")),
            "add_to_carts": _safe_int(row.get("addToCarts")),
            "purchase_quantity": _safe_int(row.get("itemPurchaseQuantity")),
            "revenue": round(_safe_float(row.get("itemRevenue")), 2),
            "revenue_formatted": f"${_safe_float(row.get('itemRevenue')):,.2f}",
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Tool 9 -- Checkout Funnel
# ---------------------------------------------------------------------------

def _event_count_for_name(event_name: str, since_days: int) -> int:
    """Query the count of a specific event by name."""
    metrics = [{"name": "eventCount"}]
    dimension_filter = {
        "filter": {
            "fieldName": "eventName",
            "stringFilter": {
                "matchType": "EXACT",
                "value": event_name,
            },
        }
    }
    try:
        totals = _ga4_run_aggregate(metrics, since_days=since_days, dimension_filter=dimension_filter)
        return _safe_int(totals.get("eventCount"))
    except Exception:
        return 0


@mcp.tool(description=(
    "Funil de checkout GA4: view_item -> add_to_cart -> begin_checkout -> purchase. "
    "Calcula taxas de abandono entre cada etapa."
))
def ga4_get_checkout_funnel(since_days: int = 30) -> dict[str, Any]:
    steps = [
        ("view_item", "View Item (product page)"),
        ("add_to_cart", "Add to Cart"),
        ("begin_checkout", "Begin Checkout"),
        ("purchase", "Purchase"),
    ]
    funnel: list[dict[str, Any]] = []

    for event_name, label in steps:
        count = _event_count_for_name(event_name, since_days)
        funnel.append({
            "step": label,
            "event_name": event_name,
            "count": count,
        })

    # Calculate drop-off rates
    for i, step in enumerate(funnel):
        if i == 0:
            step["drop_off_from_previous_pct"] = 0.0
        else:
            prev_count = max(funnel[i - 1]["count"], 1)
            step["drop_off_from_previous_pct"] = round(
                (1 - step["count"] / prev_count) * 100, 1
            )

    # Overall conversion rate
    first_count = max(funnel[0]["count"], 1) if funnel else 1
    last_count = funnel[-1]["count"] if funnel else 0
    overall_conv_pct = round(last_count / first_count * 100, 2)

    return {
        "period_days": since_days,
        "funnel": funnel,
        "overall_conversion_rate_pct": overall_conv_pct,
    }


# ---------------------------------------------------------------------------
# Tool 10 -- Audience Geo
# ---------------------------------------------------------------------------

@mcp.tool(description=(
    "Audiencia GA4 por pais/cidade: users e sessoes por localizacao geografica."
))
def ga4_get_audience_geo(since_days: int = 30, limit: int = 20) -> list[dict[str, Any]]:
    dimensions = [
        {"name": "country"},
        {"name": "city"},
    ]
    metrics = [
        {"name": "totalUsers"},
        {"name": "sessions"},
        {"name": "screenPageViews"},
        {"name": "engagementRate"},
        {"name": "averageSessionDuration"},
    ]
    order_bys = [
        {"metric": {"metricName": "totalUsers"}, "desc": True}
    ]
    rows = _ga4_run_report(
        dimensions, metrics, since_days=since_days, limit=limit, order_bys=order_bys,
    )
    return [
        {
            "country": row.get("country", ""),
            "city": row.get("city", ""),
            "total_users": _safe_int(row.get("totalUsers")),
            "sessions": _safe_int(row.get("sessions")),
            "pageviews": _safe_int(row.get("screenPageViews")),
            "engagement_rate": round(_safe_float(row.get("engagementRate")), 4),
            "avg_session_duration_sec": round(_safe_float(row.get("averageSessionDuration")), 1),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Tool 11 -- New vs Returning
# ---------------------------------------------------------------------------

@mcp.tool(description=(
    "Comparacao New vs Returning users GA4 com engagement metrics."
))
def ga4_get_new_vs_returning(since_days: int = 30) -> list[dict[str, Any]]:
    dimensions = [{"name": "newVsReturning"}]
    metrics = [
        {"name": "totalUsers"},
        {"name": "sessions"},
        {"name": "engagedSessions"},
        {"name": "engagementRate"},
        {"name": "averageSessionDuration"},
        {"name": "screenPageViews"},
    ]
    rows = _ga4_run_report(
        dimensions, metrics, since_days=since_days, limit=2,
    )
    return [
        {
            "user_type": row.get("newVsReturning", "unknown"),
            "total_users": _safe_int(row.get("totalUsers")),
            "sessions": _safe_int(row.get("sessions")),
            "engaged_sessions": _safe_int(row.get("engagedSessions")),
            "engagement_rate": round(_safe_float(row.get("engagementRate")), 4),
            "avg_session_duration_sec": round(_safe_float(row.get("averageSessionDuration")), 1),
            "pageviews": _safe_int(row.get("screenPageViews")),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Tool 12 -- Realtime
# ---------------------------------------------------------------------------

@mcp.tool(description=(
    "Usuarios ativos AGORA no GA4 (ultimos 30 minutos) por pagina/titulo de ecra."
))
def ga4_get_realtime() -> dict[str, Any]:
    """Query the GA4 Realtime API for active users in the last ~30 minutes."""
    property_name = _property_name()
    svc = _service()

    # Active users by page
    body_pages: dict[str, Any] = {
        "dimensions": [
            {"name": "unifiedScreenName"},
        ],
        "metrics": [{"name": "activeUsers"}],
        "limit": "20",
    }

    # Active users total (aggregate)
    body_total: dict[str, Any] = {
        "dimensions": [],
        "metrics": [{"name": "activeUsers"}],
        "metricAggregations": ["TOTAL"],
    }

    def _call_realtime(body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return (
                    svc.properties()
                    .runRealtimeReport(property=property_name, body=body)
                    .execute()
                )
            except Exception as exc:
                err_msg = str(exc).lower()
                is_rate = "quota" in err_msg or "rate" in err_msg or "429" in str(exc)
                if is_rate and attempt < _MAX_RETRIES:
                    wait = _BASE_BACKOFF ** attempt
                    log.warning("GA4 realtime rate-limit, sleeping %.1fs", wait)
                    time.sleep(wait)
                else:
                    raise

    try:
        pages_response = _call_realtime(body_pages)
    except Exception as exc:
        log.error("GA4 realtime pages query failed: %s", exc)
        return {"error": f"Realtime query failed: {exc}", "active_users_total": 0, "pages": []}

    try:
        total_response = _call_realtime(body_total)
        total_metric_names = [m["name"] for m in total_response.get("metricHeaders", [])]
        totals = total_response.get("totals", [])
        active_users_total = 0
        if totals:
            for idx, mv in enumerate(totals[0].get("metricValues", [])):
                if idx < len(total_metric_names) and total_metric_names[idx] == "activeUsers":
                    active_users_total = _safe_int(mv.get("value"))
                    break
    except Exception:
        active_users_total = 0

    dim_names = [d["name"] for d in pages_response.get("dimensionHeaders", [])]
    metric_names_pages = [m["name"] for m in pages_response.get("metricHeaders", [])]

    pages: list[dict[str, Any]] = []
    for row in pages_response.get("rows", []):
        entry: dict[str, Any] = {}
        for idx, dv in enumerate(row.get("dimensionValues", [])):
            if idx < len(dim_names):
                entry[dim_names[idx]] = dv.get("value", "")
        for idx, mv in enumerate(row.get("metricValues", [])):
            if idx < len(metric_names_pages):
                entry[metric_names_pages[idx]] = mv.get("value", "0")
        pages.append({
            "page": entry.get("unifiedScreenName", ""),
            "active_users": _safe_int(entry.get("activeUsers")),
        })

    # Sort by active_users descending
    pages.sort(key=lambda p: p["active_users"], reverse=True)

    return {
        "active_users_total": active_users_total,
        "pages": pages,
        "queried_at_utc": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Tool 13 -- Page Speed (average engagement time per page)
# ---------------------------------------------------------------------------

@mcp.tool(description=(
    "Page speed GA4: tempo medio de engagement por pagina (proxy de velocidade). "
    "Retorna tempo medio em segundos por pagina nos ultimos N dias."
))
def ga4_get_page_speed(since_days: int = 30, limit: int = 20) -> list[dict[str, Any]]:
    dimensions = [
        {"name": "pagePath"},
        {"name": "pageTitle"},
    ]
    metrics = [
        {"name": "screenPageViews"},
        {"name": "userEngagementDuration"},
        {"name": "sessions"},
    ]
    order_bys = [
        {"metric": {"metricName": "screenPageViews"}, "desc": True}
    ]
    rows = _ga4_run_report(
        dimensions, metrics, since_days=since_days, limit=limit, order_bys=order_bys,
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        views = max(_safe_int(row.get("screenPageViews")), 1)
        eng_duration = _safe_float(row.get("userEngagementDuration"))
        result.append({
            "page_path": row.get("pagePath", ""),
            "page_title": row.get("pageTitle", ""),
            "pageviews": views,
            "avg_engagement_time_sec": round(eng_duration / views, 1),
            "total_engagement_time_sec": round(eng_duration, 1),
            "sessions": _safe_int(row.get("sessions")),
        })
    return result


# ---------------------------------------------------------------------------
# Tool -- Health Check / Status
# ---------------------------------------------------------------------------

@mcp.tool(description=(
    "Verifica se o GA4 esta configurado e autenticado. "
    "Retorna o estado do token OAuth e a propriedade GA4."
))
def ga4_status() -> dict[str, Any]:
    token_path, status = oauth_token_status(
        GA4_TOKEN_ENV_NAMES,
        GA4_TOKEN_DEFAULTS,
        GA4_SCOPES,
    )
    property_ok = bool(_GA4_PROPERTY_ID_RAW)
    return {
        "property_id": _GA4_PROPERTY_ID_RAW or "NAO DEFINIDO",
        "property_resource": _property_name() if property_ok else "N/A",
        "oauth_status": status,
        "token_path": str(token_path),
        "ready": status == "ready" and property_ok,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
