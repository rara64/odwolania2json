"""Porównuje pliki JSON o tej samej ścieżce względnej w dwóch drzewach.

Domyślnie: input/ (oczekiwane) vs output/ (wygenerowane).

Użycie:
    python compare.py                                   # całe drzewo input/ vs output/
    python compare.py przyklad_01                       # jedna sprawa
    python compare.py przyklad_01/odwolanie.json        # jeden plik
    python compare.py input/przyklad_01/odwolanie.json  # prefiks input/ jest pomijany
    python compare.py przyklad_0* -q                    # tylko podsumowanie
    python compare.py -a dev/dane -b output przyklad_01 # inne korzenie
    python compare.py --strict ...                      # bez normalizacji (1 == true, 5.0 == 5)

Kod wyjścia: 0 gdy wszystko zgodne, 1 gdy są różnice lub brakujące pliki.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
MISSING = object()

for _stream in (sys.stdout, sys.stderr):  # polskie znaki także przy przekierowaniu (cp1252)
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
if _COLOR and os.name == "nt":
    os.system("")  # włącza sekwencje ANSI w konsoli Windows


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict) and obj:
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list) and obj:
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix or "<root>"] = obj
    return out


def norm(v: Any) -> Any:
    """Ta sama normalizacja co w tests/test_dev_f1score.py."""
    if isinstance(v, bool):
        return str(int(v))
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def fmt(v: Any) -> str:
    return "<brak>" if v is MISSING else json.dumps(v, ensure_ascii=False)


def compare_files(a: Path, b: Path, strict: bool) -> list[tuple[str, Any, Any]]:
    fa = flatten(json.loads(a.read_text(encoding="utf-8")))
    fb = flatten(json.loads(b.read_text(encoding="utf-8")))
    diffs = []
    for key in list(fa) + [k for k in fb if k not in fa]:
        va, vb = fa.get(key, MISSING), fb.get(key, MISSING)
        if va is MISSING or vb is MISSING:
            equal = False
        else:
            equal = va == vb if strict else norm(va) == norm(vb)
        if not equal:
            diffs.append((key, va, vb))
    return diffs


def resolve_targets(patterns: list[str], left: Path) -> list[Path]:
    """Zamienia argumenty na listę ścieżek względnych (wobec `left`) do plików .json."""
    if not patterns:
        patterns = ["."]
    rels: list[Path] = []
    for pat in patterns:
        p = Path(pat)
        # pozwala podać ścieżkę z prefiksem korzenia, np. input/przyklad_01/...
        try:
            p = (Path.cwd() / p).resolve().relative_to(left)
        except ValueError:
            pass
        matches = [Path(m) for m in glob.glob(str(left / p))] or [left / p]
        for m in matches:
            if m.is_dir():
                rels += sorted(f.relative_to(left) for f in m.rglob("*.json"))
            else:
                rels.append(m.relative_to(left) if m.is_absolute() else m)
    seen, uniq = set(), []
    for r in rels:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return uniq


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="pliki/katalogi/wzorce względem korzenia A (domyślnie całe drzewo)")
    ap.add_argument("-a", "--left", default="input", help="korzeń A – oczekiwane (domyślnie: input)")
    ap.add_argument("-b", "--right", default="output", help="korzeń B – porównywane (domyślnie: output)")
    ap.add_argument("-q", "--quiet", action="store_true", help="tylko status per plik, bez listy różnic")
    ap.add_argument("--strict", action="store_true", help="porównanie dokładne, bez normalizacji wartości")
    ap.add_argument("--skip-missing", action="store_true", help="pomija pliki, których brak w B")
    args = ap.parse_args()

    left = (Path.cwd() / args.left).resolve()
    right = (Path.cwd() / args.right).resolve()
    if not left.is_dir():
        print(f"Brak katalogu A: {left}", file=sys.stderr)
        return 2

    targets = resolve_targets(args.paths, left)
    if not targets:
        print("Nie znaleziono plików .json do porównania.", file=sys.stderr)
        return 2

    n_ok = n_diff = n_missing = 0
    for rel in targets:
        a, b = left / rel, right / rel
        label = rel.as_posix()
        if not a.exists():
            print(f"{_c('BRAK A', '33')}  {label}")
            n_missing += 1
            continue
        if not b.exists():
            if not args.skip_missing:
                print(f"{_c('BRAK B', '33')}  {label}")
                n_missing += 1
            continue
        try:
            diffs = compare_files(a, b, args.strict)
        except json.JSONDecodeError as e:
            print(f"{_c('BŁĄD', '31')}    {label}: niepoprawny JSON ({e})")
            n_diff += 1
            continue
        if not diffs:
            print(f"{_c('OK', '32')}      {label}")
            n_ok += 1
            continue
        n_diff += 1
        print(f"{_c('RÓŻNE', '31')}   {label}  ({len(diffs)} pól)")
        if not args.quiet:
            width = max(len(k) for k, _, _ in diffs)
            for key, va, vb in diffs:
                print(f"    {key:<{width}}  {_c(fmt(va), '32')}  →  {_c(fmt(vb), '31')}")

    total = n_ok + n_diff + n_missing
    print(f"\n{n_ok}/{total} zgodnych, {n_diff} różnych, {n_missing} brakujących  "
          f"(A={os.path.relpath(left)}, B={os.path.relpath(right)})")
    return 0 if n_diff == n_missing == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
