"""Shopify Admin API MCP server for Mike — products, orders, customers, inventory."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

log = logging.getLogger("mike.shopify_mcp")
mcp = FastMCP("Mike Shopify MCP", json_response=True)

# ---------------------------------------------------------------------------
# Configuration (all from environment)
# ---------------------------------------------------------------------------
SHOPIFY_STORE_URL = os.getenv("SHOPIFY_STORE_URL", "").strip().rstrip("/")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()
API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-07").strip()
BASE_URL = (
    f"https://{SHOPIFY_STORE_URL}/admin/api/{API_VERSION}"
    if SHOPIFY_STORE_URL
    else ""
)
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.5  # seconds


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_config() -> None:
    """Raise RuntimeError if required env vars are missing."""
    if not SHOPIFY_STORE_URL or not SHOPIFY_ACCESS_TOKEN:
        raise RuntimeError(
            "SHOPIFY_STORE_URL and SHOPIFY_ACCESS_TOKEN must be set. "
            "Example: SHOPIFY_STORE_URL=mystore.myshopify.com"
        )


def _headers() -> dict[str, str]:
    return {
        "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _parse_retry_after(response: httpx.Response) -> Optional[float]:
    """Parse Retry-After header — returns seconds or None."""
    header = response.headers.get("Retry-After", "").strip()
    if not header:
        return None
    try:
        return float(header)
    except ValueError:
        return None


def _shopify_request(
    method: str,
    path: str,
    data: Optional[dict[str, Any]] = None,
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Make an authenticated request to the Shopify Admin REST API.

    Handles rate limiting (429) with exponential backoff and server errors
    (5xx) with retries.  Network errors are also retried.
    """
    _check_config()
    url = f"{BASE_URL}{path}"
    last_exc: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.request(
                    method=method.upper(),
                    url=url,
                    headers=_headers(),
                    json=data,
                    params=params,
                )

            # ---- rate limiting -------------------------------------------------
            if response.status_code == 429:
                retry_after = _parse_retry_after(response)
                wait = retry_after or (RETRY_BACKOFF_BASE ** attempt)
                log.warning(
                    "Shopify rate limited (429). Retry %s/%s after %.1fs  path=%s",
                    attempt,
                    MAX_RETRIES,
                    wait,
                    path,
                )
                time.sleep(wait)
                continue

            response.raise_for_status()

            # 204 No Content or truly empty body ---------------------------------
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except ValueError:
                log.warning(
                    "Shopify response not valid JSON (status=%s) path=%s",
                    response.status_code,
                    path,
                )
                return {}

        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            log.error(
                "Shopify HTTP %s on %s %s: %s",
                status,
                method.upper(),
                path,
                exc.response.text[:500],
            )
            last_exc = exc
            if attempt < MAX_RETRIES and status >= 500:
                wait = RETRY_BACKOFF_BASE ** attempt
                time.sleep(wait)
                continue
            raise

        except (httpx.RequestError, httpx.TimeoutException) as exc:
            log.error(
                "Shopify request error on %s %s: %s",
                method.upper(),
                path,
                exc,
            )
            last_exc = exc
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_BASE ** attempt
                time.sleep(wait)
                continue
            raise

    if last_exc:
        raise last_exc
    raise RuntimeError(f"Max retries exceeded for {method} {path}")


# ---------------------------------------------------------------------------
# Response summarizers — strip verbose / internal fields
# ---------------------------------------------------------------------------

def _product_summary(product: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": product.get("id"),
        "title": product.get("title"),
        "handle": product.get("handle"),
        "vendor": product.get("vendor"),
        "product_type": product.get("product_type"),
        "status": product.get("status"),
        "published_at": product.get("published_at"),
        "created_at": product.get("created_at"),
        "updated_at": product.get("updated_at"),
        "tags": product.get("tags"),
        "variants_count": len(product.get("variants") or []),
        "images_count": len(product.get("images") or []),
        "variants": [
            {
                "id": v.get("id"),
                "title": v.get("title"),
                "sku": v.get("sku"),
                "price": v.get("price"),
                "compare_at_price": v.get("compare_at_price"),
                "inventory_quantity": v.get("inventory_quantity"),
                "inventory_item_id": v.get("inventory_item_id"),
                "option1": v.get("option1"),
                "option2": v.get("option2"),
                "option3": v.get("option3"),
            }
            for v in (product.get("variants") or [])
        ],
        "images": [
            {"id": img.get("id"), "src": img.get("src"), "alt": img.get("alt")}
            for img in (product.get("images") or [])
        ],
    }


