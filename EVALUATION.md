# EVALUATION.md — rubric map and evidence

This maps every claim in the README to the code that implements it and a
command that proves it, then embeds real transcripts from runs of those
commands. Read `AGENTS.md` first if you haven't already — it explains how
to get a running instance.

## The track bar: explainable, bounded, gated

Razorpay's Track 01 bar is: *"Every money action explainable, bounded and
gated. Show the audit trail and one failure handled gracefully."*

| Bar | Implementation | Verify |
|---|---|---|
| **Explainable** | `commerce/audit.py:log_action` writes one `agent_actions` row per money-touching step, with a plain-language `rationale`. Called from every code path below. | `GET /api/campaigns/audit/all` (merchant) or `GET /api/concierge/audit` (own trail). See "Audit trail completeness" in the transcript below. |
| **Bounded** | `commerce/guardrails.py:check_order_bounds` (per-order cap ₹25,000, rolling 24h agent-spend cap) and `check_campaign_bounds` (discount ≤30%, budget cap), both checked **before** `services/razorpay_gateway.py` is ever called — see `commerce/orders.py:create_order` (bounds check at the top, gateway call after). A blocked order is still audited (`order_blocked`, `campaign_blocked`) with the exact rule and numbers. | `demo/ai_buyer.py --over-budget` (transcript below); or `POST /api/campaigns/draft` with `discountPct: 60`. |
| **Gated** | The concierge can only write `checkout_proposed` with `gate_status=pending` (`commerce/concierge.py:361`); only `routers/concierge.py:confirm` (a human's Confirm & Pay click) or a signature-verified webhook event can create/settle an order. Campaigns need explicit merchant approval (`routers/campaigns.py:approve_campaign`) before a discount is live. | Try to `POST /api/concierge/confirm` for a proposal that isn't yours or isn't pending — 400. |
| **One failure handled gracefully** | A bad/forged payment signature marks the order `failed`, audits it, and tells the user "nothing was booked" (`commerce/orders.py:confirm_payment`, the `not pre_verified and not gateway.verify_payment_signature(...)` branch) — never a 500 or a silent loss. | "Simulate failed payment" button in the UI, or the "Failure handled gracefully" section of the transcript below. |

## Track-direction → code → proof

| Direction | Code | Verify |
|---|---|---|
| **Conversational in-app checkout** | `commerce/concierge.py` (hybrid: LLM for language, deterministic code for money) + `routers/concierge.py` | Concierge tab in the UI, or the "Concierge" sections of the transcript |
| **Agent-readable catalog** | `routers/agent_commerce.py:92` (`/.well-known/agent-catalog.json`, open) + `/api/agent/v1/*` (authenticated) | `curl http://localhost:8000/.well-known/agent-catalog.json`; `demo/ai_buyer.py`; `demo/mcp_server.py` |
| **Upsell & cross-sell agent** | `commerce/concierge.py` (`_relevant_addons`, logged as `upsell_proposed`) | "upsell addon lands in the cart" check |
| **Campaign orchestrator** | `routers/campaigns.py` (draft → merchant approve → payment link → conversion tracking) | "Campaign orchestrator" section of the transcript |

## What's new since the first submission pass

Four things a Razorpay-focused reviewer would specifically probe were added on top of the original four-direction build:

1. **Webhooks** (`routers/webhooks.py`) — the client-side `/verify` callback can be lost (browser closed, network drop) even though the gateway captured payment. `payment.captured` reconciles that idempotently; `payment_link.paid` is not a safety net but the *only* place a campaign-link purchase becomes an order (that flow has no in-app cart at all). Every event is HMAC-verified over the raw body first — a forged signature is rejected and audited (`webhook_rejected`) before anything else runs.
2. **Idempotency** (`orders.buyer_reference`, `agent_commerce.py:216`) — a retried `POST /api/agent/v1/orders` with the same `buyerReference` returns the original order instead of double-ordering. Documented in the catalog's `semantics.idempotency` field.
3. **Refunds** (`orders.py:refund_order`, `agent_commerce.py:agent_cancel_order`) — `POST /api/agent/v1/orders/{id}/cancel` refunds a paid order in full and rolls back any campaign spend/conversion it counted against. There's also an automatic safety-net refund in `confirm_payment` if a booking fails to create *after* payment is captured, so money is never kept for an undelivered service.
4. **Revenue/growth visibility** (`routers/merchant.py:merchant_stats`) — revenue by channel, agentic-channel share, upsell attach rate, campaign ROI, computed straight from `orders`/`agent_actions` (no separate analytics pipeline to drift). Surfaced as a panel in the Merchant dashboard.

Two correctness fixes worth flagging because they're the kind of thing a careful reviewer looks for:

- **The confirm gate now prices from the proposal's own snapshot, not the live cart** (`routers/concierge.py:confirm`, `commerce/concierge.py:361`). Originally `/confirm` re-priced from whatever the session's cart contained *at confirm time*, so if a user kept chatting after "checkout" but before clicking Confirm & Pay, they could be charged a different amount than what was on screen. Now `checkout_proposed`'s audit `detail` freezes `{lawyerId, addonIds, totalInr}`, and confirm prices strictly from that — verified by "confirm charges the proposal snapshot, not the live cart" in the transcript below (a follow-up add-on is added to the live cart after proposing, and the old proposal still charges the original amount, not the drifted one).
- **A bounds-blocked confirm no longer burns the proposal.** `gate_status` is set to `approved` only *after* `create_order` succeeds; a `BoundsExceeded` leaves the proposal `pending` and retryable instead of dead.

Also: unpaid ("created") orders from the last hour now count toward the daily agent-spend cap (`guardrails.py:check_order_bounds`, `agent_commerce.py:_key_spend_24h`) — previously an agent could mint unlimited open Razorpay orders without ever tripping it.

## Running the harness yourself

```bash
python demo/evaluate.py [--base http://localhost:8000]
```

Exercises every claim above end to end and prints PASS/FAIL/SKIP per
check; exit code 0 only if nothing failed. SKIPs are not silent gaps —
each one names exactly why (see below).

### Transcript: full stack, clean headroom (48/48)

```
== Server & gateway ==
  [PASS] server is healthy
  [PASS] payment config exposes key id + mode — mock=True

== Agent-readable discovery (open, no auth) ==
  [PASS] catalog served at /.well-known/agent-catalog.json
  [PASS] catalog lists services with schema.org-style offers
  [PASS] catalog declares auth scheme
  [PASS] catalog declares merchant bounds
  [PASS] catalog documents agent-order idempotency semantics

== AI-buyer API (authenticated) ==
  [PASS] invalid agent key rejected (401)
  [PASS] valid agent key accepted
  [PASS] quote priced server-side (base + 5% fee) — ₹2100 vs expected ₹2100
  [PASS] agent can place an order
  [PASS] order carries a razorpay order id
  [PASS] mock payment verified -> booking created
  [PASS] order status is paid end-to-end

== AI-buyer API: per-order cap, idempotency & refund ==
  [PASS] order above the agent cap (per-order or rolling daily) is refused (400) — order total ₹31500 exceeds the per-order agent cap of ₹25000
  [PASS] agent order created for idempotency/webhook/refund checks
  [PASS] retrying with the same buyerReference returns the original order, not a duplicate

== Concierge: conversation -> proposal -> human gate -> booking ==
  [PASS] user registration
  [PASS] concierge finds lawyers for a described need
  [PASS] concierge respects the stated budget — [('Sneha Iyer', 3000)]
  [PASS] concierge builds a cart from the selection
  [PASS] upsell addon lands in the cart
  [PASS] checkout produces a PROPOSAL, not an order — agent cannot create orders; gate required
  [PASS] proposal explains its guardrail check
  [PASS] human Confirm & Pay creates the order
  [PASS] order total matches the proposal (server-priced)
  [PASS] a proposal cannot be confirmed twice
  [PASS] signature verified -> booking confirmed
  [PASS] booking visible in My Bookings

== Failure handled gracefully ==
  [PASS] forged/failed signature rejected (400)
  [PASS] failed order marked failed, no booking

== Gate rejection ==
  [PASS] user can decline at the gate
  [PASS] a rejected proposal cannot be confirmed

== Concierge: confirm charges the proposal snapshot, not the live cart ==
  [PASS] confirming after the cart changed still charges exactly what was proposed, not the drifted live total — proposed ₹3649 vs charged ₹3649

== Campaign orchestrator: bounded + merchant-gated ==
  [PASS] 60% discount draft refused by guardrail (400) — discount 60% outside the allowed 0–30% range
  [PASS] in-bounds campaign drafted (status=draft, nothing live)
  [PASS] merchant approval activates campaign + mints payment link

== Webhooks: Razorpay reconciliation safety net ==
  [PASS] forged webhook signature rejected, nothing trusted (400)
  [PASS] valid signature but unrecognised order is ignored, not errored
  [PASS] webhook payment.captured reconciles an order the client never confirmed
  [PASS] order is paid via the webhook alone (no /pay-mock or /verify call made)
  [PASS] agent can cancel a paid order -> refunded in full
  [PASS] webhook payment_link.paid books an anonymous campaign-link purchase
  [PASS] campaign conversions increment from the webhook-driven purchase

== Merchant growth stats ==
  [PASS] merchant stats expose revenue by channel, agentic share, upsell rate — total=₹81368, agentic=100.0%, upsell=35.0%

== Audit trail completeness ==
  [PASS] audit trail covers every money-action type — 154 entries
  [PASS] every audit entry has a rationale
  [PASS] gate states recorded (pending/approved/rejected) — ['approved', 'not_required', 'pending', 'rejected']

==================================================
RESULT: 48 passed, 0 failed, 0 skipped
(completed in 33.6s)
```

### Transcript: lite mode (Docker-equivalent surface), long-lived shared dev DB (41/0/4)

Run against the same lite Python surface Docker runs, on a dev database
that had already accumulated spend from earlier manual testing sessions
that day. Included deliberately, not cherry-picked: it shows what an
exhausted daily cap looks like from the outside, and that the harness
degrades to informative SKIPs rather than crashing when it hits one —
itself evidence the guardrail binds. **A fresh database (a new `docker
compose up`) has full headroom and shows no skips**, as the transcript
above (same code, different DB state) demonstrates.

```
== AI-buyer API (authenticated) ==
  ...
  [PASS] daily-limit guardrail enforced with explanation — agent key 'demo-ai-buyer' has spent ₹53524 in the last 24h; adding ₹2100 would exceed its
  [SKIP] agent order placement + payment — demo key daily limit exhausted in this DB

== AI-buyer API: per-order cap, idempotency & refund ==
  [PASS] order above the agent cap (per-order or rolling daily) is refused (400) — agent key 'demo-ai-buyer' has spent ₹53524 in the last 24h; adding ₹31500 would exceed its daily lim
  [PASS] daily-limit guardrail enforced (idempotency/webhook/refund probe) — agent key 'demo-ai-buyer' has spent ₹53524 in the last 24h; adding ₹2100 would exceed its
  [SKIP] idempotent replay — demo key daily limit exhausted in this DB
  [SKIP] webhook payment.captured reconciliation — no unpaid order available
  [SKIP] refund/cancel lifecycle — no paid order available
  ...

==================================================
RESULT: 41 passed, 0 failed, 4 skipped
(completed in 45.8s)
```

Every check that doesn't need a *successful* order — discovery, the
concierge conversation and gate, the graceful-failure path, campaign
bounds, forged-webhook rejection, the campaign payment-link webhook (a
different auth path from the exhausted agent key), and the audit trail —
still passes unconditionally, because those don't depend on the shared
key's remaining headroom.

## `demo/ai_buyer.py` transcripts

**`--over-budget`**: the buyer targets the most expensive service in the
catalog (seeded specifically above the per-order cap — see "Premium
retainer" in `db/init_db.py` — so this refuses on attempt one instead of
needing several tries), gets refused with the exact rule and numbers,
then falls back to a normal in-budget purchase:

```
[ discover] GET /.well-known/agent-catalog.json from http://localhost:8000
[ discover] merchant 'LexCart' sells 6 services; bounds: max order ₹25000
[     plan] (--over-budget) targeting the priciest service 'Legal consultation — Vikram Nair' at ₹30000
[  refused] attempt 1: merchant blocked the order — order total ₹31500 exceeds the per-order agent cap of ₹25000
[    adapt] guardrail confirmed working; falling back to a bounded, in-budget purchase
[     plan] need: 'help with a property dispute over ancestral land', budget: ₹4000
[     plan] chose 'Legal consultation — Sneha Iyer' (Real Estate Law, ₹3000, rating 4.8) over 5 alternatives
[    quote] firm quote: Consultation — Sneha Iyer ₹3000; Document Review ₹499; Platform fee (5%) ₹150 => total ₹3649
[    order] order 824eca51-0cb8-41d1-8ba0-56f558f76efe created (razorpay order_MOCK2de4560c0e2546), total ₹3649
[      pay] mock gateway payment pay_MOCK837000847dc448 verified; booking 99395a98-fbae-47c0-90cc-bc36122e52d9 confirmed
[     done] final order status: paid — end-to-end agent purchase complete
```

**`--buyer-ref` passed twice** (idempotency demo — `python demo/ai_buyer.py
--buyer-ref demo-idempotency-showcase ...` run a second time with the
same flag):

```
[ discover] GET /.well-known/agent-catalog.json from http://localhost:8000
[ discover] merchant 'LexCart' sells 6 services; bounds: max order ₹25000
[     plan] need: 'startup incorporation', budget: ₹6000
[     plan] chose 'Legal consultation — Kavya Krishnan' (Business & Corporate Law, ₹5000, rating 4.9) over 5 alternatives
[    quote] firm quote: Consultation — Kavya Krishnan ₹5000; Platform fee (5%) ₹250 => total ₹5250
[    order] buyerReference 'demo-idempotency-showcase' already has order 86415225-5e4c-4f93-a82d-35a21bf7640d (status: paid) — replaying it instead of placing a duplicate
[     done] order 86415225-5e4c-4f93-a82d-35a21bf7640d is already paid — nothing more to do
```

## `demo/mcp_server.py`: any MCP client can transact with the merchant

Verified directly with an MCP client (not just imported) — `discover_services("property dispute", 4000)` correctly ranked the Real Estate Law lawyer (Sneha Iyer) top given the budget and need, and `get_quote` returned a firm server-priced total (₹3,649, matching the same pricing invariant tested above). Every call is a thin wrapper over `/api/agent/v1/*`, so an MCP purchase is indistinguishable, server-side, from any other AI-buyer purchase — same guardrails, same audit trail. See the README's "Let your own AI buy from LexCart" section for the client config.

## Honest gaps and defensive-only code

- **The Docker image's exact build was validated by CI** (`.github/workflows/eval.yml` builds the image and runs `demo/evaluate.py` inside a fresh container on every push — see the badge in the README), not by a manual `docker compose up` on the machine this was developed on (Docker isn't installed there). The lite Python surface the image runs was verified directly and extensively (transcripts above); CI is what closes the gap on the container build itself.
- **The auto-refund safety net** in `commerce/orders.py:confirm_payment` (booking creation fails *after* payment capture → automatic refund) is real code, not a stub, but isn't independently exercised by the black-box HTTP harness — triggering it requires a DB-level failure at exactly the moment between capture and booking insert, which isn't reachable through the public API alone. Reviewed by inspection; not a load-bearing claim.
- **Merchant role gating** (`deps/auth.py:require_merchant`, `MERCHANT_EMAILS` env var) defaults to off (any signed-in user acts as merchant) so the buildathon demo and harness work with one account. Setting `MERCHANT_EMAILS` in `.env` restricts `/api/campaigns/{approve,reject}`, `GET /api/campaigns`, and the full audit trail to an allowlist — this is opt-in and not exercised by `demo/evaluate.py` by default.
- **Real Razorpay test-mode keys**: everything above ran on the mock gateway (identical code paths, labeled ids). If `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET` are set, `mock=false` and the harness's mock-only checks (`pay-mock`, webhook signature fabrication) correctly SKIP with a 403-confirmation check instead — see the `if mock: ... else: skip(...)` branches throughout `demo/evaluate.py`.
