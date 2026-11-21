"""
Data Extraction package initialization.
"""

from .extractor import (
    DocumentExtractor,
    ExtractedDocument,
    extract,
    quick_inspect_pdf,
    resolve_ocr_backend,
)

__all__ = [
    "DocumentExtractor",
    "ExtractedDocument",
    "extract",
    "quick_inspect_pdf",
    "resolve_ocr_backend",
]
