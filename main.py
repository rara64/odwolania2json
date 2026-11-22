"""odwolania2json – punkt wejścia aplikacji.

Bierze pliki z folderu `input/`, przepuszcza każdy przez wszystkie kroki potoku
i zapisuje wynikowe JSON-y do `output/` w tej samej strukturze drzewa:

    input/<sprawa>/<plik>.pdf  →  output/<sprawa>/<plik>.json

Kroki dla każdego pliku:
    1. data_extraction      – tekst PDF (warstwa tekstowa; skany: OCR linii RapidOCR z prostowaniem i korektą)
    2. document-classifier  – klasa dokumentu: odwolanie / pelnomocnictwo / przelew / None
    3. json-exporter        – wartości pól, normalizacja, pola wyliczane wg `definitions/`
    4. zapis                – JSON do `output/` (dokument negatywny lub błąd → `{}`)

Pliki są przetwarzane równolegle w osobnych procesach (OCR i analiza PDF obciążają CPU,
więc wątki/asyncio nic by nie dały przez GIL). Wyniki są zapisywane w kolejności plików
wejściowych, więc wynik jest taki sam jak przy przetwarzaniu po kolei.

Użycie:
    python main.py                          # ./input → ./output
    python main.py [input] [output] [--report-dir reports] [-j N] [-v]
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import logging
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXPORTER_FILE = ROOT / "pipeline" / "json-exporter" / "exporter.py"
MAX_AUTO_JOBS = 4  # każdy proces trzyma własne modele OCR (~0.5–1 GB RAM)

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


def default_jobs() -> int:
    return max(1, min(MAX_AUTO_JOBS, (os.cpu_count() or 1) // 2))


def threads_per_job(jobs: int) -> int:
    return max(1, (os.cpu_count() or 1) // jobs)


def configure_logging(verbose: bool) -> None:
    if hasattr(sys.stderr, "reconfigure"):  # polskie znaki w logach także przy przekierowaniu na Windows
        sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(levelname)s %(message)s")
    if not verbose:  # gadatliwe biblioteki zewnętrzne
        for name in ("docling", "rapidocr", "RapidOCR", "httpx", "huggingface_hub"):
            logging.getLogger(name).setLevel(logging.WARNING)


def set_cpu_threads(threads: int) -> None:
    """Limit wątków bibliotek numerycznych w tym procesie (przed załadowaniem modeli)."""
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ[var] = str(threads)
    from pipeline.data_extraction import extractor

    extractor.CPU_THREADS = threads


def process_file(exporter, pdf: Path) -> tuple[str | None, object, object, str, float]:
    """Kroki 1–3 dla jednego pliku. Zwraca (klasa, dane, raport, opis wyniku, czas)."""
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
        logger.debug("  %s [1/4] ekstrakcja: %d znaków, status=%s, OCR=%s",
                     pdf.name, len(text), meta.get("status"), meta.get("ocr_applied"))

        # 2. klasyfikacja
        label = exporter.classify(text)
        logger.debug("  %s [2/4] klasa: %s", pdf.name, label or "negatywny")

        # 3. budowa JSON wg definicji
        data, report = exporter.build(text, label)
        report.extraction = {k: meta[k] for k in ("filename", "status", "total_pages", "ocr_applied", "warning") if k in meta}
        logger.debug("  %s [3/4] JSON: źródło=%s, uwagi=%d", pdf.name, report.source, len(report.issues))
        outcome = label or "negatywny"
    except Exception as exc:
        logger.exception("Błąd przetwarzania %s", pdf.name)
        report.add("", f"Błąd przetwarzania: {exc}", "error")
    return label, data, report, outcome, time.time() - start


def write_result(exporter, pdf: Path, out_path: Path, report_path: Path | None, label, data, report) -> Path:
    """4. zapis (zawsze – błąd/negatywny daje pusty JSON); nazwa pliku wg klasy dokumentu."""
    out_path = exporter.output_path(out_path, label)
    exporter_mod._write_json(out_path, data)
    if report_path is not None:
        exporter_mod._write_json(report_path, {"file": pdf.name, **report.to_dict()})
    logger.debug("  %s [4/4] zapisano %s", pdf.name, out_path)
    return out_path


# --- procesy robocze --------------------------------------------------------------

_worker_exporter = None


def _init_worker(verbose: bool, threads: int) -> None:
    configure_logging(verbose)
    set_cpu_threads(threads)
    global _worker_exporter
    _worker_exporter = exporter_mod.JsonExporter()


def _work(pdf: Path):
    return process_file(_worker_exporter, pdf)


def run(input_dir: Path, output_dir: Path, report_dir: Path | None = None,
        jobs: int | None = None, verbose: bool = False) -> dict[str, int]:
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    pdfs = find_inputs(input_dir)
    if not pdfs:
        logger.warning("Brak plików PDF w %s", input_dir)
        return {}

    jobs = max(1, min(jobs or default_jobs(), len(pdfs)))
    threads = threads_per_job(jobs)
    logger.info("Plików: %d, procesy: %d × %d wątki CPU", len(pdfs), jobs, threads)
    start = time.time()

    exporter = exporter_mod.JsonExporter()
    summary: dict[str, int] = {}
    done = 0

    def finish(idx: int, result) -> None:
        nonlocal done
        pdf = pdfs[idx]
        rel = pdf.relative_to(input_dir)
        label, data, report, outcome, elapsed = result
        report_path = report_dir / rel.parent / f"{rel.stem}.report.json" if report_dir else None
        out_path = write_result(exporter, pdf, output_dir / rel.with_suffix(".json"), report_path, label, data, report)
        problems = sum(1 for i in report.issues if i["level"] != "info")
        done += 1
        logger.info("(%d/%d) %s → %s [%s, uwagi: %d, %.1fs]",
                    done, len(pdfs), rel, out_path.name, outcome, problems, elapsed)
        summary[outcome] = summary.get(outcome, 0) + 1

    if jobs == 1:
        set_cpu_threads(threads)
        for idx, pdf in enumerate(pdfs):
            finish(idx, process_file(exporter, pdf))
    else:
        # Wyniki zapisywane w kolejności wejścia (bufor), żeby pliki o tej samej nazwie
        # wynikowej (np. dwa przelewy w jednej sprawie) nadpisywały się jak przy pracy sekwencyjnej.
        ready: dict[int, tuple] = {}
        next_idx = 0
        with ProcessPoolExecutor(max_workers=jobs, initializer=_init_worker, initargs=(verbose, threads)) as pool:
            pending = {pool.submit(_work, pdf): idx for idx, pdf in enumerate(pdfs)}
            while pending:
                finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in finished:
                    idx = pending.pop(future)
                    try:
                        ready[idx] = future.result()
                    except Exception as exc:  # np. awaria procesu roboczego (brak pamięci)
                        logger.error("Błąd przetwarzania %s: %s", pdfs[idx].name, exc)
                        report = exporter_mod.Report(source="error")
                        report.add("", f"Błąd przetwarzania: {exc}", "error")
                        ready[idx] = (None, copy.deepcopy(exporter.defs.negative_output), report, "błąd", 0.0)
                while next_idx in ready:
                    finish(next_idx, ready.pop(next_idx))
                    next_idx += 1

    logger.info("Gotowe: %d plików w %.1fs → %s | %s", len(pdfs), time.time() - start, output_dir,
                ", ".join(f"{k}: {v}" for k, v in sorted(summary.items())))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="input/*.pdf → output/*.json (odwołanie / pełnomocnictwo / przelew).")
    parser.add_argument("input", nargs="?", default=str(ROOT / "input"), help="katalog wejściowy (domyślnie ./input)")
    parser.add_argument("output", nargs="?", default=str(ROOT / "output"), help="katalog wynikowy (domyślnie ./output)")
    parser.add_argument("--report-dir", help="zapisz raporty ekstrakcji (<plik>.report.json) w tym katalogu")
    parser.add_argument("-j", "--jobs", type=int, default=None,
                        help=f"liczba równoległych procesów (domyślnie {default_jobs()}; 1 = sekwencyjnie)")
    parser.add_argument("-v", "--verbose", action="store_true", help="szczegóły każdego kroku")
    args = parser.parse_args(argv)
    if args.jobs is not None and args.jobs < 1:
        parser.error("--jobs musi być ≥ 1")

    configure_logging(args.verbose)
    input_dir = Path(args.input)
    if input_dir.exists() and not input_dir.is_dir():
        parser.error(f"{input_dir} nie jest katalogiem")
    run(input_dir, Path(args.output), Path(args.report_dir) if args.report_dir else None,
        jobs=args.jobs, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
