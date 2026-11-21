from __future__ import annotations

import gc
import logging
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("PdfExtractor")


def _resolve_ocr_backend() -> str:
    try:
        import onnxruntime  # type: ignore # noqa: F401
        return "onnxruntime"
    except ImportError:
        return "torch"


def _inspect_pages(path: Path) -> Tuple[int, List[int], List[int]]:
    """Zwraca (liczba_stron, strony_cyfrowe, strony_zeskanowane)."""
    digital_pages: List[int] = []
    scanned_pages: List[int] = []
    doc = None
    try:
        import pypdfium2 as pdfium  # type: ignore
        doc = pdfium.PdfDocument(str(path))
        total_pages = len(doc)
        for i in range(total_pages):
            page = doc[i]
            try:
                textpage = page.get_textpage()
                try:
                    txt = textpage.get_text_range().strip()
                    (scanned_pages if len(txt) < 50 else digital_pages).append(i + 1)
                finally:
                    textpage.close()
            finally:
                page.close()
        return total_pages, digital_pages, scanned_pages
    except Exception as e:
        logger.debug(f"Inspekcja pypdfium2 nie powiodła się dla {path.name}: {e}")
        return 1, [1], []
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


class PdfExtractor:
    """Lekki ekstraktor PDF -> dict (Docling + RapidOCR na CPU)."""

    def __init__(self, ocr_enabled: bool = True, ocr_lang: Optional[List[str]] = None):
        self.ocr_enabled = ocr_enabled
        self.ocr_lang = ocr_lang or ["pl"]
        self.backend = _resolve_ocr_backend()
        self._converter = None

    def _get_converter(self):
        if self._converter is not None:
            return self._converter

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
        options.accelerator_options = AcceleratorOptions(num_threads=4, device=AcceleratorDevice.CPU)
        options.do_ocr = self.ocr_enabled
        options.do_table_structure = True
        options.generate_table_images = False
        options.table_structure_options = TableStructureOptions(
            do_cell_matching=True,
            mode=TableFormerMode.ACCURATE,
        )
        if self.ocr_enabled:
            options.ocr_options = RapidOcrOptions(backend=self.backend, lang=self.ocr_lang)

        self._converter = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        )
        return self._converter

    def extract(
        self,
        file_path: Union[str, Path],
        max_pages: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Przetwarza plik PDF i zwraca czysty słownik zawierający pełną strukturę
        Docling (`texts`, `tables`, `pages`, `prov` z `bbox`) oraz gotowy tekst i Markdown.
        """
        start_time = time.time()
        path = Path(file_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Plik nie istnieje: {path}")

        total_pages, digital_pages, scanned_pages = _inspect_pages(path)
        page_range = (1, min(max_pages, total_pages)) if max_pages else None

        try:
            import torch
            ctx = torch.inference_mode()
        except ImportError:
            ctx = nullcontext()

        res = None
        doc = None
        try:
            with ctx:
                converter = self._get_converter()
                kwargs = {"page_range": page_range} if page_range else {}
                res = converter.convert(str(path), **kwargs)
                doc = res.document

                result: Dict[str, Any] = doc.export_to_dict()

                # Dołączenie gotowego Markdown dla każdej tabeli
                for idx, table in enumerate(doc.tables):
                    if idx < len(result.get("tables", [])):
                        try:
                            result["tables"][idx]["markdown"] = table.export_to_markdown(doc=doc)
                        except Exception:
                            pass

                full_md = doc.export_to_markdown()
                result["markdown"] = full_md
                result["text"] = doc.export_to_text()

                # Gotowy wycinek pierwszych 3 stron (z tabelami, bez stopek) dla LLM
                if len(doc.pages) <= 3:
                    result["first_3_pages_markdown"] = full_md
                else:
                    first_pages = []
                    for p_no in sorted(doc.pages.keys()):
                        if p_no <= 3:
                            try:
                                first_pages.append(doc.export_to_markdown(page_no=p_no))
                            except TypeError:
                                first_pages.append(full_md)
                                break
                    result["first_3_pages_markdown"] = "\n\n".join(first_pages)

                in_range_scans = (
                    [p for p in scanned_pages if p <= page_range[1]] if page_range else scanned_pages
                )
                result["metadata"] = {
                    "filename": path.name,
                    "source_path": str(path),
                    "total_pages": total_pages,
                    "extracted_pages": len(result.get("pages", {})),
                    "digital_pages": digital_pages,
                    "scanned_pages": scanned_pages,
                    "ocr_applied": bool(self.ocr_enabled and in_range_scans),
                    "duration_seconds": round(time.time() - start_time, 3),
                    "status": "success",
                }
                return result

        except Exception as exc:
            logger.warning(f"Błąd Docling dla {path.name} ({exc}) - uruchamianie fallbacku.")
            return self._fallback_extract(path, total_pages, digital_pages, scanned_pages, page_range, start_time, str(exc))
        finally:
            del res, doc
            gc.collect()

    def _fallback_extract(
        self,
        path: Path,
        total_pages: int,
        digital_pages: List[int],
        scanned_pages: List[int],
        page_range: Optional[Tuple[int, int]],
        start_time: float,
        error_msg: str,
    ) -> Dict[str, Any]:
        """Awaryjny odczyt per-strona (pypdfium2 + RapidOCR) zwracający ten sam schemat słownika."""
        import numpy as np
        import pypdfium2 as pdfium  # type: ignore

        end_page = page_range[1] if page_range else total_pages
        texts: List[Dict[str, Any]] = []
        pages_dict: Dict[str, Any] = {}
        all_lines: List[str] = []
        first_3_lines: List[str] = []
        rapid_engine = None
        doc = None

        try:
            doc = pdfium.PdfDocument(str(path))
            counter = 0
            for idx in range(min(end_page, len(doc))):
                page_no = idx + 1
                page = doc[idx]
                w, h = page.get_size()
                is_scan = page_no in scanned_pages
                pages_dict[str(page_no)] = {"size": {"width": w, "height": h}, "is_ocr": is_scan}

                try:
                    textpage = page.get_textpage()
                    try:
                        raw_text = textpage.get_text_range()
                    finally:
                        textpage.close()

                    if is_scan and self.ocr_enabled:
                        if rapid_engine is None:
                            from rapidocr import EngineType, RapidOCR  # type: ignore
                            eng = EngineType.TORCH if self.backend == "torch" else EngineType.ONNXRUNTIME
                            rapid_engine = RapidOCR(
                                params={"Det.engine_type": eng, "Cls.engine_type": eng, "Rec.engine_type": eng}
                            )
                        scale = 1.5
                        pil_img = page.render(scale=scale).to_pil()
                        try:
                            ocr_res = rapid_engine(np.array(pil_img))
                            if getattr(ocr_res, "txts", None):
                                for t_str, box in zip(ocr_res.txts, getattr(ocr_res, "boxes", [])):
                                    xs = [pt[0] / scale for pt in box]
                                    ys = [pt[1] / scale for pt in box]
                                    bbox = {
                                        "l": float(min(xs)), "t": float(min(ys)),
                                        "r": float(max(xs)), "b": float(max(ys)),
                                        "coord_origin": "TOPLEFT",
                                    }
                                    texts.append({
                                        "self_ref": f"#/texts/{counter}",
                                        "label": "text",
                                        "text": t_str,
                                        "prov": [{"page_no": page_no, "bbox": bbox}],
                                    })
                                    all_lines.append(t_str)
                                    if page_no <= 3:
                                        first_3_lines.append(t_str)
                                    counter += 1
                        finally:
                            pil_img.close()
                    else:
                        for line in raw_text.splitlines():
                            line = line.strip()
                            if line:
                                texts.append({
                                    "self_ref": f"#/texts/{counter}",
                                    "label": "text",
                                    "text": line,
                                    "prov": [{
                                        "page_no": page_no,
                                        "bbox": {"l": 0.0, "t": 0.0, "r": float(w), "b": float(h), "coord_origin": "BOTTOMLEFT"},
                                    }],
                                })
                                all_lines.append(line)
                                if page_no <= 3:
                                    first_3_lines.append(line)
                                counter += 1
                finally:
                    page.close()
        finally:
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass
            gc.collect()

        full_text = "\n".join(all_lines)
        return {
            "schema_name": "DoclingDocument",
            "name": path.stem,
            "texts": texts,
            "tables": [],
            "pages": pages_dict,
            "markdown": full_text,
            "text": full_text,
            "first_3_pages_markdown": "\n".join(first_3_lines),
            "metadata": {
                "filename": path.name,
                "source_path": str(path),
                "total_pages": total_pages,
                "extracted_pages": len(pages_dict),
                "digital_pages": digital_pages,
                "scanned_pages": scanned_pages,
                "ocr_applied": bool(rapid_engine is not None),
                "duration_seconds": round(time.time() - start_time, 3),
                "status": "fallback",
                "warning": error_msg,
            },
        }


# Współdzielona instancja do prostego wywołania w innych skryptach potoku
_EXTRACTOR: Optional[PdfExtractor] = None


def extract_pdf(file_path: Union[str, Path], max_pages: Optional[int] = None) -> Dict[str, Any]:
    """Główna funkcja dla potoku: przyjmuje ścieżkę do PDF i zwraca czysty dict."""
    global _EXTRACTOR
    if _EXTRACTOR is None:
        _EXTRACTOR = PdfExtractor()
    return _EXTRACTOR.extract(file_path, max_pages=max_pages)