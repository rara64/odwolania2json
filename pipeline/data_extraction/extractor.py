"""Ekstrakcja PDF -> dict (Docling + RapidOCR na CPU, z awaryjnym odczytem przez pypdfium2).

Użycie:
    from pipeline.data_extraction import extract_pdf

    result = extract_pdf("odwolanie.pdf")
    result["text"], result["markdown"], result["first_3_pages_markdown"], result["metadata"]

Wynik ma schemat DoclingDocument (`texts`, `tables`, `pages`, `prov` z `bbox`) uzupełniony
o gotowy tekst, Markdown i metadane. Gdy Docling zawiedzie, ten sam schemat buduje
odczyt awaryjny: warstwa tekstowa pypdfium2, a dla zeskanowanych stron RapidOCR.
"""
from __future__ import annotations

import gc
import logging
import time
from contextlib import closing, nullcontext
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

MIN_TEXT_CHARS = 50  # mniej znaków w warstwie tekstowej => strona traktowana jako skan
PREVIEW_PAGES = 3  # liczba stron w `first_3_pages_markdown`
OCR_RENDER_SCALE = 1.5  # skala renderowania strony do OCR w odczycie awaryjnym
CPU_THREADS = 4


# --- publiczne API -------------------------------------------------------------

@dataclass(frozen=True)
class PageLayout:
    """Podział stron PDF (numerowanych od 1) na cyfrowe i zeskanowane."""

    total: int
    digital: list[int]
    scanned: list[int]


def resolve_ocr_backend() -> str:
    """RapidOCR na onnxruntime, jeśli jest zainstalowany; w przeciwnym razie na torch."""
    try:
        import onnxruntime  # type: ignore # noqa: F401
    except ImportError:
        return "torch"
    return "onnxruntime"


def inspect_pages(path: Path) -> PageLayout:
    """Sprawdza warstwę tekstową każdej strony. Przy błędzie zakłada jedną stronę cyfrową."""
    try:
        import pypdfium2 as pdfium  # type: ignore

        digital: list[int] = []
        scanned: list[int] = []
        with closing(pdfium.PdfDocument(str(path))) as pdf:
            for page_no, page in _iter_pages(pdf, len(pdf)):
                has_text = len(_page_text(page).strip()) >= MIN_TEXT_CHARS
                (digital if has_text else scanned).append(page_no)
            return PageLayout(len(pdf), digital, scanned)
    except Exception as exc:
        logger.debug("Inspekcja pypdfium2 nie powiodła się dla %s: %s", path.name, exc)
        return PageLayout(1, [1], [])


