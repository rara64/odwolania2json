"""Ekstrakcja PDF -> dict (Docling + RapidOCR na CPU, z awaryjnym odczytem przez pypdfium2).

Użycie:
    from pipeline.data_extraction import extract_pdf

    result = extract_pdf("odwolanie.pdf")
    result["text"], result["markdown"], result["first_3_pages_markdown"], result["metadata"]

Wynik ma schemat DoclingDocument (`texts`, `tables`, `pages`, `prov` z `bbox`) uzupełniony
o gotowy tekst, Markdown i metadane.

- PDF cyfrowy: tekst z warstwy tekstowej pypdfium2 (linie jak w PDF); z ODW_DOCLING=1 dodatkowo
  Docling (struktura, tabele, Markdown) – wolne, a potok z tego nie korzysta.
- PDF ze skanowanymi stronami (wydruk zeskanowany, krzywy, z pieczątkami): odczyt per strona –
  warstwa tekstowa pypdfium2 albo własny OCR linii (wyprostowanie strony, RapidOCR, ponowne
  rozpoznanie słabych fragmentów, złożenie w linie, korekta słownikowa typowych błędów OCR).
  Ten sam odczyt jest awaryjnym, gdy Docling zawiedzie.
"""
from __future__ import annotations

import gc
import logging
import os
import re
import time
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

MIN_TEXT_CHARS = 50  # mniej znaków w warstwie tekstowej => strona traktowana jako skan
PREVIEW_PAGES = 3  # liczba stron w `first_3_pages_markdown`
OCR_RENDER_SCALE = 2.5  # skala renderowania skanu do OCR (~180 DPI; przy 1.5 gubione były polskie znaki)
CPU_THREADS = 4
# Docling (układ strony, tabele, Markdown) dla PDF-ów cyfrowych. Potok korzysta tylko z `text`, który i tak
# pochodzi z warstwy tekstowej pypdfium2, a Docling kosztuje 2–30 s na plik – domyślnie wyłączony.
# Włączenie: ODW_DOCLING=1 (np. gdy potrzebne są `tables` / `markdown` w wyniku).
USE_DOCLING = os.environ.get("ODW_DOCLING", "").strip().lower() in ("1", "true", "yes")

# OCR skanów (strony bez warstwy tekstowej)
DESKEW_MAX_ANGLE = 6.0  # szukany kąt przekrzywienia skanu (stopnie, w obie strony)
DESKEW_STEP = 0.2
DESKEW_MIN_ANGLE = 0.1  # mniejszego przekrzywienia nie prostujemy
RERECOGNIZE_SCORE = 0.85  # linia o niższej pewności OCR jest rozpoznawana ponownie we fragmentach
RERECOGNIZE_MIN_RATIO = 8  # … o ile jest długa (szerokość / wysokość)
CHUNK_MAX_RATIO = 10  # maks. szerokość fragmentu (w wysokościach linii) przy ponownym rozpoznaniu


# --- publiczne API -------------------------------------------------------------

@dataclass(frozen=True)
class PageLayout:
    """Podział stron PDF (numerowanych od 1) na cyfrowe i zeskanowane."""

    total: int
    digital: list[int]
    scanned: list[int]