def _order_summary(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": order.get("id"),
        "name": order.get("name"),
        "email": order.get("email"),
        "created_at": order.get("created_at"),
        "updated_at": order.get("updated_at"),
        "financial_status": order.get("financial_status"),
        "fulfillment_status": order.get("fulfillment_status"),
        "total_price": order.get("total_price"),
        "currency": order.get("currency"),
        "subtotal_price": order.get("subtotal_price"),
        "total_discounts": order.get("total_discounts"),
        "total_tax": order.get("total_tax"),
        "customer": _customer_summary(order.get("customer", {}) or {}),
        "shipping_address": order.get("shipping_address"),
        "line_items": [
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "quantity": item.get("quantity"),
                "price": item.get("price"),
                "sku": item.get("sku"),
                "product_id": item.get("product_id"),
                "variant_id": item.get("variant_id"),
                "vendor": item.get("vendor"),
            }
            for item in (order.get("line_items") or [])
        ],
    }


def _customer_summary(customer: dict[str, Any]) -> dict[str, Any]:
    if not customer:
        return {}
    return {
        "id": customer.get("id"),
        "email": customer.get("email"),
        "first_name": customer.get("first_name"),
        "last_name": customer.get("last_name"),
        "phone": customer.get("phone"),
        "orders_count": customer.get("orders_count"),
        "total_spent": customer.get("total_spent"),
        "state": customer.get("state"),
        "created_at": customer.get("created_at"),
        "updated_at": customer.get("updated_at"),
        "default_address": customer.get("default_address"),
        "addresses": customer.get("addresses"),
    }


# ---------------------------------------------------------------------------
# Tools: Products (6)
# ---------------------------------------------------------------------------

@mcp.tool(description="List Shopify products with optional status filter (active, archived, draft).")
def shopify_list_products(limit: int = 50, status: str = "active") -> dict[str, Any]:
    """List products with pagination and status filter."""
    limit = max(1, min(int(limit), 250))
    result = _shopify_request(
        "GET",
        "/products.json",
        params={"limit": limit, "status": status},
    )
    products = result.get("products", [])
    return {
        "count": len(products),
        "products": [_product_summary(p) for p in products],
    }


@mcp.tool(description="Get full details of a Shopify product by ID, including variants and images.")
def shopify_get_product(product_id: int) -> dict[str, Any]:
    if not product_id:
        raise ValueError("product_id is required")
    result = _shopify_request("GET", f"/products/{product_id}.json")
    product = result.get("product", {})
    return {"product": _product_summary(product)}


@mcp.tool(description="Create a new Shopify product with variants and images.")
def shopify_create_product(
    title: str,
    description_html: str = "",
    vendor: str = "",
    product_type: str = "",
    tags: str = "",
    variants: Optional[list[dict[str, Any]]] = None,
    images: Optional[list[dict[str, str]]] = None,
) -> dict[str, Any]:
    """Create a product.

    Variants: list of {"option1": "...", "price": "19.99", "sku": "ABC", ...}
    Images:   list of {"src": "https://..."} or {"src": "...", "alt": "..."}
    """
    if not title.strip():
        raise ValueError("title is required")

    product_data: dict[str, Any] = {"title": title.strip()}
    if description_html:
        product_data["body_html"] = description_html
    if vendor:
        product_data["vendor"] = vendor
    if product_type:
        product_data["product_type"] = product_type
    if tags:
        product_data["tags"] = tags

    if variants:
        product_data["variants"] = variants
    else:
        product_data["variants"] = [{"price": "0.00"}]

    if images:
        product_data["images"] = images

    result = _shopify_request(
        "POST", "/products.json", data={"product": product_data}
    )
    return {"product": _product_summary(result.get("product", {}))}


@mcp.tool(description="Update fields of an existing Shopify product.")
def shopify_update_product(
    product_id: int,
    title: str = "",
    description_html: str = "",
    vendor: str = "",
    product_type: str = "",
    tags: str = "",
    status: str = "",
) -> dict[str, Any]:
    """Update a product. Only non-empty fields are changed."""
    if not product_id:
        raise ValueError("product_id is required")

    product_data: dict[str, Any] = {"id": product_id}
    if title:
        product_data["title"] = title
    if description_html:
        product_data["body_html"] = description_html
    if vendor:
        product_data["vendor"] = vendor
    if product_type:
        product_data["product_type"] = product_type
    if tags:
        product_data["tags"] = tags
    if status:
        product_data["status"] = status

    if len(product_data) <= 1:
        raise ValueError("At least one field to update must be provided")

    result = _shopify_request(
        "PUT", f"/products/{product_id}.json", data={"product": product_data}
    )
    return {"product": _product_summary(result.get("product", {}))}


