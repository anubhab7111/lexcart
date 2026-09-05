"""Tools package initialization."""

import os

from .document_extractor import DocumentExtractor, get_document_extractor
from .crime_reporter import detect_crime_type, is_complex_crime, CRIME_TYPES
from .lawyer_recommender import (
    recommend_lawyers,
    format_lawyer_results,
    LEGAL_SPECIALIZATIONS,
)

__all__ = [
    "DocumentExtractor",
    "get_document_extractor",
    "detect_crime_type",
    "is_complex_crime",
    "CRIME_TYPES",
    "recommend_lawyers",
    "format_lawyer_results",
    "LEGAL_SPECIALIZATIONS",
]

# LEXCART_LITE=1 (the Docker evaluation image) has none of the RAG/
# document-analysis dependencies installed (aiohttp, langchain, ...) --
# see server/requirements-lite.txt. The lite-mode routers only ever reach
# into this package for lawyer_recommender above; everything past this
# point belongs to the legal-chatbot/document-analysis feature set and
# must not be eagerly imported here (this __init__ runs on any
# `from app.tools.<anything> import ...`, before any caller-side guard).
if os.getenv("LEXCART_LITE", "").strip().lower() not in ("1", "true", "yes"):
    from .indian_kanoon import IndianKanoonTool, get_indian_kanoon_tool
    from .document_classifier import DocumentClassifier, get_document_classifier
    from .statutory_validator import StatutoryValidator, get_statutory_validator
    from .indian_law_rag import IndianLawRAGTool, get_indian_law_rag
    from .legal_defect_analyzer import LegalDefectAnalyzer, get_legal_defect_analyzer

    # Multi-domain RAG modules (replaces monolithic crime_rag.py)
    from .base_legal_rag import BaseLegalRAGSystem, LegalChunk, LegalContext
    from .criminal_rag import CriminalRAGSystem, get_criminal_rag_system
    from .civil_rag import CivilRAGSystem, get_civil_rag_system
    from .constitutional_rag import ConstitutionalRAGSystem, get_constitutional_rag_system
    from .unified_legal_rag import UnifiedLegalRAGSystem, get_unified_rag_system

    __all__ += [
        "IndianKanoonTool",
        "get_indian_kanoon_tool",
        "DocumentClassifier",
        "get_document_classifier",
        "StatutoryValidator",
        "get_statutory_validator",
        "IndianLawRAGTool",
        "get_indian_law_rag",
        "LegalDefectAnalyzer",
        "get_legal_defect_analyzer",
        # Multi-domain RAG
        "BaseLegalRAGSystem",
        "LegalChunk",
        "LegalContext",
        "CriminalRAGSystem",
        "get_criminal_rag_system",
        "CivilRAGSystem",
        "get_civil_rag_system",
        "ConstitutionalRAGSystem",
        "get_constitutional_rag_system",
        "UnifiedLegalRAGSystem",
        "get_unified_rag_system",
    ]