class PdfExtractor:
    """Lekki ekstraktor PDF -> dict. Konwerter Docling i silnik OCR są tworzone leniwie i współdzielone."""

    def __init__(self, ocr_enabled: bool = True, ocr_lang: list[str] | None = None):
        self.ocr_enabled = ocr_enabled
        self.ocr_lang = ocr_lang or ["pl"]
        self.backend = resolve_ocr_backend()
        self._converter = None
        self._ocr_engine = None

    def extract(self, file_path: str | Path, max_pages: int | None = None) -> dict[str, Any]:
        """Przetwarza plik PDF (opcjonalnie tylko `max_pages` pierwszych stron) i zwraca dict."""
        start = time.time()
        path = Path(file_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Plik nie istnieje: {path}")

        layout = inspect_pages(path)
        last_page = min(max_pages, layout.total) if max_pages else layout.total

        try:
            with _inference_mode():
                result = self._extract_with_docling(path, (1, last_page) if max_pages else None)
            ocr_applied = self.ocr_enabled and any(p <= last_page for p in layout.scanned)
            status, extra = "success", {}
        except Exception as exc:
            logger.warning("Błąd Docling dla %s (%s) - uruchamianie fallbacku.", path.name, exc)
            result, ocr_applied = self._extract_fallback(path, layout, last_page)
            status, extra = "fallback", {"warning": str(exc)}
        finally:
            gc.collect()

        result["metadata"] = {
            "filename": path.name,
            "source_path": str(path),
            "total_pages": layout.total,
            "extracted_pages": len(result.get("pages", {})),
            "digital_pages": layout.digital,
            "scanned_pages": layout.scanned,
            "ocr_applied": ocr_applied,
            "duration_seconds": round(time.time() - start, 3),
            "status": status,
            **extra,
        }
        return result

    # --- Docling ---------------------------------------------------------------

    def _extract_with_docling(self, path: Path, page_range: tuple[int, int] | None) -> dict[str, Any]:
        kwargs = {"page_range": page_range} if page_range else {}
        doc = self._get_converter().convert(str(path), **kwargs).document

        result: dict[str, Any] = doc.export_to_dict()
        for table_dict, table in zip(result.get("tables", []), doc.tables):
            try:
                table_dict["markdown"] = table.export_to_markdown(doc=doc)
            except Exception as exc:
                logger.debug("Nie udało się wyeksportować tabeli do Markdown: %s", exc)

        markdown = doc.export_to_markdown()
        result["markdown"] = markdown
        result["text"] = doc.export_to_text()
        result["first_3_pages_markdown"] = _docling_preview(doc, markdown)
        return result

    def _get_converter(self):
        if self._converter is None:
            self._converter = self._build_converter()
        return self._converter

    def _build_converter(self):
        from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            PdfPipelineOptions,
            RapidOcrOptions,
            TableFormerMode,
            TableStructureOptions,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption

        options = PdfPipelineOptions()
        options.accelerator_options = AcceleratorOptions(num_threads=CPU_THREADS, device=AcceleratorDevice.CPU)
        options.do_ocr = self.ocr_enabled
        options.do_table_structure = True
        options.generate_table_images = False
        options.table_structure_options = TableStructureOptions(
            do_cell_matching=True,
            mode=TableFormerMode.ACCURATE,
        )
        if self.ocr_enabled:
            options.ocr_options = RapidOcrOptions(backend=self.backend, lang=self.ocr_lang)

        return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})

    # --- odczyt awaryjny (pypdfium2 + RapidOCR) --------------------------------

    def _extract_fallback(self, path: Path, layout: PageLayout, last_page: int) -> tuple[dict[str, Any], bool]:
        """Odczyt per-strona w schemacie DoclingDocument. Zwraca (wynik, czy_użyto_OCR)."""
        import pypdfium2 as pdfium  # type: ignore

        texts: list[dict[str, Any]] = []
        pages: dict[str, Any] = {}
        ocr_applied = False

        with closing(pdfium.PdfDocument(str(path))) as pdf:
            for page_no, page in _iter_pages(pdf, min(last_page, len(pdf))):
                width, height = page.get_size()
                is_scan = page_no in layout.scanned
                pages[str(page_no)] = {"size": {"width": width, "height": height}, "is_ocr": is_scan}

                if is_scan and self.ocr_enabled:
                    lines = self._ocr_lines(page)
                    ocr_applied = True
                else:
                    lines = _text_layer_lines(page, width, height)

                for text, bbox in lines:
                    texts.append({
                        "self_ref": f"#/texts/{len(texts)}",
                        "label": "text",
                        "text": text,
                        "prov": [{"page_no": page_no, "bbox": bbox}],
                    })

        full_text = "\n".join(t["text"] for t in texts)
        preview = "\n".join(t["text"] for t in texts if t["prov"][0]["page_no"] <= PREVIEW_PAGES)
        result = {
            "schema_name": "DoclingDocument",
            "name": path.stem,
            "texts": texts,
            "tables": [],
            "pages": pages,
            "markdown": full_text,
            "text": full_text,
            "first_3_pages_markdown": preview,
        }
        return result, ocr_applied

    def _ocr_lines(self, page) -> list[tuple[str, dict[str, Any]]]:
        """OCR wyrenderowanej strony; bbox przeliczone na współrzędne strony (TOPLEFT)."""
        import numpy as np

        image = page.render(scale=OCR_RENDER_SCALE).to_pil()
        try:
            ocr = self._get_ocr_engine()(np.array(image))
        finally:
            image.close()

        if not getattr(ocr, "txts", None):
            return []

        lines = []
        for text, box in zip(ocr.txts, getattr(ocr, "boxes", [])):
            xs = [pt[0] / OCR_RENDER_SCALE for pt in box]
            ys = [pt[1] / OCR_RENDER_SCALE for pt in box]
            bbox = {
                "l": float(min(xs)), "t": float(min(ys)),
                "r": float(max(xs)), "b": float(max(ys)),
                "coord_origin": "TOPLEFT",
            }
            lines.append((text, bbox))
        return lines

    def _get_ocr_engine(self):
        if self._ocr_engine is None:
            from rapidocr import EngineType, RapidOCR  # type: ignore

            engine = EngineType.TORCH if self.backend == "torch" else EngineType.ONNXRUNTIME
            self._ocr_engine = RapidOCR(params={f"{stage}.engine_type": engine for stage in ("Det", "Cls", "Rec")})
        return self._ocr_engine


@lru_cache(maxsize=1)
def _default_extractor() -> PdfExtractor:
    return PdfExtractor()


def extract_pdf(file_path: str | Path, max_pages: int | None = None) -> dict[str, Any]:
    """Główna funkcja dla potoku: przyjmuje ścieżkę do PDF i zwraca dict (współdzielony ekstraktor)."""
    return _default_extractor().extract(file_path, max_pages=max_pages)


# --- pomocnicze -----------------------------------------------------------------

def _iter_pages(pdf, count: int) -> Iterator[tuple[int, Any]]:
    """Kolejne strony (numer od 1, strona); każda zamykana po przejściu do następnej."""
    for idx in range(count):
        with closing(pdf[idx]) as page:
            yield idx + 1, page


def _page_text(page) -> str:
    with closing(page.get_textpage()) as textpage:
        return textpage.get_text_range()


def _text_layer_lines(page, width: float, height: float) -> list[tuple[str, dict[str, Any]]]:
    """Niepuste linie warstwy tekstowej; bez pozycji linii, więc bbox to cała strona."""
    lines = []
    for line in _page_text(page).splitlines():
        line = line.strip()
        if line:
            bbox = {"l": 0.0, "t": 0.0, "r": float(width), "b": float(height), "coord_origin": "BOTTOMLEFT"}
            lines.append((line, bbox))
    return lines


def _docling_preview(doc, full_markdown: str) -> str:
    """Markdown pierwszych PREVIEW_PAGES stron (dla LLM)."""
    if len(doc.pages) <= PREVIEW_PAGES:
        return full_markdown
    try:
        parts = [doc.export_to_markdown(page_no=p) for p in sorted(doc.pages) if p <= PREVIEW_PAGES]
    except TypeError:  # starszy Docling bez parametru page_no
        return full_markdown
    return "\n\n".join(parts)


def _inference_mode():
    try:
        import torch
    except ImportError:
        return nullcontext()
    return torch.inference_mode()
