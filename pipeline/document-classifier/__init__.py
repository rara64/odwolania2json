"""Klasyfikator dokumentów spraw odwoławczych KIO – tekst → nazwa klasy (reguły z wagami, bez ML).

Użycie:
    from classifier import classify

    classify(text)   # -> "odwolanie" | "pelnomocnictwo" | "przelew" | None (dokument negatywny)

`text` to tekst dokumentu wyciągnięty wcześniej (warstwa tekstowa PDF albo OCR).
Reguły i wagi są w pliku rules.yaml obok (nową klasę dodaje się tylko tam).
Pliki: classifier/__init__.py + classifier/rules.yaml. Zależność: PyYAML.

Pomocniczo:
    explain(text)    # opis decyzji: dopasowane reguły z wagami i fragmentami tekstu
    classes()        # lista klas zdefiniowanych w rules.yaml
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

__all__ = ["classify", "explain", "classes"]

RULES_FILE = Path(__file__).resolve().parent / "rules.yaml"


# --- publiczne API -------------------------------------------------------------

def classify(text: str, rules_file: str | Path | None = None) -> str | None:
    """Zwraca nazwę klasy dokumentu ("odwolanie", "pelnomocnictwo", "przelew") albo None."""
    return _engine(_rules_path(rules_file)).classify(text).label


def explain(text: str, rules_file: str | Path | None = None) -> str:
    """Opis decyzji klasyfikatora (do diagnostyki): wynik i dopasowane reguły dla każdej klasy."""
    return _engine(_rules_path(rules_file)).classify(text).explain()


def classes(rules_file: str | Path | None = None) -> list[str]:
    """Lista klas zdefiniowanych w pliku reguł."""
    return list(_engine(_rules_path(rules_file)).classes)


# --- silnik reguł --------------------------------------------------------------

def _rules_path(rules_file: str | Path | None) -> str:
    return str(Path(rules_file).resolve()) if rules_file else str(RULES_FILE)


@lru_cache(maxsize=8)
def _engine(rules_path: str) -> "_Engine":
    return _Engine(rules_path)  # reguły wczytywane raz i buforowane


@dataclass
class _Rule:
    name: str
    regex: re.Pattern
    weight: float
    required: bool = False
    scope: int | None = None  # szukaj tylko w pierwszych N znakach (sygnały z nagłówka dokumentu)

    @classmethod
    def from_dict(cls, d: dict) -> "_Rule":
        flags = re.MULTILINE | (0 if d.get("case_sensitive") else re.IGNORECASE)
        return cls(
            name=d.get("name", d["pattern"]),
            regex=re.compile(d["pattern"], flags),
            weight=float(d["weight"]),
            required=bool(d.get("required", False)),
            scope=int(d["zakres"]) if d.get("zakres") else None,
        )


@dataclass
class _Score:
    label: str
    score: float = 0.0
    matched: list = field(default_factory=list)  # (reguła, waga, fragment)
    has_required: bool = True
    eligible: bool = False


@dataclass
class _Result:
    label: str | None
    reason: str
    scores: dict

    def explain(self) -> str:
        lines = [f"klasa: {self.label or 'BRAK (negatywny)'} – {self.reason}"]
        for s in sorted(self.scores.values(), key=lambda s: -s.score):
            flag = "" if s.has_required else " [brak reguły wymaganej]"
            lines.append(f"  {s.label:15s} {s.score:6.1f}{flag}")
            lines += [f"      {w:+5.1f}  {n}  «{f}»" for n, w, f in s.matched]
        return "\n".join(lines)


class _Engine:
    def __init__(self, rules_path: str):
        cfg = yaml.safe_load(Path(rules_path).read_text(encoding="utf-8"))
        g = cfg.get("global", {})
        self.max_chars = int(g.get("max_chars", 0)) or None
        self.min_margin = float(g.get("min_margin", 0))
        self.min_text_chars = int(g.get("min_text_chars", 0))
        self.common_negative = [_Rule.from_dict(r) for r in cfg.get("common_negative", [])]
        self.classes = {
            label: (float(c.get("min_score", 0)), [_Rule.from_dict(r) for r in c["rules"]])
            for label, c in cfg["classes"].items()
        }

    def classify(self, text: str) -> _Result:
        text = (text or "").replace(" ", " ")
        if self.max_chars:
            text = text[: self.max_chars]
        if len(re.sub(r"\s", "", text)) < self.min_text_chars:
            return _Result(None, "za mało tekstu", {})

        scores = {}
        for label, (min_score, rules) in self.classes.items():
            s = _Score(label)
            required_missing = any(r.required for r in rules)
            for rule in rules + self.common_negative:
                m = rule.regex.search(text[: rule.scope] if rule.scope else text)
                if m:
                    s.score += rule.weight
                    s.matched.append((rule.name, rule.weight, " ".join(m.group(0).split())[:60]))
                    if rule.required:
                        required_missing = False
            s.has_required = not required_missing
            s.eligible = s.has_required and s.score >= min_score
            scores[label] = s

        ranked = sorted(scores.values(), key=lambda s: -s.score)
        best = ranked[0]
        second = ranked[1].score if len(ranked) > 1 else float("-inf")
        if not best.eligible:
            return _Result(None, f"najlepsza klasa '{best.label}' poniżej progu lub bez reguły wymaganej", scores)
        if best.score - second < self.min_margin:
            return _Result(None, f"zbyt mała przewaga '{best.label}' nad drugą klasą", scores)
        return _Result(best.label, f"wynik {best.score:.1f}, przewaga {best.score - second:.1f}", scores)