@mcp.tool(description="Delete a Shopify product by ID.")
def shopify_delete_product(product_id: int) -> dict[str, Any]:
    if not product_id:
        raise ValueError("product_id is required")
    _shopify_request("DELETE", f"/products/{product_id}.json")
    return {"deleted": True, "product_id": product_id}


@mcp.tool(description="Search Shopify products by title or description text.")
def shopify_search_products(query: str, limit: int = 20) -> dict[str, Any]:
    """Search products matching a text query in title or description."""
    if not query.strip():
        raise ValueError("query is required")
    limit = max(1, min(int(limit), 250))

    # First attempt: use Shopify's built-in title filter ------------------------
    result = _shopify_request(
        "GET",
        "/products.json",
        params={"limit": limit, "title": query.strip()},
    )
    products = result.get("products", [])

    # If the title filter returned nothing, fall back to client-side full-text
    # search across title + body_html -------------------------------------------
    if not products:
        result = _shopify_request(
            "GET",
            "/products.json",
            params={"limit": 250, "status": "active"},
        )
        all_products = result.get("products", [])
        q_lower = query.strip().lower()
        products = [
            p
            for p in all_products
            if q_lower in (p.get("title") or "").lower()
            or q_lower in (p.get("body_html") or "").lower()
        ][:limit]

    return {
        "count": len(products),
        "query": query.strip(),
        "products": [_product_summary(p) for p in products],
    }


# ---------------------------------------------------------------------------
# Tools: Orders (3)
# ---------------------------------------------------------------------------

@mcp.tool(description="List Shopify orders, optionally filtered by status and time window.")
def shopify_list_orders(
    limit: int = 50,
    status: str = "any",
    since_days: int = 7,
) -> dict[str, Any]:
    """List orders.  status: any, open, closed, cancelled."""
    import datetime as _dt

    limit = max(1, min(int(limit), 250))
    since_days = max(1, min(int(since_days), 365))
    created_at_min = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=since_days)
    ).isoformat()

    result = _shopify_request(
        "GET",
        "/orders.json",
        params={
            "limit": limit,
            "status": status,
            "created_at_min": created_at_min,
            "order": "created_at desc",
        },
    )
    orders = result.get("orders", [])
    return {
        "count": len(orders),
        "orders": [_order_summary(o) for o in orders],
    }


@mcp.tool(description="Get full details of a Shopify order — line items, customer, shipping.")
def shopify_get_order(order_id: int) -> dict[str, Any]:
    if not order_id:
        raise ValueError("order_id is required")
    result = _shopify_request("GET", f"/orders/{order_id}.json")
    order = result.get("order", {})
    return {"order": _order_summary(order)}


@mcp.tool(description="Count Shopify orders matching status and time window. Lightweight — ideal for dashboards.")
def shopify_count_orders(
    status: str = "any", since_days: int = 30
) -> dict[str, Any]:
    """Count orders.  status: any, open, closed, cancelled."""
    import datetime as _dt

    since_days = max(1, min(int(since_days), 365))
    created_at_min = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=since_days)
    ).isoformat()

    result = _shopify_request(
        "GET",
        "/orders/count.json",
        params={
            "status": status,
            "created_at_min": created_at_min,
        },
    )
    return {
        "count": result.get("count", 0),
        "status": status,
        "since_days": since_days,
    }


# ---------------------------------------------------------------------------
# Tools: Customers (2)
# ---------------------------------------------------------------------------

@mcp.tool(description="List Shopify customers, newest first.")
def shopify_list_customers(limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(int(limit), 250))
    result = _shopify_request(
        "GET",
        "/customers.json",
        params={"limit": limit, "order": "created_at desc"},
    )
    customers = result.get("customers", [])
    return {
        "count": len(customers),
        "customers": [_customer_summary(c) for c in customers],
    }


@mcp.tool(description="Get full customer details — addresses, lifetime spend, and recent orders.")
def shopify_get_customer(customer_id: int) -> dict[str, Any]:
    if not customer_id:
        raise ValueError("customer_id is required")
    result = _shopify_request("GET", f"/customers/{customer_id}.json")
    customer = result.get("customer", {})

    # Enrich with recent orders -------------------------------------------------
    try:
        orders_result = _shopify_request(
            "GET",
            f"/customers/{customer_id}/orders.json",
            params={"limit": 10, "order": "created_at desc"},
        )
        recent = orders_result.get("orders", [])
    except Exception:
        recent = []

    summary = _customer_summary(customer)
    summary["recent_orders"] = [
        {
            "id": o.get("id"),
            "name": o.get("name"),
            "created_at": o.get("created_at"),
            "total_price": o.get("total_price"),
            "financial_status": o.get("financial_status"),
        }
        for o in recent
    ]
    return {"customer": summary}