def resolve_ocr_backend() -> str | None:
    """Wybiera dostępny backend OCR. Obsługiwane jest wyłącznie ONNX Runtime."""
    try:
        import onnxruntime  # type: ignore # noqa: F401
    except ImportError:
        return None
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

    def __init__(self, ocr_enabled: bool = True, ocr_lang: list[str] | None = None, use_docling: bool | None = None):
        self.ocr_enabled = ocr_enabled
        self.ocr_lang = ocr_lang or ["pl"]
        self.use_docling = USE_DOCLING if use_docling is None else use_docling
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

        if self.ocr_enabled and any(p <= last_page for p in layout.scanned):
            # Skany (także odręczne/krzywe): odczyt per strona – warstwa tekstowa albo własny OCR linii
            # (prostowanie strony, linie złożone z boksów RapidOCR). Docling dzieli skan na bloki
            # i kolumny ('Odwołujący:' / 'Zamawiający:' oddzielone od wartości), a reguły exportera
            # opierają się na liniach – jak w warstwie tekstowej dokumentów cyfrowych.
            try:
                result, ocr_applied = self._extract_fallback(path, layout, last_page)
                status, extra = "success", {"method": "ocr_lines"}
            finally:
                gc.collect()
            result["metadata"] = self._metadata(path, layout, result, ocr_applied, start, status, extra)
            return result

        if not self.use_docling:
            # PDF cyfrowy: same linie warstwy tekstowej (ten sam `text`, co ścieżka Docling, bez jej kosztu)
            try:
                result, ocr_applied = self._extract_fallback(path, layout, last_page)
                if result["text"].strip():
                    result["metadata"] = self._metadata(path, layout, result, ocr_applied, start, "success",
                                                        {"method": "text_layer"})
                    return result
            except Exception as exc:
                logger.debug("Odczyt warstwy tekstowej nie powiódł się dla %s (%s) - próba Docling.", path.name, exc)

        try:
            result = self._extract_with_docling(path, (1, last_page) if max_pages else None, layout, last_page)
            ocr_applied = False
            status, extra = "success", {}
        except Exception as exc:
            logger.warning("Błąd Docling dla %s (%s) - uruchamianie fallbacku.", path.name, exc)
            result, ocr_applied = self._extract_fallback(path, layout, last_page)
            status, extra = "fallback", {"warning": str(exc)}
        finally:
            gc.collect()

        result["metadata"] = self._metadata(path, layout, result, ocr_applied, start, status, extra)
        return result

    @staticmethod
    def _metadata(path: Path, layout: PageLayout, result: dict, ocr_applied: bool, start: float,
                  status: str, extra: dict) -> dict[str, Any]:
        return {
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

    # --- Docling ---------------------------------------------------------------

    def _extract_with_docling(
        self, path: Path, page_range: tuple[int, int] | None, layout: PageLayout, last_page: int
    ) -> dict[str, Any]:
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
        result["text"] = _line_text(path, doc, layout, last_page)
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
        """OCR skanu → linie tekstu (jak w warstwie tekstowej PDF); bbox we współrzędnych strony (TOPLEFT).

        1. render w odcieniach szarości i wyprostowanie przekrzywionego skanu,
        2. RapidOCR (detekcja + rozpoznanie fragmentów),
        3. ponowne rozpoznanie długich fragmentów o niskiej pewności w kawałkach
           (wyjustowane linie z szerokimi odstępami bywają odczytane jako szum),
        4. złożenie fragmentów w linie: ta sama wysokość na stronie, kolejność od lewej.
        """
        import numpy as np
        from PIL import Image

        image = page.render(scale=OCR_RENDER_SCALE).to_pil().convert("L")
        try:
            angle = _skew_angle(image)
            if abs(angle) >= DESKEW_MIN_ANGLE:
                rotated = image.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=255)
                image.close()
                image = rotated
            pixels = np.array(image)
        finally:
            image.close()

        engine = self._get_ocr_engine()
        # flagi jawnie: RapidOCR zapamiętuje je z poprzedniego wywołania (_rerecognize wyłącza detekcję)
        ocr = engine(pixels, use_det=True, use_cls=True, use_rec=True)
        if not getattr(ocr, "txts", None) or getattr(ocr, "boxes", None) is None:
            return []

        fragments = []
        for text, box, score in zip(ocr.txts, ocr.boxes, ocr.scores):
            xs = [float(pt[0]) for pt in box]
            ys = [float(pt[1]) for pt in box]
            l, t, r, b = min(xs), min(ys), max(xs), max(ys)
            if score < RERECOGNIZE_SCORE and (r - l) > RERECOGNIZE_MIN_RATIO * (b - t):
                text = self._rerecognize(pixels, (l, t, r, b), text, score)
            if text.strip():
                fragments.append((l, t, r, b, text.strip()))

        lines = []
        for frag in _group_lines(fragments):
            l = min(f[0] for f in frag)
            t = min(f[1] for f in frag)
            r = max(f[2] for f in frag)
            b = max(f[3] for f in frag)
            bbox = {
                "l": l / OCR_RENDER_SCALE, "t": t / OCR_RENDER_SCALE,
                "r": r / OCR_RENDER_SCALE, "b": b / OCR_RENDER_SCALE,
                "coord_origin": "TOPLEFT",
            }
            lines.append((correct_ocr_text(" ".join(f[4] for f in frag)), bbox))
        return lines

    def _rerecognize(self, pixels, box: tuple[float, float, float, float], text: str, score: float) -> str:
        """Fragment linii rozpoznany ponownie w kawałkach (cięcie w przerwach między słowami)."""
        import numpy as np

        l, t, r, b = (int(round(v)) for v in box)
        crop = pixels[max(0, t):b, max(0, l):r]
        if crop.size == 0:
            return text
        texts, scores = [], []
        for start, end in _word_chunks(crop):
            piece = np.pad(crop[:, max(0, start - 4):end + 4], 6, constant_values=255)
            res = self._get_ocr_engine()(piece, use_det=False, use_cls=False, use_rec=True)
            if getattr(res, "txts", None):
                texts.append(res.txts[0])
                scores.append(float(res.scores[0]))
        if texts and sum(scores) / len(scores) > score:
            return " ".join(texts)
        return text

    def _get_ocr_engine(self):
        if self._ocr_engine is None:
            from rapidocr import EngineType, RapidOCR  # type: ignore

            if self.backend != "onnxruntime":
                raise RuntimeError("Brak backendu OCR – zainstaluj onnxruntime.")
            self._ocr_engine = RapidOCR(params={
                **{f"{stage}.engine_type": EngineType.ONNXRUNTIME for stage in ("Det", "Cls", "Rec")},
                # bez limitu ONNX Runtime bierze wszystkie rdzenie – przy kilku procesach main.py to przeciążenie CPU
                "EngineConfig.onnxruntime.intra_op_num_threads": CPU_THREADS,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            })
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


