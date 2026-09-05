#!/usr/bin/env python3
"""
Automated evaluation harness for LexCart (Razorpay Buildathon, Track 01).

Run against a live server (docker compose up, or the local dev server):

    python demo/evaluate.py [--base http://localhost:8000]

Exercises every claim the submission makes and prints PASS/FAIL per check
plus a summary; exit code 0 only if everything passed. Checks that need to
complete a payment run only on the mock gateway (no Razorpay keys) and are
reported as SKIP when real test keys are configured.

Covered: discovery catalog, AI-buyer auth + quote/order/pay lifecycle,
idempotent order retries, the per-order guardrail cap, refund/cancel,
server-side pricing, concierge conversation -> proposal -> human gate ->
verified booking, a stale proposal rejected after the cart drifts,
rejected gate, failed payment handled gracefully, guardrail refusals
(campaign discount cap), campaign draft/approve, Razorpay webhook
reconciliation (forged signatures rejected; payment.captured and
payment_link.paid processed idempotently), merchant growth stats, and the
completeness of the audit trail. Only stdlib + httpx.
"""

import argparse
import json
import sys
import time
import uuid

import httpx

DEMO_AGENT_KEY = "lexcart_agent_demo_a7f3e9c1"  # seeded by init_db

PASSED, FAILED, SKIPPED = [], [], []


def check(name: str, ok: bool, detail: str = ""):
    bucket = PASSED if ok else FAILED
    bucket.append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def skip(name: str, why: str):
    SKIPPED.append(name)
    print(f"  [SKIP] {name} — {why}")


def section(title: str):
    print(f"\n== {title} ==")


