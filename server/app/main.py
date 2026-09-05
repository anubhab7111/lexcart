"""
FastAPI backend for the legal platform.
Single Python backend: chatbot/RAG plus auth, lawyers, and bookings
(previously served by the Express server).
"""

import asyncio
import logging
import os
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.deps.errors import MessageHTTPException

# Most of this codebase's own diagnostics use print() (see _warmup's
# docstring below for why), but app/metrics/* and app/multilingual/* use
# logging.getLogger(__name__) -- with no handler configured anywhere,
# those calls were silently dropped rather than reaching stderr. This is
# additive only: it does not change any of the print() call sites.
logging.basicConfig(
    level=logging.DEBUG if get_settings().debug else logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

# Lite mode (LEXCART_LITE=1): boot only the agentic-commerce surface —
# auth, lawyers, payments, concierge, AI-buyer API, campaigns — with no
# LLM/RAG/ML dependencies, scheduler, or warmup. The concierge degrades to
# its deterministic parser; everything that touches money is identical.
# This is what docker-compose runs so an evaluator needs nothing but Docker.
LITE_MODE = os.getenv("LEXCART_LITE", "").strip().lower() in ("1", "true", "yes")

from app.routers import (  # noqa: E402
    agent_commerce,
    auth,
    bookings,
    campaigns,
    concierge,
    lawyers,
    merchant,
    notifications,
    webhooks,
)

if not LITE_MODE:
    from app.routers import (
        bare_acts,
        calendar,
        cases,
        cause_list,
        chat,
        similar_cases,
        vault,
    )
    from app.scheduler import get_scheduler, register_jobs


async def _warmup() -> None:
    """Pre-load everything the chatbot lazily initializes on first use —
    BGE-M3 embeddings, the reranker, the unified + case-law FAISS/BM25
    indices, the fastText language detector, and the Ollama LLM (into VRAM,
    kept resident via keep_alive) — so the first real user query isn't the
    one paying for it. Measured cold-start cost without this: 10+ minutes.

    Runs as a background task so it never delays uvicorn accepting
    connections, and must never raise: any failure here just means the
    first request falls back to the existing per-component lazy-init path.

    Uses print(), not the logging module — nothing in this app configures a
    logging handler/level (uvicorn only configures its own loggers), so
    logger.info()/logger.exception() calls here would be silently dropped.
    print() is what every other diagnostic line in this codebase uses.
    """
    try:
        from app.chatbot import get_fast_llm, get_llm, invoke_llm_safely
        from app.intent_classifier import classify_intent_embedding
        from app.multilingual.language_detection import detect_language
        from app.tools.case_law_rag import get_case_law_rag_system
        from app.tools.unified_legal_rag import get_unified_rag_system

        print("[Warmup] starting background warmup...")
        await get_unified_rag_system().initialize()
        await get_case_law_rag_system().initialize()
        await classify_intent_embedding("What is the punishment for theft?", False)
        await detect_language("test")
        await invoke_llm_safely(get_llm(), "Reply with OK.", stream=False)
        await invoke_llm_safely(get_fast_llm(), "Reply with OK.", stream=False)
        print("[Warmup] complete.")
    except Exception:
        print("[Warmup] failed; components will lazy-init on first request")
        traceback.print_exc()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if LITE_MODE:
        print("[Lite] LEXCART_LITE=1 — commerce-only surface, no scheduler/warmup")
        yield
        return
    # First-ever startup hook in this app. Scheduler jobs are durable
    # (SQLAlchemy jobstore) so restarts don't need to recreate DB state, but
    # add_job(..., replace_existing=True) inside register_jobs() still runs
    # every boot to pick up code changes to job schedules.
    scheduler = get_scheduler()
    register_jobs(scheduler)
    scheduler.start()
    warmup_task = asyncio.create_task(_warmup())
    try:
        yield
    finally:
        warmup_task.cancel()
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="Legal Platform API",
    description="AI-powered legal assistant with document analysis, crime reporting guidance, lawyer search, auth, and bookings.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# CORS middleware for frontend integration. Origins are an explicit allowlist
# (not "*") because allow_credentials=True combined with a wildcard would let
# any origin make credentialed requests.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files for frontend. A Vite production build placed at
# server/frontend (docker does this) is served whole: its hashed bundles
# live under /assets, and the SPA itself hash-routes from index.html.
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
    if (FRONTEND_DIR / "assets").exists():
        app.mount(
            "/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets"
        )

app.include_router(auth.router)
app.include_router(lawyers.router)
app.include_router(bookings.router)
app.include_router(notifications.router)
app.include_router(concierge.router)
app.include_router(agent_commerce.router)
app.include_router(campaigns.router)
app.include_router(merchant.router)
app.include_router(webhooks.router)

if not LITE_MODE:
    app.include_router(chat.router)
    app.include_router(bare_acts.router)
    app.include_router(similar_cases.router)
    app.include_router(cases.router)
    app.include_router(cause_list.router)
    app.include_router(vault.router)
    app.include_router(calendar.router)


@app.get("/")
async def root():
    """Serve the frontend."""
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {"status": "healthy", "version": "1.0.0"}


@app.get("/health")
async def health_check():
    """Health check: distinguishes "process is up" from "process is up but
    a real dependency is down" -- the previous static response couldn't
    tell an operator anything. DB and (outside lite mode) Ollama are
    probed with a short timeout each; a probe failure is reported, not
    raised, so /health itself never 500s."""
    settings = get_settings()
    checks: dict = {}

    try:
        from sqlalchemy import text

        from app.db.engine import get_engine

        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {e}" if settings.debug else "error"

    if not LITE_MODE:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{settings.ollama_base_url}/api/version")
            checks["ollama"] = "ok" if resp.status_code == 200 else f"error: status {resp.status_code}"
        except Exception as e:
            checks["ollama"] = f"error: {e}" if settings.debug else "error"

    overall = "healthy" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": overall, "version": "1.0.0", "checks": checks}


# ============================================================================
# Exception Handlers
# ============================================================================


@app.exception_handler(MessageHTTPException)
async def message_http_exception_handler(request, exc: MessageHTTPException):
    """Scoped to MessageHTTPException only (raised by app.deps.auth and the
    new feature routers) so existing HTTPException usage elsewhere (e.g.
    app.routers.chat) keeps its current {"detail": ...} response shape."""
    return JSONResponse(status_code=exc.status_code, content={"message": exc.detail})


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Global exception handler for unhandled errors.

    FastAPI/Starlette places a bare-Exception handler in
    ServerErrorMiddleware, which sits OUTSIDE CORSMiddleware — so a response
    built here never gets CORS headers from that middleware, and the browser
    reports every unhandled 500 as an opaque "Failed to fetch"/CORS error
    instead of surfacing the actual message. Attach the header manually
    (only for an origin already on the allowlist), and use the
    {"message": ...} shape the client's error handling expects everywhere
    else (see app.deps.errors.MessageHTTPException).
    """
    print(f"Unhandled exception on {request.method} {request.url.path}")
    traceback.print_exc()
    settings = get_settings()
    response = JSONResponse(
        status_code=500,
        content={
            "message": str(exc) if settings.debug else "An unexpected error occurred."
        },
    )
    origin = request.headers.get("origin")
    if origin and origin in settings.cors_origins_list:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
    return response


# ============================================================================
# Main Entry Point
# ============================================================================


def create_app() -> FastAPI:
    """Factory function to create the FastAPI app."""
    return app


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app", host=settings.host, port=settings.port, reload=settings.reload
    )
