"""
App package initialization.
"""

import os

from app.config import get_settings, Settings

__all__ = [
    "get_settings",
    "Settings",
]

# LEXCART_LITE=1 (the Docker evaluation image) has no LLM/RAG dependencies
# installed, so chatbot.py (which imports langchain_core) must not be
# eagerly imported here — this runs on every `import app.*`, including
# `python -m app.db.init_db`, before main.py's own lite-mode gating ever runs.
if os.getenv("LEXCART_LITE", "").strip().lower() not in ("1", "true", "yes"):
    from app.chatbot import get_chatbot, LegalChatbot

    __all__ += ["get_chatbot", "LegalChatbot"]