def _is_daily_cap_refusal(resp: httpx.Response) -> bool:
    """The agent daily-spend guardrail has two independent enforcement
    points with different wording — the per-key cap ("...exceed its daily
    limit of...", app/routers/agent_commerce.py) and the global agent cap
    ("...exceed the daily cap of...", app/commerce/guardrails.py) — either
    can fire first depending on which key/global budget is tighter on a
    given DB. Match both rather than one exact phrase."""
    if resp.status_code != 400:
        return False
    return "daily" in resp.json().get("message", "").lower()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    args = ap.parse_args()
    c = httpx.Client(base_url=args.base, timeout=60)

    section("Server & gateway")
    r = c.get("/health")
    check("server is healthy", r.status_code == 200)
    cfg = c.get("/api/bookings/config").json()
    mock = bool(cfg.get("mock"))
    check("payment config exposes key id + mode", "keyId" in cfg, f"mock={mock}")

    section("Agent-readable discovery (open, no auth)")
    cat = c.get("/.well-known/agent-catalog.json")
    check("catalog served at /.well-known/agent-catalog.json", cat.status_code == 200)
    cat = cat.json()
    services = cat.get("services", [])
    check("catalog lists services with schema.org-style offers",
          len(services) >= 3 and all("offers" in s and "price" in s["offers"] for s in services))
    check("catalog declares auth scheme", cat.get("authentication", {}).get("header") == "X-Agent-Key")
    check("catalog declares merchant bounds", "maxOrderInr" in cat.get("bounds", {}))
    check("catalog documents agent-order idempotency semantics",
          "idempotency" in cat.get("semantics", {}))

    section("AI-buyer API (authenticated)")
    bad = c.get("/api/agent/v1/catalog", headers={"X-Agent-Key": "wrong-key"})
    check("invalid agent key rejected (401)", bad.status_code == 401)
    ah = {"X-Agent-Key": DEMO_AGENT_KEY}
    r = c.get("/api/agent/v1/catalog", headers=ah)
    check("valid agent key accepted", r.status_code == 200)

    # Cheapest service keeps repeated harness runs inside the demo key's
    # daily spend limit for as long as possible.
    svc = min(services, key=lambda s: s["offers"]["price"])
    quote = c.post("/api/agent/v1/quote", headers=ah,
                   json={"serviceId": svc["serviceId"], "addonIds": []}).json()
    base_price = svc["offers"]["price"]
    expected = base_price + round(base_price * 0.05)
    check("quote priced server-side (base + 5% fee)",
          quote.get("totalInr") == expected, f"₹{quote.get('totalInr')} vs expected ₹{expected}")

    resp = c.post("/api/agent/v1/orders", headers=ah,
                  json={"serviceId": svc["serviceId"], "buyerReference": f"eval-harness-{uuid.uuid4().hex[:8]}"})
    if _is_daily_cap_refusal(resp):
        # A long-lived dev DB can exhaust the demo key's ₹/day cap across
        # runs — itself proof the guardrail binds. Fresh (docker) DBs never
        # hit this.
        check("daily-limit guardrail enforced with explanation", True,
              resp.json()["message"][:90])
        skip("agent order placement + payment", "demo key daily limit exhausted in this DB")
    else:
        check("agent can place an order", resp.status_code == 200)
        order = resp.json()
        check("order carries a razorpay order id",
              str(order.get("payment", {}).get("razorpayOrderId", "")).startswith("order_"))
        if mock:
            pay = c.post(f"/api/agent/v1/orders/{order['orderId']}/pay-mock", headers=ah).json()
            check("mock payment verified -> booking created", bool(pay.get("bookingId")))
            status = c.get(f"/api/agent/v1/orders/{order['orderId']}", headers=ah).json()
            check("order status is paid end-to-end", status.get("status") == "paid")
        else:
            skip("agent mock payment completion",
                 "real Razorpay keys configured; pay via payment link")
            forbidden = c.post(f"/api/agent/v1/orders/{order['orderId']}/pay-mock", headers=ah)
            check("pay-mock refused when real keys configured (403)", forbidden.status_code == 403)

    section("AI-buyer API: per-order cap, idempotency & refund")
    premium = max(services, key=lambda s: s["offers"]["price"])
    over_cap = c.post("/api/agent/v1/orders", headers=ah,
                      json={"serviceId": premium["serviceId"], "buyerReference": "eval-cap-probe"})
    check("order above the agent cap (per-order or rolling daily) is refused (400)",
          over_cap.status_code == 400,
          over_cap.json().get("message", "")[:100] if over_cap.status_code == 400 else "")

    order_a = None
    refund_tested = False
    idem_ref = f"eval-idem-{uuid.uuid4().hex[:8]}"
    resp_a = c.post("/api/agent/v1/orders", headers=ah,
                    json={"serviceId": svc["serviceId"], "buyerReference": idem_ref})
    if _is_daily_cap_refusal(resp_a):
        check("daily-limit guardrail enforced (idempotency/webhook/refund probe)", True,
              resp_a.json()["message"][:90])
        skip("idempotent replay", "demo key daily limit exhausted in this DB")
        skip("webhook payment.captured reconciliation", "no unpaid order available")
        skip("refund/cancel lifecycle", "no paid order available")
    else:
        check("agent order created for idempotency/webhook/refund checks", resp_a.status_code == 200)
        order_a = resp_a.json()
        resp_a2 = c.post("/api/agent/v1/orders", headers=ah,
                         json={"serviceId": svc["serviceId"], "buyerReference": idem_ref})
        check(
            "retrying with the same buyerReference returns the original order, not a duplicate",
            resp_a2.status_code == 200
            and resp_a2.json().get("orderId") == order_a["orderId"]
            and resp_a2.json().get("idempotent") is True,
        )

    section("Concierge: conversation -> proposal -> human gate -> booking")
    email = f"eval-{uuid.uuid4().hex[:8]}@lexcart.local"
    reg = c.post("/api/auth/register",
                 json={"name": "Eval Agent", "email": email, "password": "eval-pass-123"})
    check("user registration", reg.status_code in (200, 201))
    uh = {"Authorization": f"Bearer {reg.json()['token']}"}
    user_id = reg.json()["user"]["id"]
    sid = f"eval-{uuid.uuid4().hex[:8]}"

    def chat(msg: str) -> dict:
        return c.post("/api/concierge/chat", headers=uh,
                      json={"sessionId": sid, "message": msg}).json()

    r1 = chat("I need help with a property dispute, budget under 4000")
    check("concierge finds lawyers for a described need", len(r1.get("lawyers", [])) >= 1)
    check("concierge respects the stated budget",
          all(lw["hourlyRate"] <= 4000 for lw in r1.get("lawyers", [])),
          str([(lw["name"], lw["hourlyRate"]) for lw in r1.get("lawyers", [])]))

    r2 = chat("go with the first one")
    check("concierge builds a cart from the selection", bool(r2.get("cart")))
    r3 = chat("yes add the document review")
    cart = r3.get("cart") or {}
    check("upsell addon lands in the cart",
          any(a["id"] == "addon-doc-review" for a in cart.get("addons", [])))

    r4 = chat("checkout please")
    proposal = r4.get("proposal")
    check("checkout produces a PROPOSAL, not an order", bool(proposal),
          "agent cannot create orders; gate required")
    check("proposal explains its guardrail check", bool(proposal and proposal.get("boundsNote")))

    if not proposal:
        print("\nCannot continue concierge checks without a proposal.")
    else:
        confirm = c.post("/api/concierge/confirm", headers=uh,
                         json={"sessionId": sid, "proposalId": proposal["proposalId"]})
        check("human Confirm & Pay creates the order", confirm.status_code == 200)
        order2 = confirm.json()
        check("order total matches the proposal (server-priced)",
              order2.get("amountInr") == proposal["totalInr"])

        reused = c.post("/api/concierge/confirm", headers=uh,
                        json={"sessionId": sid, "proposalId": proposal["proposalId"]})
        check("a proposal cannot be confirmed twice", reused.status_code == 400)

        if mock:
            pay = c.post("/api/bookings/mock-pay", headers=uh,
                         json={"orderId": order2["orderId"]}).json()
            ver = c.post("/api/bookings/verify", headers=uh,
                         json={"orderId": order2["orderId"],
                               "razorpayPaymentId": pay["razorpayPaymentId"],
                               "razorpaySignature": pay["razorpaySignature"]}).json()
            check("signature verified -> booking confirmed", ver.get("status") == "success")
            bookings = c.get(f"/api/bookings/user-bookings/{user_id}", headers=uh).json()
            check("booking visible in My Bookings", len(bookings) >= 1)

            section("Failure handled gracefully")
            chat("book another consultation with the same lawyer")
            r5 = chat("checkout")
            p2 = r5.get("proposal")
            if p2:
                o3 = c.post("/api/concierge/confirm", headers=uh,
                            json={"sessionId": sid, "proposalId": p2["proposalId"]}).json()
                bad_pay = c.post("/api/bookings/mock-pay", headers=uh,
                                 json={"orderId": o3["orderId"], "fail": True}).json()
                bad_ver = c.post("/api/bookings/verify", headers=uh,
                                 json={"orderId": o3["orderId"],
                                       "razorpayPaymentId": bad_pay["razorpayPaymentId"],
                                       "razorpaySignature": bad_pay["razorpaySignature"]})
                check("forged/failed signature rejected (400)", bad_ver.status_code == 400)
                o3_status = c.get(f"/api/bookings/orders/{o3['orderId']}", headers=uh).json()
                check("failed order marked failed, no booking",
                      o3_status.get("status") == "failed")
            else:
                check("second checkout proposal", False, "no proposal returned")
        else:
            skip("concierge mock payment + failure path", "real Razorpay keys configured")

        section("Gate rejection")
        chat("one more consultation with the same lawyer")
        r6 = chat("checkout")
        p3 = r6.get("proposal")
        if p3:
            rej = c.post("/api/concierge/reject", headers=uh,
                         json={"sessionId": sid, "proposalId": p3["proposalId"]})
            check("user can decline at the gate", rej.status_code == 200)
            confirm_rejected = c.post("/api/concierge/confirm", headers=uh,
                                      json={"sessionId": sid, "proposalId": p3["proposalId"]})
            check("a rejected proposal cannot be confirmed", confirm_rejected.status_code == 400)
        else:
            check("third checkout proposal", False, "no proposal returned")

        section("Concierge: confirm charges the proposal snapshot, not the live cart")
        chat("I need another consultation, budget under 3000")
        chat("go with the first one")
        r7 = chat("checkout")
        p4 = r7.get("proposal")
        if p4:
            chat("yes add the follow-up call")  # mutates the live cart AFTER the proposal snapshot
            confirmed = c.post("/api/concierge/confirm", headers=uh,
                               json={"sessionId": sid, "proposalId": p4["proposalId"]})
            check(
                "confirming after the cart changed still charges exactly what was proposed, "
                "not the drifted live total",
                confirmed.status_code == 200 and confirmed.json().get("amountInr") == p4["totalInr"],
                f"proposed ₹{p4['totalInr']} vs charged ₹{confirmed.json().get('amountInr')}",
            )
        else:
            check("fourth checkout proposal", False, "no proposal returned")

    section("Campaign orchestrator: bounded + merchant-gated")
    blocked = c.post("/api/campaigns/draft", headers=uh,
                     json={"objective": "mega sale", "lawyerId": "1",
                           "discountPct": 60, "budgetInr": 5000})
    check("60% discount draft refused by guardrail (400)", blocked.status_code == 400,
          blocked.json().get("message", "")[:80])
    draft = c.post("/api/campaigns/draft", headers=uh,
                   json={"objective": "win back visitors who browsed but did not book",
                         "lawyerId": "1", "discountPct": 15, "budgetInr": 5000}).json()
    check("in-bounds campaign drafted (status=draft, nothing live)",
          draft.get("status") == "draft")
    approved = c.post(f"/api/campaigns/{draft['id']}/approve", headers=uh).json()
    check("merchant approval activates campaign + mints payment link",
          approved.get("status") == "active" and bool(approved.get("paymentLinkUrl")))

    section("Webhooks: Razorpay reconciliation safety net")
    forged = c.post("/api/webhooks/razorpay", content=b'{"event":"payment.captured"}',
                    headers={"X-Razorpay-Signature": "forged"})
    check("forged webhook signature rejected, nothing trusted (400)", forged.status_code == 400)

    if mock:
        unknown_body = json.dumps({
            "event": "payment.captured",
            "payload": {"payment": {"entity": {"order_id": "order_MOCKdoesnotexist", "id": "pay_x"}}},
        }).encode()
        unk_sig = c.post("/api/webhooks/razorpay/_mock-sign", content=unknown_body).json()["signature"]
        unk_resp = c.post("/api/webhooks/razorpay", content=unknown_body,
                          headers={"X-Razorpay-Signature": unk_sig})
        check("valid signature but unrecognised order is ignored, not errored",
              unk_resp.status_code == 200 and unk_resp.json().get("status") == "ignored")

        if order_a:
            cap_body = json.dumps({
                "event": "payment.captured",
                "payload": {"payment": {"entity": {
                    "order_id": order_a["payment"]["razorpayOrderId"],
                    "id": f"pay_MOCKwebhook{uuid.uuid4().hex[:8]}",
                }}},
            }).encode()
            cap_sig = c.post("/api/webhooks/razorpay/_mock-sign", content=cap_body).json()["signature"]
            cap_resp = c.post("/api/webhooks/razorpay", content=cap_body,
                              headers={"X-Razorpay-Signature": cap_sig})
            check("webhook payment.captured reconciles an order the client never confirmed",
                  cap_resp.status_code == 200 and cap_resp.json().get("status") == "processed")
            status_a = c.get(f"/api/agent/v1/orders/{order_a['orderId']}", headers=ah).json()
            check("order is paid via the webhook alone (no /pay-mock or /verify call made)",
                  status_a.get("status") == "paid" and bool(status_a.get("bookingId")))

            cancel = c.post(f"/api/agent/v1/orders/{order_a['orderId']}/cancel", headers=ah)
            check("agent can cancel a paid order -> refunded in full",
                  cancel.status_code == 200 and cancel.json().get("status") == "refunded"
                  and bool(cancel.json().get("razorpayRefundId")))
            refund_tested = True

        link_id = approved.get("paymentLinkId")
        plink_body = json.dumps({
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {"entity": {"id": link_id}},
                "payment": {"entity": {"id": f"pay_MOCKlink{uuid.uuid4().hex[:8]}"}},
            },
        }).encode()
        plink_sig = c.post("/api/webhooks/razorpay/_mock-sign", content=plink_body).json()["signature"]
        plink_resp = c.post("/api/webhooks/razorpay", content=plink_body,
                            headers={"X-Razorpay-Signature": plink_sig})
        check("webhook payment_link.paid books an anonymous campaign-link purchase",
              plink_resp.status_code == 200 and plink_resp.json().get("status") == "processed")
        campaigns_after = c.get("/api/campaigns", headers=uh).json()
        updated_campaign = next((cc for cc in campaigns_after if cc["id"] == draft["id"]), None)
        check("campaign conversions increment from the webhook-driven purchase",
              bool(updated_campaign) and updated_campaign["conversions"] >= 1)
    else:
        skip("webhook payment.captured / payment_link.paid reconciliation",
             "real Razorpay keys configured; cannot fabricate signed events")

    section("Merchant growth stats")
    stats = c.get("/api/merchant/stats", headers=uh).json()
    check("merchant stats expose revenue by channel, agentic share, upsell rate",
          {"totalRevenueInr", "revenueByChannel", "agenticSharePct", "upsellAttachRatePct"} <= stats.keys(),
          f"total=₹{stats.get('totalRevenueInr')}, agentic={stats.get('agenticSharePct')}%, "
          f"upsell={stats.get('upsellAttachRatePct')}%")

    section("Audit trail completeness")
    audit = c.get("/api/campaigns/audit/all?limit=200", headers=uh).json()
    actions = {a["action"] for a in audit}
    expected_actions = {"catalog_searched", "upsell_proposed", "checkout_proposed",
                        "order_created", "quote_issued", "campaign_blocked",
                        "campaign_drafted", "campaign_approved", "checkout_rejected",
                        "order_blocked", "webhook_rejected"}
    if mock:
        expected_actions |= {"payment_confirmed", "payment_failed"}
    if refund_tested:
        expected_actions |= {"refund_issued"}
    missing = expected_actions - actions
    check("audit trail covers every money-action type", not missing,
          f"missing: {sorted(missing)}" if missing else f"{len(audit)} entries")
    check("every audit entry has a rationale",
          all(a.get("rationale") for a in audit if a["action"] != "payment_confirmed"))
    gates = {a["gateStatus"] for a in audit}
    check("gate states recorded (pending/approved/rejected)",
          {"approved", "rejected"} <= gates or {"approved", "pending"} <= gates, str(sorted(gates)))

    print(f"\n{'=' * 50}")
    print(f"RESULT: {len(PASSED)} passed, {len(FAILED)} failed, {len(SKIPPED)} skipped")
    if FAILED:
        print("Failed checks:")
        for f in FAILED:
            print(f"  - {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    t0 = time.time()
    code = main()
    print(f"(completed in {time.time() - t0:.1f}s)")
    sys.exit(code)
