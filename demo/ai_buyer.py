#!/usr/bin/env python3
"""
Demo AI buyer: an external agent transacting with LexCart end to end.

    python demo/ai_buyer.py --need "help with a property dispute" --budget 4000

Flow: discover the open catalog at /.well-known/agent-catalog.json, score
services against the stated need/budget, get a firm quote, place the order,
and complete payment (mock gateway) — printing its reasoning at each step.
Pass --over-budget to watch the merchant's guardrails refuse an oversized
order, then see the buyer adapt gracefully.

Only stdlib + httpx (already a server dependency).
"""

import argparse
import sys
import uuid

import httpx

DEFAULT_BASE = "http://localhost:8000"
DEFAULT_KEY = "lexcart_agent_demo_a7f3e9c1"  # seeded by init_db for demos


def say(step: str, msg: str) -> None:
    print(f"[{step:>9}] {msg}")


def score_service(svc: dict, need: str, budget: int | None) -> float:
    """Cheap relevance score: keyword overlap with category+description,
    weighted by rating, filtered by budget."""
    price = svc["offers"]["price"]
    if budget is not None and price > budget:
        return -1.0
    text = (svc["category"] + " " + svc["description"]).lower()
    words = [w for w in need.lower().split() if len(w) > 3]
    overlap = sum(1 for w in words if w in text)
    rating = svc["provider"]["aggregateRating"]["ratingValue"]
    return overlap * 10 + rating


def main() -> int:
    ap = argparse.ArgumentParser(description="LexCart demo AI buyer")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--key", default=DEFAULT_KEY)
    ap.add_argument("--need", default="help with a property dispute over ancestral land")
    ap.add_argument("--budget", type=int, default=4000)
    ap.add_argument("--addons", nargs="*", default=["addon-doc-review"])
    ap.add_argument(
        "--over-budget", action="store_true",
        help="deliberately order the priciest service repeatedly to trip the merchant's bounds",
    )
    ap.add_argument(
        "--buyer-ref", default=None,
        help="idempotency key for the purchase (see /api/agent/v1/orders' buyerReference). "
        "Defaults to a fresh random value each run; pass the same value twice to see the "
        "second run replay the first order instead of placing a duplicate.",
    )
    args = ap.parse_args()
    buyer_ref = args.buyer_ref or f"demo-ai-buyer-{uuid.uuid4().hex[:8]}"

    client = httpx.Client(base_url=args.base, headers={"X-Agent-Key": args.key}, timeout=30)

    say("discover", f"GET /.well-known/agent-catalog.json from {args.base}")
    catalog = client.get("/.well-known/agent-catalog.json").raise_for_status().json()
    merchant = catalog["merchant"]["name"]
    services = catalog["services"]
    say("discover", f"merchant '{merchant}' sells {len(services)} services; "
        f"bounds: max order ₹{catalog['bounds']['maxOrderInr']}")

    if args.over_budget:
        # Priciest service first: if it's over the per-order cap on its
        # own, the merchant refuses on attempt one — instant, and doesn't
        # spend down the daily cap. Only hammers repeatedly (the old
        # behaviour) if nothing in the catalog trips the per-order cap by
        # itself, to demonstrate the rolling daily-cap guardrail instead.
        target = max(services, key=lambda s: s["offers"]["price"])
        say("plan", f"(--over-budget) targeting the priciest service "
            f"'{target['name']}' at ₹{target['offers']['price']}")
        refusals = 0
        for attempt in range(1, 30):
            resp = client.post(
                "/api/agent/v1/orders",
                json={"serviceId": target["serviceId"], "buyerReference": f"stress-{attempt}"},
            )
            if resp.status_code == 400:
                say("refused", f"attempt {attempt}: merchant blocked the order — {resp.json()['message']}")
                refusals += 1
                break
            order = resp.raise_for_status().json()
            pay = client.post(f"/api/agent/v1/orders/{order['orderId']}/pay-mock")
            say("paid", f"attempt {attempt}: order {order['orderId']} for ₹{order['totalInr']} "
                f"-> {pay.json().get('status', pay.status_code)}")
        if refusals:
            say("adapt", "guardrail confirmed working; falling back to a bounded, in-budget purchase")
        # fall through to the normal flow

    say("plan", f"need: {args.need!r}, budget: ₹{args.budget}")
    ranked = sorted(services, key=lambda s: score_service(s, args.need, args.budget), reverse=True)
    best = ranked[0]
    if score_service(best, args.need, args.budget) < 0:
        say("plan", "nothing fits the budget; aborting politely")
        return 1
    say("plan", f"chose '{best['name']}' ({best['category']}, ₹{best['offers']['price']}, "
        f"rating {best['provider']['aggregateRating']['ratingValue']}) "
        f"over {len(ranked) - 1} alternatives")

    quote = client.post(
        "/api/agent/v1/quote",
        json={"serviceId": best["serviceId"], "addonIds": args.addons},
    ).raise_for_status().json()
    lines = "; ".join(f"{li['label']} ₹{li['amountInr']}" for li in quote["lineItems"])
    say("quote", f"firm quote: {lines} => total ₹{quote['totalInr']}")

    if args.budget is not None and quote["totalInr"] > args.budget * 1.25:
        say("quote", "total with addons is >125% of budget; dropping addons")
        args.addons = []
        quote = client.post(
            "/api/agent/v1/quote", json={"serviceId": best["serviceId"], "addonIds": []}
        ).raise_for_status().json()
        say("quote", f"re-quoted without addons: ₹{quote['totalInr']}")

    resp = client.post(
        "/api/agent/v1/orders",
        json={"serviceId": best["serviceId"], "addonIds": args.addons,
              "buyerReference": buyer_ref},
    )
    if resp.status_code == 400:
        say("refused", f"merchant declined: {resp.json()['message']}")
        return 1
    order = resp.raise_for_status().json()
    if order.get("idempotent"):
        say("order", f"buyerReference {buyer_ref!r} already has order {order['orderId']} "
            f"(status: {order['status']}) — replaying it instead of placing a duplicate")
    else:
        say("order", f"order {order['orderId']} created (razorpay {order['payment']['razorpayOrderId']}), "
            f"total ₹{order['totalInr']}")

    if order["status"] != "created":
        say("done", f"order {order['orderId']} is already {order['status']} — nothing more to do")
        return 0

    if order["payment"]["mode"] == "mock":
        pay = client.post(f"/api/agent/v1/orders/{order['orderId']}/pay-mock").raise_for_status().json()
        say("pay", f"mock gateway payment {pay['razorpayPaymentId']} verified; "
            f"booking {pay['bookingId']} confirmed")
    else:
        say("pay", f"real test mode: complete payment at {order['payment']['url']}")

    status = client.get(f"/api/agent/v1/orders/{order['orderId']}").raise_for_status().json()
    say("done", f"final order status: {status['status']} — end-to-end agent purchase complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
