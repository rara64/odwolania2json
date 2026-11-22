"""odwolania2json – punkt wejścia aplikacji.

Bierze pliki z folderu `input/`, przepuszcza każdy przez wszystkie kroki potoku
i zapisuje wynikowe JSON-y do `output/` w tej samej strukturze drzewa:

    input/<sprawa>/<plik>.pdf  →  output/<sprawa>/<plik>.json

Kroki dla każdego pliku:
    1. data_extraction      – tekst PDF (Docling + RapidOCR, awaryjnie pypdfium2)
    2. document-classifier  – klasa dokumentu: odwolanie / pelnomocnictwo / przelew / None
    3. json-exporter        – wartości pól, normalizacja, pola wyliczane wg `definitions/`
    4. zapis                – JSON do `output/` (dokument negatywny lub błąd → `{}`)

Użycie:
    python main.py                          # ./input → ./output
    python main.py [input] [output] [--report-dir reports] [-v]
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXPORTER_FILE = ROOT / "pipeline" / "json-exporter" / "exporter.py"

logger = logging.getLogger("odwolania2json")


def _load_exporter_module():
    """Katalog `json-exporter` ma myślnik w nazwie, więc moduł ładujemy po ścieżce."""
    spec = importlib.util.spec_from_file_location("json_exporter", EXPORTER_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


exporter_mod = _load_exporter_module()


def find_inputs(input_dir: Path) -> list[Path]:
    """Wszystkie PDF-y z `input/` (rekurencyjnie); inne pliki są pomijane z ostrzeżeniem."""
    pdfs, skipped = [], []
    for path in sorted(p for p in input_dir.rglob("*") if p.is_file()):
        (pdfs if path.suffix.lower() == ".pdf" else skipped).append(path)
    for path in skipped:
        logger.warning("Pominięto (nie PDF): %s", path.relative_to(input_dir))
    return pdfs


def process_file(exporter, pdf: Path, out_path: Path, report_path: Path | None) -> str:
    """Jeden plik przez wszystkie kroki potoku. Zwraca opis wyniku do podsumowania."""
    from pipeline.data_extraction import extract_pdf

    start = time.time()
    report = exporter_mod.Report(source="error")
    data = copy.deepcopy(exporter.defs.negative_output)
    label, outcome = None, "błąd"
    try:
        # 1. ekstrakcja tekstu
        extracted = extract_pdf(pdf)
        text = extracted.get("text") or extracted.get("markdown") or ""
        meta = extracted.get("metadata") or {}
        logger.debug("  [1/4] ekstrakcja: %d znaków, status=%s, OCR=%s",
                     len(text), meta.get("status"), meta.get("ocr_applied"))

        # 2. klasyfikacja
        label = exporter.classify(text)
        logger.debug("  [2/4] klasa: %s", label or "negatywny")

        # 3. budowa JSON wg definicji
        data, report = exporter.build(text, label)
        report.extraction = {k: meta[k] for k in ("filename", "status", "total_pages", "ocr_applied", "warning") if k in meta}
        logger.debug("  [3/4] JSON: źródło=%s, uwagi=%d", report.source, len(report.issues))
        outcome = label or "negatywny"
    except Exception as exc:
        logger.exception("Błąd przetwarzania %s", pdf.name)
        report.add("", f"Błąd przetwarzania: {exc}", "error")

    # 4. zapis (zawsze – błąd/negatywny daje pusty JSON); nazwa pliku wg klasy dokumentu
    out_path = exporter.output_path(out_path, label)
    exporter_mod._write_json(out_path, data)
    if report_path is not None:
        exporter_mod._write_json(report_path, {"file": pdf.name, **report.to_dict()})
    logger.debug("  [4/4] zapisano %s", out_path)

    problems = sum(1 for i in report.issues if i["level"] != "info")
    logger.info("%s → %s [%s, uwagi: %d, %.1fs]", pdf.name, out_path.name, outcome, problems, time.time() - start)
    return outcome


def run(input_dir: Path, output_dir: Path, report_dir: Path | None = None) -> dict[str, int]:
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    pdfs = find_inputs(input_dir)
    if not pdfs:
        logger.warning("Brak plików PDF w %s", input_dir)
        return {}

    exporter = exporter_mod.JsonExporter()
    summary: dict[str, int] = {}
    for n, pdf in enumerate(pdfs, 1):
        rel = pdf.relative_to(input_dir)
        logger.info("(%d/%d) %s", n, len(pdfs), rel)
        out_path = output_dir / rel.with_suffix(".json")
        report_path = report_dir / rel.parent / f"{rel.stem}.report.json" if report_dir else None
        outcome = process_file(exporter, pdf, out_path, report_path)
        summary[outcome] = summary.get(outcome, 0) + 1

    logger.info("Gotowe: %d plików → %s | %s", len(pdfs), output_dir,
                ", ".join(f"{k}: {v}" for k, v in sorted(summary.items())))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="input/*.pdf → output/*.json (odwołanie / pełnomocnictwo / przelew).")
    parser.add_argument("input", nargs="?", default=str(ROOT / "input"), help="katalog wejściowy (domyślnie ./input)")
    parser.add_argument("output", nargs="?", default=str(ROOT / "output"), help="katalog wynikowy (domyślnie ./output)")
    parser.add_argument("--report-dir", help="zapisz raporty ekstrakcji (<plik>.report.json) w tym katalogu")
    parser.add_argument("-v", "--verbose", action="store_true", help="szczegóły każdego kroku")
    args = parser.parse_args(argv)

    if hasattr(sys.stderr, "reconfigure"):  # polskie znaki w logach także przy przekierowaniu na Windows
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    if not args.verbose:  # gadatliwe biblioteki zewnętrzne
        for name in ("docling", "rapidocr", "RapidOCR", "httpx", "huggingface_hub"):
            logging.getLogger(name).setLevel(logging.WARNING)

    input_dir = Path(args.input)
    if input_dir.exists() and not input_dir.is_dir():
        parser.error(f"{input_dir} nie jest katalogiem")
    run(input_dir, Path(args.output), Path(args.report_dir) if args.report_dir else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