# --- korekta tekstu z OCR ----------------------------------------------------------

_PL_LOWER = "ąćęłńóśźż"
_PL_UPPER = "ĄĆĘŁŃÓŚŹŻ"
# typowe pomyłki OCR w polskim tekście: zgubione (albo dodane) znaki diakrytyczne, 'ł' czytane jako 't',
# 'c'/'e', 'l'/'i', 'ą' jako 'q'
_OCR_CONFUSIONS = {
    "a": "ą", "c": "će", "e": "ęc", "l": "ł", "t": "ł", "n": "ń", "o": "ó", "s": "ś", "z": "żź", "i": "l", "q": "ą",
    "ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n", "ó": "o", "ś": "s", "ź": "zż", "ż": "zź",
    "A": "Ą", "C": "ĆE", "E": "ĘC", "L": "Ł", "N": "Ń", "O": "Ó", "S": "Ś", "Z": "ŻŹ",
    "Ą": "A", "Ć": "C", "Ę": "E", "Ł": "L", "Ń": "N", "Ó": "O", "Ś": "S", "Ź": "ZŻ", "Ż": "ZŹ",
}
_OCR_MAX_EDITS = 2
# słowo (bez adresów e-mail/WWW i skrótów z kropką: 'poz.', 'godz.' – poza słownikiem, a ich warianty już nie)
_WORD_RE = re.compile(r"(?<![\w@.\-/])([^\W\d_]{3,}(?![\w@\-/]|\.)|[^\W\d_]{6,}(?=\.(?!\w)))")


