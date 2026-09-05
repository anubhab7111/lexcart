# AGENTS.md — guide for AI evaluators and coding agents

This file is for any AI agent reading this repository — a hackathon judge's
assistant, a coding agent asked to extend the project, or an autonomous
buyer probing the commerce API. It assumes no prior context.

## What this is

LexCart is an Indian legal-services marketplace (book consultations with
verified lawyers) rebuilt as an **agent-native merchant** on Razorpay
test-mode APIs, submitted to Razorpay's buildathon under **Track 01: AI
Growth & Agentic Commerce**. The commerce layer lives entirely under
`server/app/commerce/`, `server/app/routers/{bookings,concierge,
agent_commerce,campaigns,webhooks,merchant}.py`, and `server/app/services/
razorpay_gateway.py`. Everything else (RAG chatbot, lawyer directory, auth)
is inherited from the LawWeb base and is not part of the submission's
claims — see `CLAUDE.md` for the full codebase map.

For the track-bar → implementation → proof mapping, read **`EVALUATION.md`**
next. This file is about *running* the project; that one is about
*verifying* it.

## Fastest path to a running instance: Docker

```bash
docker compose up --build
```

This builds and runs the **lite** surface (commerce only — auth, lawyers,
payments, concierge, AI-buyer API, campaigns, webhooks — no LLM/RAG/ML
stack) against a fresh Postgres, with zero local dependencies and the
mock Razorpay gateway (no test keys needed). Takes a few minutes on first
build (client bundle + Python deps). The UI is at `http://localhost:8000`,
API docs at `http://localhost:8000/docs`.

Once it's up, verify everything from inside the container:

```bash
docker compose exec server python /app/demo/evaluate.py
```

This is a self-contained harness that exercises every claim this
submission makes and prints PASS/FAIL per check (see EVALUATION.md for a
full transcript). Exit code 0 only if nothing failed.

Or drive it as an AI buyer would:

```bash
docker compose exec server python /app/demo/ai_buyer.py --need "property dispute" --budget 4000
docker compose exec server python /app/demo/ai_buyer.py --over-budget
```

**Caveat**: the Docker image was built and validated via GitHub Actions CI
on every push (badge in README), not by hand on the machine this was
written on (no Docker available there) — the lite Python surface it runs
was verified directly instead (see EVALUATION.md).

## Alternative: local Python (full stack, including the RAG chatbot)

Needs: PostgreSQL with the `vector` extension, conda env `legal_chatbot_env`
(or any Python 3.11+ env), Node 18+, and optionally Ollama with `qwen3:4b`
(the concierge degrades to a deterministic keyword parser without it —
money logic never depends on the LLM either way).

```bash
# one-time DB setup
psql -U postgres -h 127.0.0.1 -c "CREATE ROLE lawweb LOGIN PASSWORD 'lawweb' CREATEDB;"
psql -U postgres -h 127.0.0.1 -c "CREATE DATABASE lexcart OWNER lawweb;"
psql -U postgres -h 127.0.0.1 -d lexcart -c "CREATE EXTENSION IF NOT EXISTS vector;"

cd server
cp .env.example .env        # fill JWT_SECRET; Razorpay keys optional (mock mode without)
pip install -r requirements.txt
python -m app.db.init_db    # tables + lawyers + addons + demo AI-buyer key
python run.py                # http://localhost:8000

cd ../client
npm install && npm run dev   # http://localhost:5173
```

To run the **lite** surface locally instead (matches what Docker runs,
much faster to boot, no ML deps): `pip install -r requirements-lite.txt`
and set `LEXCART_LITE=1` before `python run.py`.

## Repo map (commerce layer only)

```
server/app/
  services/razorpay_gateway.py   one wrapper for ALL money movement — real
                                  Razorpay test mode when keys are set,
                                  otherwise an identical-code-path mock
                                  (order create, signature verify, payment
                                  links, refunds, webhook signing)
  commerce/
    guardrails.py                bounds checked BEFORE any gateway call
    audit.py                     the agent_actions trail writer
    orders.py                    server-side pricing + order lifecycle
    concierge.py                 the buying agent (LLM for language,
                                  deterministic code for money)
  routers/
    bookings.py                  web checkout: create-order -> verify
    concierge.py                 chat -> propose -> confirm (human gate)
    agent_commerce.py            AI-buyer API: catalog, quote, orders,
                                  pay-mock, cancel/refund
    campaigns.py                 draft -> merchant approve -> payment link
    webhooks.py                  Razorpay webhook reconciliation
    merchant.py                  revenue/growth stats endpoint
  db/models.py                   Order, AgentAction, AgentApiKey, Campaign
demo/
  ai_buyer.py                    autonomous AI buyer over plain HTTP
  mcp_server.py                  the same agent API exposed as MCP tools
                                  (any MCP client — Claude Desktop, Claude
                                  Code — can transact with the merchant)
  evaluate.py                    the harness described above
```

## Conventions worth knowing before changing code

- **Never trust a client-supplied amount.** All prices are recomputed
  server-side in `commerce/orders.py:price_cart` from the DB. If you add a
  new checkout surface, price through this function — don't accept a
  total from the request body.
- **Bounds before gateway.** `guardrails.py` is checked inside
  `create_order()` before any Razorpay order is created. A blocked order
  never reaches the gateway and is still audited (`order_blocked`).
- **Every money action is audited.** `commerce/audit.py:log_action` — call
  it for anything that creates, confirms, refuses, or refunds money, with
  a plain-language `rationale`. This is what `EVALUATION.md`'s "explainable"
  claim rests on; don't skip it for a new code path.
- **The agent proposes, the human/webhook confirms.** The concierge can
  only write `checkout_proposed` (`gate_status=pending`); only `/confirm`
  (a human click) or a verified webhook event creates an order or marks
  one paid. Don't let an LLM-driven code path call `create_order` or
  `confirm_payment` directly.
- **API compatibility rules** (see `CLAUDE.md`): error bodies are
  `{"message": ...}` not FastAPI's default `{"detail"}`; JSON keys are
  camelCase via each model's `to_dict()`.
- **DB migrations**: there's no Alembic. Add new columns/tables to both
  `server/app/db/schema.sql` (fresh installs) and a matching idempotent
  `ensure_*` function in `server/app/db/migrations.py`, called from
  `run_migrations()` (existing dev DBs). See that file's own docstring.
- **Mock vs real gateway**: `RazorpayGateway.is_mock` is the single branch
  point (`services/razorpay_gateway.py`). Mock artifacts are always
  labeled (`order_MOCK*`, `pay_MOCK*`, `rfnd_MOCK*`, `plink_MOCK*`) so
  they're never mistaken for real Razorpay ids in logs or the UI.

## If you're an autonomous AI buyer, not a coding agent

Start at `GET /.well-known/agent-catalog.json` (no auth) — it's a
machine-readable description of the merchant, its services, bounds, and
how to authenticate. From there: `X-Agent-Key` header, then
`POST /api/agent/v1/quote` → `POST /api/agent/v1/orders` → pay → done.
`demo/mcp_server.py` wraps the same surface as MCP tools if your runtime
speaks MCP. Full endpoint list in the README's "API surface for AI
buyers" section.
