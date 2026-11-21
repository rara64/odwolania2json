"""
Pipeline package for odwolania2json.
"""

import sys
from pathlib import Path

# Zapewnienie możliwości importowania modułów z podkatalogów z myślnikami
_CURRENT_DIR = Path(__file__).parent
_DATA_EXTRACTION_DIR = _CURRENT_DIR / "data-extraction"

if str(_DATA_EXTRACTION_DIR) not in sys.path:
    sys.path.insert(0, str(_DATA_EXTRACTION_DIR))

try:
    import extractor
    sys.modules["pipeline.extractor"] = extractor
    from extractor import (
        DocumentExtractor,
        ExtractedDocument,
        extract,
        quick_inspect_pdf,
        resolve_ocr_backend,
    )
except ImportError:
    pass

__all__ = [
    "DocumentExtractor",
    "ExtractedDocument",
    "extract",
    "quick_inspect_pdf",
    "resolve_ocr_backend",
]