@lru_cache(maxsize=1)
def _morfeusz():
    try:
        from morfeusz2 import Morfeusz  # type: ignore

        return Morfeusz()
    except Exception as exc:  # bez słownika – tylko korekty znakowe
        logger.debug("Morfeusz niedostępny, pomijam korektę słownikową OCR: %s", exc)
        return None


@lru_cache(maxsize=50000)
def _is_known_word(word: str) -> bool:
    morfeusz = _morfeusz()
    return any(item[2][2] != "ign" for item in morfeusz.analyse(word))


@lru_cache(maxsize=20000)
def _correct_word(word: str) -> str:
    """Nieznane słowo → najbliższy wariant ze słownika (≤ 2 podmiany z _OCR_CONFUSIONS); inaczej bez zmian."""
    if _is_known_word(word):
        return word
    positions = [i for i, ch in enumerate(word) if ch in _OCR_CONFUSIONS]
    frontier = [word]
    for _ in range(_OCR_MAX_EDITS):
        variants = []
        for base in frontier:
            for i in positions:
                if base[i] != word[i]:
                    continue
                for repl in _OCR_CONFUSIONS[word[i]]:
                    variant = base[:i] + repl + base[i + 1:]
                    if _is_known_word(variant):
                        return variant
                    variants.append(variant)
        frontier = variants
    return word


def correct_ocr_text(text: str) -> str:
    """Poprawki typowych błędów OCR polskiego tekstu (tylko dla stron skanowanych).

    - litery z obcymi znakami diakrytycznymi ('Brzozowskì', 'Systémy') → litera podstawowa,
    - 'z 0.0.' / 'z o.0.' → 'z o.o.',
    - sklejone słowa ('MiejskiOśrodek', 'wyjaśnieńSWZ') → rozdzielone spacją,
    - słowa spoza słownika (Morfeusz) → wariant ze słownika różniący się ≤ 2 znakami z typowych pomyłek
      ('Odwołujacy' → 'Odwołujący', 'Dotyezy' → 'Dotyczy', 'petnomocnika' → 'pełnomocnika', 'Lódź' → 'Łódź');
      nazwy własne spoza słownika zostają bez zmian.
    """
    chars = []
    for ch in text:
        if ch.isalpha() and ch not in _PL_LOWER + _PL_UPPER and not ch.isascii():
            base = unicodedata.normalize("NFD", ch)[0]
            chars.append(base if base.isascii() else ch)
        else:
            chars.append(ch)
    text = "".join(chars)
    text = re.sub(r"\b([zZ])\s?[0oO]\.\s?[0oO]\.", r"\1 o.o.", text)
    text = re.sub(r"(?<=\w)[·•](?=\w)", " ", text)  # 'OŚRODEK·SPORTU' – odstęp czytany jako kropka środkowa
    # sklejenie: słowo (≥ 3 małe litery) + słowo od wielkiej litery albo skrót wersalikami ('KlO' to błąd OCR 'KIO')
    text = re.sub(rf"(?<=[a-z{_PL_LOWER}]{{3}})(?=[A-Z{_PL_UPPER}](?:[a-z{_PL_LOWER}]|[A-Z{_PL_UPPER}]))", " ", text)
    if _morfeusz() is None:
        return text
    return _WORD_RE.sub(lambda m: _correct_word(m.group(1)), text)


def _skew_angle(image) -> float:
    """Kąt (stopnie) prostujący skan: obrót, przy którym profil poziomy (wiersze tekstu) jest najostrzejszy."""
    import numpy as np
    from PIL import Image

    width, height = image.size
    k = 800 / max(width, height)
    small = image.resize((max(1, int(width * k)), max(1, int(height * k))))
    best_score, best_angle = -1.0, 0.0
    for angle in np.arange(-DESKEW_MAX_ANGLE, DESKEW_MAX_ANGLE + 1e-6, DESKEW_STEP):
        rotated = np.asarray(small.rotate(float(angle), resample=Image.BILINEAR, fillcolor=255))
        profile = (rotated < 128).sum(axis=1).astype(np.float64)
        score = float(np.var(np.diff(profile)))
        if score > best_score:
            best_score, best_angle = score, float(angle)
    return round(best_angle, 2)


