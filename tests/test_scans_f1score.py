"""Ewaluacja potoku na skanach z dev/dane/skany/<rodzaj>/<nr>/<nr>.pdf + <nr>.json.

Skany (wydruki zeskanowane, krzywe, z pieczątkami i podpisami) nie mają warstwy tekstowej –
tekst pochodzi z OCR. Katalog jest poza repozytorium (.gitignore), więc bez niego testy są pomijane.

Wyniki potoku: katalog z ODW_SCAN_OUTPUT_DIR albo output/skany_eval (generowany przy pierwszym
uruchomieniu – OCR ~10–40 s na dokument).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.test_dev_f1score import _flatten, _norm

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIR = ROOT / "dev" / "dane" / "skany"
KIND = {"odwolania": "odwolanie", "pelnomocnictwa": "pelnomocnictwo", "potwierdzenia": "przelew"}

pytestmark = pytest.mark.skipif(not SCAN_DIR.is_dir(), reason="brak katalogu dev/dane/skany")


def _references() -> list[Path]:
    return sorted(SCAN_DIR.glob("*/*/*.json"))


def _output_dir() -> Path:
    env_dir = os.environ.get("ODW_SCAN_OUTPUT_DIR")
    if env_dir and Path(env_dir).exists():
        return Path(env_dir)
    out_dir = ROOT / "output" / "skany_eval"
    if len(list(out_dir.rglob("*.json"))) < len(_references()):
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        import main
        main.run(SCAN_DIR, out_dir)
    return out_dir


@pytest.fixture(scope="session")
def scan_results() -> dict[str, Any]:
    out_dir = _output_dir()
    tp = fp = fn = cls_ok = 0
    docs = []
    for ref in _references():
        rel = ref.parent.relative_to(SCAN_DIR)
        outs = list((out_dir / rel).glob("*.json"))
        got = json.loads(outs[0].read_text(encoding="utf-8")) if len(outs) == 1 else {}
        expected_class = KIND[rel.parts[0]]
        got_class = outs[0].stem if len(outs) == 1 and got else "negatywny"
        cls_ok += got_class == expected_class
        fe, fg = _flatten(json.loads(ref.read_text(encoding="utf-8"))), _flatten(got)
        diffs = []
        for key in sorted(set(fe) | set(fg)):
            e, g = _norm(fe.get(key)), _norm(fg.get(key))
            if e is None and g is None:
                continue
            if e == g:
                tp += 1
                continue
            fp += g is not None
            fn += e is not None
            diffs.append((key, e, g))
        docs.append((rel.as_posix(), got_class, diffs))
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {
        "docs": docs, "cls_ok": cls_ok, "tp": tp, "fp": fp, "fn": fn,
        "precision": p, "recall": r, "f1": 2 * p * r / (p + r) if p + r else 0.0,
    }


class TestScans:
    def test_scan_classification(self, scan_results):
        wrong = [(name, cls) for name, cls, _ in scan_results["docs"] if KIND[name.split("/")[0]] != cls]
        assert not wrong, f"Błędna klasyfikacja skanów: {wrong}"

    def test_scan_f1(self, scan_results):
        assert scan_results["f1"] >= 0.95, f"Regresja F1 na skanach: {scan_results['f1']:.3f} < 0.95"

    def test_scan_scorecard(self, capsys, scan_results):
        res = scan_results
        with capsys.disabled():
            print(f"\nSKANY: klasyfikacja {res['cls_ok']}/{len(res['docs'])} | TP={res['tp']} FP={res['fp']} "
                  f"FN={res['fn']} | P={res['precision']:.3f} R={res['recall']:.3f} F1={res['f1']:.3f}")
            for name, _cls, diffs in res["docs"]:
                for key, e, g in diffs:
                    print(f"  {name:22s} {key}: {e!r} → {g!r}")
