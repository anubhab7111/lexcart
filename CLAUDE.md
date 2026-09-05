# CLAUDE.md

Guidance for working on this repository.

## Project Overview

LexCart (Razorpay Buildathon, Track 01: AI Growth & Agentic Commerce) is an AI-powered legal-services marketplace built on the LawWeb codebase. On top of the legal platform it adds an agentic-commerce layer on Razorpay test mode: a conversational checkout concierge, an agent-readable catalog + AI-buyer API, upsell add-ons, and a campaign orchestrator — all bounded, gated, and audited (see `server/app/commerce/` and README.md).

The chatbot uses a retrieval-augmented generation (RAG) pipeline built on Indian Bare Acts and legal documents, along with LangGraph-based agent orchestration, to answer legal questions with relevant statutory references. The platform also includes user authentication, lawyer management, online appointment booking, and payment integration.

The primary goals of the project are:
- Provide accurate, retrieval-backed answers to questions about Indian law.
- Help users discover and book consultations with lawyers.
- Serve as an educational platform for understanding legal concepts and procedures.
- Maintain a modular architecture that supports future expansion with additional legal datasets and AI capabilities.

## Python environment — important

**Always use the conda env `legal_chatbot_env`** for anything Python (running the server, scripts, installing deps):

```bash
conda activate legal_chatbot_env
# or directly: /home/ushtro/.conda/envs/legal_chatbot_env/bin/python
```

## Layout

- `server/` — the single FastAPI backend (port 8000). `app/main.py` wires routers from `app/routers/` (auth, lawyers, bookings, chat); `app/chatbot.py` is the LangGraph chatbot; `app/tools/` holds RAG systems and document pipeline; `app/db/` holds SQLModel models, `schema.sql`, and `init_db`.
- `client/` — React + Vite frontend (port 5173). API base URL from `VITE_API_URL`, default `http://localhost:8000/api`.
- There is no Node backend. JS/TS exists only in `client/`.

## Running

```bash
cd server && python run.py        # backend; DB init first time: python -m app.db.init_db
cd client && npm run dev          # frontend
```

- Ollama must be running with `qwen3:14b` (model names in `app/config.py`).
- Run uvicorn with a **single worker**: hardware budget is ~15GB RAM / 4GB VRAM, and each worker would load its own copy of the embedding models.

## Database

Local PostgreSQL, db/role `lawweb`/`lawweb`, database `lexcart`, connection via `DATABASE_URL` in `server/.env` (never commit `.env`). Schema is plain SQL in `server/app/db/schema.sql`; `python -m app.db.init_db` is idempotent (creates tables if missing, seeds 5 demo lawyers with legacy text ids `'1'..'5'`). To reset: `dropdb`/`createdb` as postgres (`psql -U postgres -h 127.0.0.1` works without sudo), then rerun `init_db`.

## API compatibility rules (client depends on these)

- Auth/lawyers/bookings errors return `{"message": "..."}` bodies — not FastAPI's default `{"detail"}`.
- Lawyer and booking JSON uses **camelCase** keys (`hourlyRate`, `successRate`, `userId`, `transactionId`, …) via the models' `to_dict()` helpers.
- `GET /api/bookings/client_token` returns **plain text**, not JSON.
- Chat endpoint paths under `/api/chat` are fixed — the client calls them directly.
- JWT secret/algorithm (HS256) and bcrypt hash format must stay compatible with existing users and tokens.

## RAG

- Corpus: `server/app/data/bare_acts/<domain>/*.pdf`; indices: `server/app/data/faiss_index/<domain>/`.
- Rebuild after corpus changes: `python rebuild_rag_indices.py --all` (run from `server/`; scripts chdir themselves because data paths resolve relative to CWD).
- Accuracy sweep: `python tests/test_chatbot.py` (run from `server/`; slow; needs Ollama).

## Payments

Razorpay **test mode** via `app/services/razorpay_gateway.py` — the single wrapper for all money movement (orders, HMAC signature verification, payment links). With `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET` unset in `server/.env`, a mock gateway with identical code paths runs instead (labeled `order_MOCK*` ids; `POST /api/bookings/mock-pay` stands in for checkout.js). Amounts are always priced server-side in `app/commerce/orders.py`; every money action is audited to `agent_actions` and bounds-checked by `app/commerce/guardrails.py` before any gateway order exists.

## Working conventions

- **Comment sparingly.** Only add a comment where the logic is genuinely complex or non-obvious (a subtle workflow, a non-obvious constraint, a "why" that isn't clear from the code). Don't add comments that just restate what the code plainly does or narrate each step — let readable code speak for itself.
- **Ask when unsure.** When requirements are ambiguous, a decision is genuinely the user's to make, or a change is hard to reverse, ask a clarifying question instead of assuming. A quick question is better than building the wrong thing.

## Git workflow

Before opening a PR/MR, rebase the branch onto the latest base branch (e.g. `main`) so it merges cleanly with a linear history instead of drifting or landing via a merge commit.