def _word_chunks(crop) -> list[tuple[int, int]]:
    """Zakresy kolumn fragmentu linii: słowa (przerwa ≥ ~½ wysokości) łączone w kawałki ≤ CHUNK_MAX_RATIO wysokości."""
    height = crop.shape[0]
    ink = (crop < 140).sum(axis=0) > 0
    words: list[tuple[int, int]] = []
    start = last = None
    for x, has_ink in enumerate(ink):
        if has_ink:
            if start is None:
                start = x
            last = x
        elif start is not None and x - last > 0.45 * height:
            words.append((start, last + 1))
            start = None
    if start is not None:
        words.append((start, last + 1))

    chunks: list[tuple[int, int]] = []
    for a, b in words:
        if chunks and b - chunks[-1][0] <= CHUNK_MAX_RATIO * height:
            chunks[-1] = (chunks[-1][0], b)
        else:
            chunks.append((a, b))
    return chunks


def _group_lines(fragments: list[tuple[float, float, float, float, str]]) -> list[list]:
    """Fragmenty OCR (l, t, r, b, tekst) → linie: środek w pionie bliski środkowi linii, w linii od lewej."""
    lines: list[dict[str, Any]] = []
    for frag in sorted(fragments, key=lambda f: ((f[1] + f[3]) / 2, f[0])):
        center, height = (frag[1] + frag[3]) / 2, frag[3] - frag[1]
        for line in lines:
            overlaps_x = any(frag[0] < f[2] and f[0] < frag[2] for f in line["items"])
            if not overlaps_x and abs(line["center"] - center) < 0.5 * min(height, line["height"]):
                line["items"].append(frag)
                break
        else:
            lines.append({"center": center, "height": height, "items": [frag]})
    lines.sort(key=lambda line: line["center"])
    return [sorted(line["items"], key=lambda f: f[0]) for line in lines]


def _line_text(path: Path, doc, layout: PageLayout, last_page: int) -> str:
    """Tekst z zachowanym podziałem na linie PDF: strony cyfrowe z warstwy tekstowej, skany z OCR Docling.

    `doc.export_to_text()` skleja linie w akapity, a tabele zamienia na Markdown
    (np. 'Hartica Serwis S.A. ul. Akacjowa 54, … KRS: …' w jednej linii, '| Mocodawca | … |').
    Reguły exportera opierają się na liniach (nazwa = pierwsza linia sekcji, data w nagłówku
    na początku linii, podpis = linia z samym imieniem i nazwiskiem), więc dla stron z warstwą
    tekstową bierzemy linie z pypdfium2.
    """
    if not layout.digital:
        return doc.export_to_text()
    try:
        import pypdfium2 as pdfium  # type: ignore

        pages: list[str] = []
        with closing(pdfium.PdfDocument(str(path))) as pdf:
            for page_no, page in _iter_pages(pdf, min(last_page, len(pdf))):
                if page_no in layout.scanned:
                    pages.append(doc.export_to_text(page_no=page_no))
                else:
                    width, height = page.get_size()
                    pages.append("\n".join(line for line, _ in _text_layer_lines(page, width, height)))
        return "\n".join(p for p in pages if p.strip())
    except Exception as exc:
        logger.debug("Odczyt warstwy tekstowej nie powiódł się dla %s: %s", path.name, exc)
        return doc.export_to_text()


def _docling_preview(doc, full_markdown: str) -> str:
    """Markdown pierwszych PREVIEW_PAGES stron (tam zwykle są kluczowe dane)."""
    if len(doc.pages) <= PREVIEW_PAGES:
        return full_markdown
    try:
        parts = [doc.export_to_markdown(page_no=p) for p in sorted(doc.pages) if p <= PREVIEW_PAGES]
    except TypeError:  # starszy Docling bez parametru page_no
        return full_markdown
    return "\n\n".join(parts)
