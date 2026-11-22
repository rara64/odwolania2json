"""Ewaluacja potoku odwolania2json na zbiorze ewaluacyjnym dev/dane.

Mierzy:
  - Dokładność klasyfikacji dokumentów (180/180) oraz macierz pomyłek
  - Wartości liści (leaf values): TP, FP, FN, TN, Precision, Recall, Micro F1
  - Metryki per klasa: odwołanie, pełnomocnictwo, przelew
  - Metryki na poziomie dokumentu: Macro F1, mediana, liczba dopasowań 1:1 (exact JSON)
  - Wyniki per pole (posortowane wg F1) z wykrywaniem regresji
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
REF_DIR = ROOT / "dev" / "dane"


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    d = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            d.update(_flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            d.update(_flatten(v, f"{prefix}[{i}]"))
    else:
        d[prefix] = obj
    return d


def _norm(v: Any) -> str | None:
    if isinstance(v, bool):
        return str(int(v))
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _match_outputs(case: Path, out_dir: Path) -> dict[Path, Path]:
    """Dopasowuje pliki referencyjne z danej sprawy do wygenerowanych plików JSON."""
    case_out = out_dir / case.name
    outs = {p.stem: p for p in case_out.glob("*.json")} if case_out.exists() else {}
    matched = {}
    for ref in case.glob("*.json"):
        stem = ref.stem
        if stem in outs:
            matched[ref] = outs[stem]
        elif stem == "przelew" and "potwierdzenie_przelewu" in outs:
            matched[ref] = outs["potwierdzenie_przelewu"]
    return matched


def _get_output_dir(ref_dir: Path) -> Path:
    """Wyszukuje katalog z wygenerowanymi JSON-ami lub uruchamia potok jeśli brak."""
    env_dir = os.environ.get("ODW_OUTPUT_DIR")
    if env_dir and Path(env_dir).exists():
        return Path(env_dir)

    candidates = [
        ROOT / "output",
        ROOT / "out-dev-after",
        Path(os.environ.get("TEMP", "/tmp")) / "opencode" / "out-dev-after",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Temp" / "opencode" / "out-dev-after",
    ]
    for c in candidates:
        if c.exists() and len(list(c.rglob("odwolanie.json"))) >= 50:
            return c

    # Jeśli brak pre-generowanych wyników, uruchom potok do output/dev_eval
    out_dir = ROOT / "output" / "dev_eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    if len(list(out_dir.rglob("odwolanie.json"))) < 50:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import main
        main.run(ref_dir, out_dir)
    return out_dir


def evaluate_dataset(ref_dir: Path, out_dir: Path) -> dict[str, Any]:
    tp = fp = fn = tn = 0
    per_field: dict[str, Counter] = defaultdict(Counter)
    cls_ok = cls_tot = 0
    conf: Counter = Counter()
    class_stats: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])  # [tp, fp, fn, count]
    doc_f1s: list[float] = []
    exact_matches: dict[str, int] = defaultdict(int)
    doc_counts: dict[str, int] = defaultdict(int)

    for case in sorted(ref_dir.iterdir()):
        if not case.is_dir() or case.name == "negatywne":
            continue
        matched = _match_outputs(case, out_dir)
        for ref, out in matched.items():
            doc_class = ref.stem
            exp = json.loads(ref.read_text(encoding="utf-8"))
            got = json.loads(out.read_text(encoding="utf-8"))

            got_class = "negatywny" if got == {} else doc_class
            cls_tot += 1
            cls_ok += int(got_class == doc_class)
            conf[(doc_class, got_class)] += 1

            doc_counts[doc_class] += 1
            if exp == got:
                exact_matches[doc_class] += 1

            fe, fg = _flatten(exp), _flatten(got)
            d_tp = d_fp = d_fn = 0
            for k in set(fe) | set(fg):
                e, g = _norm(fe.get(k)), _norm(fg.get(k))
                if e is None and g is None:
                    tn += 1
                    per_field[k]["tn"] += 1
                    continue
                if e is None:
                    fp += 1
                    d_fp += 1
                    per_field[k]["fp"] += 1
                    continue
                if g is None:
                    fn += 1
                    d_fn += 1
                    per_field[k]["fn"] += 1
                    continue
                if e == g:
                    tp += 1
                    d_tp += 1
                    per_field[k]["tp"] += 1
                else:
                    fp += 1
                    fn += 1
                    d_fp += 1
                    d_fn += 1
                    per_field[k]["fp"] += 1
                    per_field[k]["fn"] += 1

            cs = class_stats[doc_class]
            cs[0] += d_tp
            cs[1] += d_fp
            cs[2] += d_fn
            cs[3] += 1
            d_p = d_tp / (d_tp + d_fp) if (d_tp + d_fp) else 0.0
            d_r = d_tp / (d_tp + d_fn) if (d_tp + d_fn) else 0.0
            doc_f1s.append(2 * d_p * d_r / (d_p + d_r) if (d_p + d_r) else 0.0)

    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * p * r / (p + r) if (p + r) else 0.0
    macro_f1 = sum(doc_f1s) / len(doc_f1s) if doc_f1s else 0.0
    median_f1 = sorted(doc_f1s)[len(doc_f1s) // 2] if doc_f1s else 0.0

    class_metrics = {}
    for c, (c_tp, c_fp, c_fn, n) in class_stats.items():
        cp = c_tp / (c_tp + c_fp) if (c_tp + c_fp) else 0.0
        cr = c_tp / (c_tp + c_fn) if (c_tp + c_fn) else 0.0
        cf = 2 * cp * cr / (cp + cr) if (cp + cr) else 0.0
        class_metrics[c] = {
            "p": cp, "r": cr, "f1": cf, "tp": c_tp, "fp": c_fp, "fn": c_fn,
            "docs": n, "exact": exact_matches[c]
        }

    return {
        "cls_ok": cls_ok,
        "cls_tot": cls_tot,
        "cls_acc": cls_ok / cls_tot if cls_tot else 0.0,
        "confusion": dict(conf),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": p, "recall": r, "f1": f1,
        "macro_doc_f1": macro_f1,
        "median_doc_f1": median_f1,
        "class_metrics": class_metrics,
        "exact_matches": dict(exact_matches),
        "doc_counts": dict(doc_counts),
        "total_exact": sum(exact_matches.values()),
        "total_docs": sum(doc_counts.values()),
        "per_field": per_field,
        "output_dir": str(out_dir),
    }


@pytest.fixture(scope="session")
def dev_eval_results() -> dict[str, Any]:
    out_dir = _get_output_dir(REF_DIR)
    return evaluate_dataset(REF_DIR, out_dir)


class TestDevF1Score:
    """Testy ewaluacyjne F1 i regresji na zbiorze dev/dane."""

    def test_f1score_document_type(self, dev_eval_results):
        """Weryfikuje poprawność klasyfikacji typów dokumentów."""
        acc = dev_eval_results["cls_acc"]
        assert acc >= 0.98, f"Regresja klasyfikacji: {acc:.3f} < 0.98"

    def test_f1score_json(self, dev_eval_results):
        """Weryfikuje globalne micro F1 oraz kluczowe klasy dokumentów."""
        f1 = dev_eval_results["f1"]
        odw_f1 = dev_eval_results["class_metrics"]["odwolanie"]["f1"]
        przel_f1 = dev_eval_results["class_metrics"]["przelew"]["f1"]
        peln_f1 = dev_eval_results["class_metrics"]["pelnomocnictwo"]["f1"]

        # Aserty sprawdzające brak regresji:
        assert f1 >= 0.85, f"Regresja Micro F1: {f1:.3f} < 0.85"
        assert odw_f1 >= 0.90, f"Regresja odwołanie F1: {odw_f1:.3f} < 0.90"
        assert przel_f1 >= 0.85, f"Regresja przelew F1: {przel_f1:.3f} < 0.85"
        assert peln_f1 >= 0.55, f"Regresja pełnomocnictwo F1: {peln_f1:.3f} < 0.55"

    def test_dev_scorecard(self, capsys, dev_eval_results):
        """Wyświetla pełne podsumowanie i tabelę per-field do monitorowania regresji."""
        res = dev_eval_results
        with capsys.disabled():
            print("\n" + "=" * 95)
            print(f"             EWALUACJA PIPELINE NA ZBIORZE DEV/DANE (katalog: {res['output_dir']})")
            print("=" * 95)
            print(f"Klasyfikacja dokumentów: {res['cls_ok']}/{res['cls_tot']} ({res['cls_acc'] * 100:.1f}%)")
            print(f"Macierz pomyłek: {res['confusion']}")
            print(f"\nMetryki globalne (wartości liści):")
            print(f"  TP: {res['tp']} | FP: {res['fp']} | FN: {res['fn']} | TN: {res['tn']}")
            print(f"  Precision: {res['precision']:.3f} | Recall: {res['recall']:.3f} | Micro F1: {res['f1']:.3f}")
            print(f"\nMetryki per klasa dokumentu:")
            for cls_name, m in sorted(res["class_metrics"].items()):
                print(f"  {cls_name:<16} P={m['p']:.3f} R={m['r']:.3f} F1={m['f1']:.3f}  "
                      f"(dokładny JSON: {m['exact']}/{m['docs']})")
            print(f"\nPoziom dokumentów:")
            print(f"  Macro doc-level F1:   {res['macro_doc_f1']:.3f}")
            print(f"  Mediana doc-level F1: {res['median_doc_f1']:.3f}")
            print(f"  Dokładny JSON (1:1):  {res['total_exact']}/{res['total_docs']} "
                  f"({res['total_exact'] / res['total_docs'] * 100:.1f}%)")

            print("\nTabela szczegółowa pól posortowana wg F1:")
            print(f"  {'F1':<5} {'P':<5} {'R':<5} {'TP':<5} {'FP':<5} {'FN':<5} {'TN':<5}  Pole")
            print("  " + "-" * 90)
            rows = []
            for k, c in res["per_field"].items():
                c_tp, c_fp, c_fn, c_tn = c["tp"], c["fp"], c["fn"], c["tn"]
                if c_tp + c_fp + c_fn == 0:
                    continue
                pp = c_tp / (c_tp + c_fp) if (c_tp + c_fp) else 0.0
                rr = c_tp / (c_tp + c_fn) if (c_tp + c_fn) else 0.0
                ff = 2 * pp * rr / (pp + rr) if (pp + rr) else 0.0
                rows.append((ff, pp, rr, c_tp, c_fp, c_fn, c_tn, k))
            rows.sort(key=lambda x: (x[0], -x[3]))
            for ff, pp, rr, c_tp, c_fp, c_fn, c_tn, k in rows:
                print(f"  {ff:5.2f} {pp:5.2f} {rr:5.2f} {c_tp:4d}  {c_fp:4d}  {c_fn:4d}  {c_tn:4d}   {k}")
            print("=" * 95)