# ---------------------------------------------------------------------------
# Tools: Inventory (2)
# ---------------------------------------------------------------------------

@mcp.tool(description="Get inventory levels for all variants of a Shopify product across locations.")
def shopify_get_inventory_levels(product_id: int) -> dict[str, Any]:
    if not product_id:
        raise ValueError("product_id is required")

    # Step 1: get product to discover inventory_item_ids ------------------------
    product_result = _shopify_request("GET", f"/products/{product_id}.json")
    product = product_result.get("product", {})
    variants = product.get("variants", [])

    inventory_item_ids = [
        str(v["inventory_item_id"])
        for v in variants
        if v.get("inventory_item_id")
    ]

    if not inventory_item_ids:
        return {
            "product_id": product_id,
            "product_title": product.get("title"),
            "variants": [],
        }

    # Step 2: query inventory levels for all those items ------------------------
    result = _shopify_request(
        "GET",
        "/inventory_levels.json",
        params={"inventory_item_ids": ",".join(inventory_item_ids)},
    )
    levels = result.get("inventory_levels", [])

    # Build lookup map by inventory_item_id -------------------------------------
    level_map: dict[int, list[dict[str, Any]]] = {}
    for level in levels:
        iid = level.get("inventory_item_id")
        if iid:
            level_map.setdefault(iid, []).append(
                {
                    "location_id": level.get("location_id"),
                    "available": level.get("available"),
                    "updated_at": level.get("updated_at"),
                }
            )

    return {
        "product_id": product_id,
        "product_title": product.get("title"),
        "variants": [
            {
                "variant_id": v.get("id"),
                "title": v.get("title"),
                "sku": v.get("sku"),
                "inventory_item_id": v.get("inventory_item_id"),
                "inventory_levels": level_map.get(v.get("inventory_item_id"), []),
            }
            for v in variants
        ],
    }


@mcp.tool(description="Set available inventory quantity for a specific inventory item at a location.")
def shopify_update_inventory(
    inventory_item_id: int,
    location_id: int,
    available: int,
) -> dict[str, Any]:
    """Set the available quantity for one inventory item at one location."""
    if not inventory_item_id or not location_id:
        raise ValueError("inventory_item_id and location_id are required")

    result = _shopify_request(
        "POST",
        "/inventory_levels/set.json",
        data={
            "inventory_item_id": inventory_item_id,
            "location_id": location_id,
            "available": int(available),
        },
    )
    level = result.get("inventory_level", {})
    return {
        "inventory_item_id": level.get("inventory_item_id"),
        "location_id": level.get("location_id"),
        "available": level.get("available"),
        "updated_at": level.get("updated_at"),
    }


# ---------------------------------------------------------------------------
# Tools: Store (1)
# ---------------------------------------------------------------------------

@mcp.tool(description="Get Shopify store information — name, currency, timezone, plan, domain.")
def shopify_get_store_info() -> dict[str, Any]:
    result = _shopify_request("GET", "/shop.json")
    shop = result.get("shop", {})
    return {
        "id": shop.get("id"),
        "name": shop.get("name"),
        "email": shop.get("email"),
        "domain": shop.get("domain"),
        "myshopify_domain": shop.get("myshopify_domain"),
        "currency": shop.get("currency"),
        "money_format": shop.get("money_with_currency_format"),
        "timezone": shop.get("iana_timezone"),
        "plan_name": shop.get("plan_name"),
        "plan_display_name": shop.get("plan_display_name"),
        "shop_owner": shop.get("shop_owner"),
        "created_at": shop.get("created_at"),
        "updated_at": shop.get("updated_at"),
        "country": shop.get("country"),
        "country_code": shop.get("country_code"),
        "province": shop.get("province"),
        "city": shop.get("city"),
        "address1": shop.get("address1"),
        "zip": shop.get("zip"),
        "phone": shop.get("phone"),
        "customer_email": shop.get("customer_email"),
        "has_storefront": shop.get("has_storefront"),
        "eligible_for_payments": shop.get("eligible_for_payments"),
        "multi_location_enabled": shop.get("multi_location_enabled"),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
