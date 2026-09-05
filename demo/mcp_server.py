#!/usr/bin/env python3
"""
LexCart MCP server: exposes the same agent-readable commerce surface that
demo/ai_buyer.py drives over plain HTTP, as MCP tools — so any MCP client
(Claude Desktop, Claude Code, etc.) can discover, price, and purchase a
LexCart consultation directly from a conversation.

This is a thin wrapper: every tool below is one httpx call to the public
/api/agent/v1/* API with the same X-Agent-Key auth demo/ai_buyer.py uses.
Nothing here bypasses the merchant's guardrails, pricing, or audit trail —
an MCP purchase is indistinguishable, server-side, from any other AI-buyer
purchase, and shows up in the merchant's audit trail the same way.

Setup (Claude Desktop claude_desktop_config.json, or any MCP client):
    {
      "mcpServers": {
        "lexcart": {
          "command": "python",
          "args": ["/absolute/path/to/demo/mcp_server.py"],
          "env": {
            "LEXCART_BASE_URL": "http://localhost:8000",
            "LEXCART_AGENT_KEY": "lexcart_agent_demo_a7f3e9c1"
          }
        }
      }
    }

Requires: pip install mcp (in addition to httpx, already a server dep).
"""

import os
from typing import Optional

import httpx
from mcp.server.mcpserver import MCPServer

BASE_URL = os.environ.get("LEXCART_BASE_URL", "http://localhost:8000")
AGENT_KEY = os.environ.get("LEXCART_AGENT_KEY", "lexcart_agent_demo_a7f3e9c1")

mcp = MCPServer("lexcart")


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE_URL, headers={"X-Agent-Key": AGENT_KEY}, timeout=30)


def _score(svc: dict, need: str, budget: Optional[int]) -> float:
    """Same cheap relevance heuristic as demo/ai_buyer.py: keyword overlap
    with category+description, weighted by rating, filtered by budget."""
    price = svc["offers"]["price"]
    if budget is not None and price > budget:
        return -1.0
    text = (svc["category"] + " " + svc["description"]).lower()
    words = [w for w in need.lower().split() if len(w) > 3]
    overlap = sum(1 for w in words if w in text)
    return overlap * 10 + svc["provider"]["aggregateRating"]["ratingValue"]


@mcp.tool()
def discover_services(need: str, budget_inr: Optional[int] = None) -> dict:
    """Find LexCart legal-consultation services matching a described need
    and an optional budget cap, ranked best-first. Always call this before
    get_quote/place_order — it returns the serviceId those tools need."""
    with _client() as c:
        catalog = c.get("/.well-known/agent-catalog.json").raise_for_status().json()
    ranked = sorted(
        catalog["services"], key=lambda s: _score(s, need, budget_inr), reverse=True
    )
    return {
        "merchant": catalog["merchant"]["name"],
        "bounds": catalog["bounds"],
        "matches": [
            {
                "serviceId": s["serviceId"],
                "name": s["name"],
                "category": s["category"],
                "priceInr": s["offers"]["price"],
                "rating": s["provider"]["aggregateRating"]["ratingValue"],
                "fitsInBudget": budget_inr is None or s["offers"]["price"] <= budget_inr,
            }
            for s in ranked
        ],
        "addons": catalog["addons"],
    }


@mcp.tool()
def get_quote(service_id: str, addon_ids: Optional[list[str]] = None) -> dict:
    """Firm, server-priced quote for a service plus optional add-ons.
    No money moves yet — this is read-only."""
    with _client() as c:
        r = c.post("/api/agent/v1/quote", json={"serviceId": service_id, "addonIds": addon_ids or []})
    if r.status_code != 200:
        return {"error": r.json().get("message", "quote failed")}
    return r.json()


@mcp.tool()
def place_order(service_id: str, addon_ids: Optional[list[str]] = None, buyer_reference: str = "") -> dict:
    """Place an order for a service. The merchant checks it against
    per-order and daily spend caps BEFORE creating it — if it's refused,
    the response explains exactly why (and by how much), so you can adapt
    (drop add-ons, pick a cheaper service) rather than just fail. Pass a
    stable buyer_reference to make retries safe: the same reference
    returns the existing order instead of duplicating it."""
    with _client() as c:
        r = c.post(
            "/api/agent/v1/orders",
            json={"serviceId": service_id, "addonIds": addon_ids or [], "buyerReference": buyer_reference},
        )
    if r.status_code != 200:
        return {"refused": True, "reason": r.json().get("message", "order refused")}
    return r.json()


@mcp.tool()
def pay_order(order_id: str) -> dict:
    """Complete payment for an order (mock gateway only — this call 403s
    if the merchant has real Razorpay test keys configured, in which case
    a human pays via the payment link returned by place_order)."""
    with _client() as c:
        r = c.post(f"/api/agent/v1/orders/{order_id}/pay-mock")
    if r.status_code != 200:
        return {"error": r.json().get("message", "payment failed")}
    return r.json()


@mcp.tool()
def order_status(order_id: str) -> dict:
    """Check an order's current status (created / paid / failed / refunded)."""
    with _client() as c:
        r = c.get(f"/api/agent/v1/orders/{order_id}")
    if r.status_code != 200:
        return {"error": r.json().get("message", "not found")}
    return r.json()


if __name__ == "__main__":
    mcp.run(transport="stdio")
