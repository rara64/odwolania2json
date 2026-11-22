"""JSON exporter – PDF → JSON zgodny z definicjami z katalogu `definitions/`.

Potok dla jednego pliku:
    data_extraction (tekst PDF) → document-classifier (klasa) → wartości pól → normalizacja
    (formats.yaml) → pola wyliczane (derivations.yaml) → JSON w kolejności kluczy z
    field_definitions.yaml. Dokument spoza klas → `negative_output` z documents.yaml (`{}`).

Skąd biorą się wartości pól:
    Reguły ekstrakcji – wartości są wyszukiwane w tekście dokumentu przy etykietach z `hints`
    i dopasowywane wzorcami `capture` z definicji (formats.yaml, field_definitions.yaml).
    Sekcje dokumentu (np. 'Odwołujący:', 'Zamawiający:') wyznaczają fragmenty tekstu dla
    obiektów i elementów tablic.

Użycie (CLI):
    python pipeline/json-exporter/exporter.py [input] [output] [--report-dir DIR]

    input  – katalog (domyślnie ./input, przetwarzany rekurencyjnie) albo pojedynczy PDF
    output – katalog wynikowy (domyślnie ./output); input/<sprawa>/x.pdf → output/<sprawa>/x.json

Użycie (kod):
    exporter = JsonExporter()
    exporter.export_tree("input", "output")
    data, report = exporter.build(text, "przelew")      # z gotowego tekstu i klasy
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import importlib.util
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFINITIONS_DIR = ROOT / "definitions"
CLASSIFIER_FILE = ROOT / "pipeline" / "document-classifier" / "__init__.py"

if str(ROOT) not in sys.path:  # uruchomienie jako skrypt
    sys.path.insert(0, str(ROOT))


# --- definicje -------------------------------------------------------------------

@dataclass
class Definitions:
    """Zawartość plików z `definitions/`."""

    documents: dict
    fields: dict
    formats: dict
    lookups: dict
    derivations: dict
    negative_output: Any = field(default_factory=dict)

    @classmethod
    def load(cls, directory: str | Path = DEFINITIONS_DIR) -> "Definitions":
        directory = Path(directory)

        def read(name: str) -> dict:
            return yaml.safe_load((directory / name).read_text(encoding="utf-8")) or {}

        documents = read("documents.yaml")
        return cls(
            documents=documents.get("documents") or {},
            negative_output=documents.get("negative_output", {}),
            fields=read("field_definitions.yaml").get("documents") or {},
            formats=read("formats.yaml").get("formats") or {},
            lookups=read("lookups.yaml").get("lookups") or {},
            derivations=read("derivations.yaml").get("documents") or {},
        )

    def format_of(self, node: dict) -> dict:
        return self.formats.get(node.get("type"), {})


def _is_container(node: dict) -> bool:
    return node.get("type") in ("object", "array")


# --- raport (transparentność ekstrakcji) -------------------------------------------

@dataclass
class Report:
    document_class: str | None = None
    source: str = ""
    issues: list[dict] = field(default_factory=list)
    extraction: dict = field(default_factory=dict)

    def add(self, path: str, message: str, level: str = "warning", value: Any = None) -> None:
        issue = {"path": path, "level": level, "message": message}
        if value is not None:
            issue["value"] = value
        self.issues.append(issue)
        logger.debug("%s %s: %s", level, path, message)

    def to_dict(self) -> dict:
        return {
            "document_class": self.document_class,
            "source": self.source,
            "extraction": self.extraction,
            "issues": self.issues,
        }


# --- normalizacja (formats.yaml) ---------------------------------------------------

_MONTHS = {
    "stycz": 1, "lut": 2, "mar": 3, "kwie": 4, "maj": 5, "czerw": 6, "lip": 7,
    "sierp": 8, "wrze": 9, "paźdz": 10, "pazdz": 10, "listop": 11, "grud": 12,
}
_TITLES_RE = re.compile(
    r"(?<!\w)(adw\.|adwokat|r\.\s?pr\.|radca\s+prawny|radcy\s+prawnego|mec\.|dr\s+hab\.|dr\.?|prof\.|mgr\.?|inż\.)(?!\w)",
    re.IGNORECASE,
)

_MORFEUSZ = None


def _get_morfeusz():
    global _MORFEUSZ
    if _MORFEUSZ is None:
        from morfeusz2 import Morfeusz
        _MORFEUSZ = Morfeusz()
    return _MORFEUSZ


def _morfeusz_lemma(interpretation: tuple) -> str:
    lemma = interpretation[1]
    return lemma.split(":", 1)[0]


def _nominative_word(word: str) -> str:
    morfeusz = _get_morfeusz()
    candidates = morfeusz.analyse(word)
    named = []
    for interpretation in candidates:
        data = interpretation[2]
        if not isinstance(data, tuple) or len(data) < 4:
            continue
        orth, _lemma_field, tag, labels = data[:4]
        if not orth:
            continue
        lemma = _morfeusz_lemma(data)
        if not lemma:
            continue
        name_label = "imię" in labels or "nazwisko" in labels
        if name_label:
            named.append((data, lemma, name_label))

    if not named:
        return word

    def is_nominative(data: tuple) -> bool:
        tag = data[2]
        return ":nom" in tag and ":sg:" in tag

    def is_pure_oblique(data: tuple) -> bool:
        tag = data[2]
        return ":nom" not in tag and ":voc" not in tag

    oblique = [
        item for item in named
        if is_pure_oblique(item[0])
        and ("imię" in item[0][3] or "nazwisko" in item[0][3])
    ]
    nominative = [item for item in named if is_nominative(item[0])]

    # Prefer an explicit nominative first name. It prevents names already in
    # mianownik from being replaced because of a second, ambiguous interpretation.
    for data, _lemma, _ in nominative:
        if "imię" in data[3]:
            strong_oblique = [
                item for item in oblique
                if "imię" in item[0][3]
                and item[0][2].startswith("subst:")
            ]
            # In an ambiguous female nominative (e.g. Aleksandra), keep the
            # nominative form instead of choosing a masculine oblique lemma.
            if not strong_oblique or ":f" in data[2]:
                return data[0]

    if oblique:
        def oblique_rank(item: tuple) -> tuple:
            data, _lemma, _name_label = item
            tag = data[2]
            return (
                "imię" in data[3],
                tag.startswith("subst:"),
                ":gen.acc:" in tag,
            )

        data, lemma, _ = max(oblique, key=oblique_rank)
        tag = data[2]
        if ":nom" in tag or ":voc" in tag:
            return data[0]
    elif nominative:
        return max(nominative, key=lambda item: item[0][2].startswith("subst:"))[0][0]
    else:
        return word

    lemma_candidates = morfeusz.analyse(lemma)
    nominative = []
    for interpretation in lemma_candidates:
        lemma_data = interpretation[2]
        if not isinstance(lemma_data, tuple) or len(lemma_data) < 4:
            continue
        lemma_tag = lemma_data[2]
        if ":nom" in lemma_tag and (
            lemma_data[0].casefold() == lemma.casefold()
            or "imię" in lemma_data[3]
            or "nazwisko" in lemma_data[3]
        ):
            nominative.append(lemma_data)
    if not nominative:
        return lemma
    return max(nominative, key=lambda data: data[2].startswith("subst:"))[0]


def _nominative_person_name(text: str, _arg: Any = None) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    if not text:
        return text

    def inflect_part(part: str) -> str:
        pieces = part.split("-")
        return "-".join(_nominative_word(piece) for piece in pieces)

    return " ".join(inflect_part(word) for word in text.split(" "))


def _date_to_iso(text: str, _arg: Any = None) -> str:
    candidates = []
    if m := re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text):
        candidates.append((m.group(1), m.group(2), m.group(3)))
    if m := re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", text):
        candidates.append((m.group(3), m.group(2), m.group(1)))
    if m := re.search(r"(\d{1,2})\s+([^\W\d_]+)\.?\s+(\d{4})", text):
        month = next((n for stem, n in _MONTHS.items() if m.group(2).lower().startswith(stem)), None)
        if month:
            candidates.append((m.group(3), month, m.group(1)))
    for year, month, day in candidates:
        try:
            return dt.date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            continue
    return text


def _remove_thousand_separators(text: str, _arg: Any = None) -> str:
    text = re.sub(r"(?<=\d)[\s\u00a0'](?=\d{3}\b)", "", text)
    if "," in text:  # 1.200,50 → 1200,50
        text = re.sub(r"(?<=\d)\.(?=\d{3}\b)", "", text)
    return text


def _regex_replace(text: str, arg: dict) -> str:
    return re.sub(arg["from"], arg["to"], text)


def _left_pad(text: str, arg: dict) -> str:
    return text.rjust(int(arg["width"]), str(arg.get("char", " "))) if text else text


def _match_rules(text: str, arg: Any = None) -> str:
    """Uniwersalna klasyfikacja/mapowanie tekstu na podstawie reguł regex z YAML.

    Obsługiwany format parametru `arg`:
      - słownik z konfiguracją:
          rules: [{value: ..., pattern: ...}, ...]
          strategy: 'earliest' (domyślnie: najwcześniejsze dopasowanie w tekście) | 'first' (kolejność reguł)
          case_insensitive: bool (domyślnie: True)
      - lista reguł (shorthand):
          [{value: 1, pattern: '...'}, ...] lub [[1, '...'], ...]
    """
    if not text:
        return ""

    if isinstance(arg, dict):
        rules_def = arg.get("rules", [])
        strategy = arg.get("strategy", "earliest")
        case_insensitive = arg.get("case_insensitive", True)
    elif isinstance(arg, list):
        rules_def = arg
        strategy = "earliest"
        case_insensitive = True
    else:
        rules_def = []
        strategy = "earliest"
        case_insensitive = True

    rules: list[tuple[str, str]] = []
    for item in rules_def:
        if isinstance(item, dict):
            val = str(item.get("value", item.get("id", "")))
            pat = str(item.get("pattern", item.get("match", "")))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            val, pat = str(item[0]), str(item[1])
        else:
            continue
        if pat:
            rules.append((val, pat))

    stripped = text.strip()
    if any(stripped == r[0] for r in rules):
        return stripped

    clean = re.sub(r"\s+", " ", text)
    flags = re.IGNORECASE if case_insensitive else 0

    if strategy == "first":
        for val, pat in rules:
            if re.search(pat, clean, flags):
                return val
        return ""
    else:
        # 'earliest' - najwcześniejsze wystąpienie w oknie tekstu (np. pierwszy zarzut)
        matches: list[tuple[int, str]] = []
        for val, pat in rules:
            m = re.search(pat, clean, flags)
            if m:
                matches.append((m.start(), val))
        if not matches:
            return ""
        matches.sort(key=lambda x: x[0])
        return matches[0][1]


NORMALIZERS: dict[str, Callable[[str, Any], str]] = {
    "strip": lambda s, _: s.strip(),
    "collapse_whitespace": lambda s, _: re.sub(r"\s+", " ", s).strip(),
    "remove_whitespace": lambda s, _: re.sub(r"\s+", "", s),
    "lowercase": lambda s, _: s.lower(),
    "uppercase": lambda s, _: s.upper(),
    "digits_only": lambda s, _: re.sub(r"\D", "", s),
    "remove_prefix_PL": lambda s, _: re.sub(r"^\s*PL\s*", "", s, flags=re.IGNORECASE),
    "remove_currency": lambda s, _: re.sub(r"(?i)\s*(zł(otych)?|pln)\.?", "", s).strip(),
    "remove_thousand_separators": _remove_thousand_separators,
    "decimal_comma_to_dot": lambda s, _: s.replace(",", "."),
    "remove_titles": lambda s, _: re.sub(r"\s+", " ", _TITLES_RE.sub(" ", s)).strip(" ,;"),
    "nominative_person_name": _nominative_person_name,
    "date_to_iso": _date_to_iso,
    "match_rules": _match_rules,
    "earliest_match": _match_rules,
    "czynnosc_id": _match_rules,
    "lider_flag": _match_rules,
    "left_pad": _left_pad,
    "regex_replace": _regex_replace,
}


def _apply_step(text: str, step: Any) -> str:
    if isinstance(step, dict):
        (name, arg), = step.items()
    else:
        name, arg = step, None
    if name not in NORMALIZERS:
        raise KeyError(f"Nieznany krok normalizacji w formats.yaml: {name!r}")
    return NORMALIZERS[name](text, arg)


def _weighted_mod(digits: str, weights: list[int], mod: int) -> int:
    return sum(int(d) * w for d, w in zip(digits, weights)) % mod


def _check_nip(v: str) -> bool:
    c = _weighted_mod(v, [6, 5, 7, 2, 3, 4, 5, 6, 7], 11)
    return len(v) == 10 and c != 10 and c == int(v[9])


def _check_regon(v: str) -> bool:
    if len(v) == 9:
        return _weighted_mod(v, [8, 9, 2, 3, 4, 5, 6, 7], 11) % 10 == int(v[8])
    if len(v) == 14:
        return _weighted_mod(v, [2, 4, 8, 5, 0, 9, 7, 3, 6, 1, 2, 4, 8], 11) % 10 == int(v[13])
    return False


def _check_pesel(v: str) -> bool:
    return len(v) == 11 and (10 - _weighted_mod(v, [1, 3, 7, 9, 1, 3, 7, 9, 1, 3], 10)) % 10 == int(v[10])


def _check_iban_mod97(v: str) -> bool:  # NRB (26 cyfr) jako polski IBAN
    return len(v) == 26 and int(v[2:] + "2521" + v[:2]) % 97 == 1


CHECKSUMS: dict[str, Callable[[str], bool]] = {
    "nip": _check_nip,
    "regon": _check_regon,
    "pesel": _check_pesel,
    "iban_mod97": _check_iban_mod97,
}

_EMPTY_MARKERS = {"", "null", "none", "brak", "-", "—", "n/a"}


def _canonical(value: Any) -> str:
    return re.sub(r"[\s_-]+", "_", str(value).strip().lower())


# --- reguły ekstrakcji ---------------------------------------------------------------

_UPPER = "A-ZĄĆĘŁŃÓŚŹŻ"
_LOWER = "a-ząćęłńóśźż"
_TITLE_LINE_RE = re.compile(rf"[{_UPPER}][{_UPPER}\s]{{4,}}")
_HEADING_RE = re.compile(r"^(?:§\s*\d+[.)]?|\d+[.)])\s*")
_LABEL_WORDS_RE = re.compile(r"^(?:Zamawiający|Mocodawca|Partner|Lider|Wykonawca|Odwołujący)\b", re.IGNORECASE)
_QUOTES = {"„": "”", "\"": "\"", "“": "”", "«": "»"}


def _hint_re(hint: str, anchored: bool = False) -> re.Pattern:
    """Etykieta z `hints` → tolerancyjny regex (wielkość liter, odstępy, końcówki fleksyjne)."""
    hint = hint.strip()
    if hint.endswith(":"):
        hint = hint[:-1].rstrip()
    parts: list[str] = []
    tokens = [t for t in re.split(r"(\s+|<[^>]+>)", hint) if t]
    for i, token in enumerate(tokens):
        if token.isspace():
            parts.append(r"\s+")
        elif token.startswith("<"):  # np. <miejscowość>
            parts.append(rf"[{_UPPER}][\w-]+")
        else:
            for piece in (p for p in re.split(r"(\W)", token) if p):
                if piece == ".":
                    parts.append(r"\.\s*")
                elif re.fullmatch(r"\w+", piece):
                    parts.append(re.escape(piece[:-1]) + r"\w*" if len(piece) >= 6 else re.escape(piece))
                else:
                    parts.append(re.escape(piece))
    body = "".join(parts)
    last_word = re.search(r"(\w+)$", hint)
    if last_word and len(last_word.group(1)) < 6:
        body += r"(?!\w)"
    return re.compile((r"^[ \t]*" if anchored else r"(?<!\w)") + body, re.IGNORECASE | re.MULTILINE)


def _label_re(hint: str) -> re.Pattern:
    """Etykieta sekcji: fraza na początku linii, po niej ':' / '(' albo koniec linii.

    Bez myślnika – 'Zamawiający – X – prowadzi postępowanie…' to proza/podpis, nie nagłówek sekcji.
    """
    regex = _hint_re(hint, anchored=True)
    return re.compile(regex.pattern + r"[ \t]*(?::|$|\()", regex.flags)


def _clean_head(text: str) -> str | None:
    """Pierwsza linia sekcji → kandydat na nazwę: bez adresu/dopisków i początkowych słów pisanych małą literą."""
    text = _HEADING_RE.sub("", text.strip(" :\t"), count=1)
    text = re.sub(r"^(?:Mocodawc[aąęy]\s+(?:jest|są)\s+|Mocodawca\s+|Zamawiający\s+|Wykonawca\s+|Odwołujący\s+)", "", text, flags=re.IGNORECASE)
    text = re.split(r",\s|\s\(", text, maxsplit=1)[0]
    text = re.sub(rf"^(?:[{_LOWER}]+\s+)+", "", text).strip()
    return text if re.match(rf"[{_UPPER}]", text) else None


def _segment_head(text: str) -> str | None:
    """Nagłówek sekcji → nazwa podmiotu; pomija etykiety typu 'Zamawiający – Lider:' i numery listy."""
    lines = text.split("\n")
    first = _clean_head(lines[0]) if lines else None
    if first and not _LABEL_WORDS_RE.match(first):
        return first
    for line in lines[1:]:
        line = line.strip()
        if not line or _TITLE_LINE_RE.fullmatch(line):
            continue
        candidate = _clean_head(_HEADING_RE.sub("", line, count=1))
        if candidate and not _LABEL_WORDS_RE.match(candidate):
            return candidate
    return None


_ID_LABEL_RE = re.compile(r"\b(?:KRS|NIP|REGON)\b[ \t]*[:\d]")


def _split_numbered(text: str) -> list[str]:
    """Rozbija listę '1) … 2) …' na elementy (tylko numeracja z nawiasem).

    Listy podmiotów mają w każdym elemencie identyfikator (KRS/NIP/REGON); numerowane
    zarzuty/prozy (np. „1) zaniechaniu … wykonawcy X sp. z o.o. …”) – nie, więc ich nie rozbijamy.
    """
    lines = text.split("\n")
    starts = [i for i, line in enumerate(lines) if re.match(r"^\s*\d+\)\s*\S", line)]
    if len(starts) < 2:
        return [text]
    parts = ["\n".join(lines[start:end]) for start, end in zip(starts, starts[1:] + [len(lines)])]
    return parts if all(_ID_LABEL_RE.search(part) for part in parts) else [text]


def _first_content_hint(hints: list[str], text: str) -> re.Match | None:
    """Pierwsze wystąpienie etykiety, które nie stoi w linii-nagłówku ('§ 1. Mocodawca', '1) …')."""
    for hint in hints:
        for match in _hint_re(hint).finditer(text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            line_end = text.find("\n", match.end())
            line = text[line_start: line_end if line_end != -1 else None].strip()
            if not _HEADING_RE.match(line):
                return match
    return None


def _is_signature_block(lines: list[str], i: int) -> bool:
    """Etykieta, po której od razu stoją kropki podpisu, to blok podpisów, nie sekcja danych."""
    following = next((line.strip() for line in lines[i + 1:] if line.strip()), "")
    return bool(re.fullmatch(r"[.\s]+", following))


def _all_hints(node: dict) -> set[str]:
    hints = set(node.get("hints", []))
    if node.get("type") == "object":
        for child in node["fields"].values():
            hints |= _all_hints(child)
    elif node.get("type") == "array":
        hints |= _all_hints(node["items"])
    return hints


def _filled(value: Any) -> int:
    """Liczba niepustych pól-liści (do wyboru najlepszego podziału tekstu)."""
    if isinstance(value, dict):
        return sum(_filled(v) for v in value.values())
    if isinstance(value, list):
        return sum(_filled(v) for v in value)
    return 0 if value in (None, "") else 1


def _first_group(match: re.Match) -> str:
    return next((g for g in match.groups() if g), match.group(0))


class RuleFiller:
    """Wypełnia drzewo pól na podstawie `hints` i wzorców `capture`."""

    OBJECT_WINDOW = (0, 4)   # linie wokół etykiety obiektu (od, do)
    ARRAY_WINDOW = (-3, 2)   # linie wokół etykiety tablicy (podpisy stoją zwykle nad funkcją)

    def __init__(self, definitions: Definitions):
        self.defs = definitions
        self.document = ""
        self.labels: list[re.Pattern] = []

    def fill(self, label: str, text: str) -> Any:
        self.document = (text or "").replace("\r", "").replace("\u00a0", " ")
        schema = self.defs.fields[label]
        self.labels = [_hint_re(h, anchored=True) for h in sorted(_all_hints(schema))]
        return self._node(schema, self.document, [None], [])

    # węzły

    def _node(self, node: dict, scope: str, head: list, siblings: list[str], focused: bool = False) -> Any:
        kind = node.get("type")
        if kind == "object":
            return self._object(node, scope, head)
        if kind == "array":
            return self._array(node, scope, siblings)
        return self._leaf(node, scope, head, focused)

    def _object(self, node: dict, scope: str, head: list, focused: bool = False) -> dict:
        children = node["fields"]
        result = {}
        for name, child in children.items():
            siblings = [h for other, c in children.items() if other != name and _is_container(c) for h in c.get("hints", [])]
            if child.get("type") == "object" and child.get("hints"):
                segmentations = self._segmentations(child, scope, siblings, self.OBJECT_WINDOW) or [[(scope, None)]]
                candidates = [
                    self._object(child, sub_scope, [sub_head], focused=True)
                    for sub_scope, sub_head in (segs[0] for segs in segmentations)
                ]
                result[name] = max(candidates, key=_filled)  # przy remisie pierwszy kandydat
            else:
                result[name] = self._node(child, scope, head, siblings, focused)
        return result

    def _array(self, node: dict, scope: str, siblings: list[str]) -> list:
        segmentations = self._segmentations(node, scope, siblings, self.ARRAY_WINDOW)
        if not segmentations:
            return []
        if node.get("split_numbered"):
            segmentations = [self._expand_numbered(segs) for segs in segmentations]
        candidates = [self._items(node["items"], segs) for segs in segmentations]
        return max(candidates, key=_filled)

    def _expand_numbered(self, segments: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
        """Sekcje zawierające numerowaną listę podmiotów (opt-in: `split_numbered`) → element na pozycję."""
        expanded: list[tuple[str, str | None]] = []
        for seg, seg_head in segments:
            parts = _split_numbered(seg)
            if len(parts) > 1:
                expanded.extend((part, _segment_head(part)) for part in parts)
            else:
                expanded.append((seg, seg_head))
        return expanded

    def _items(self, item: dict, segments: list[tuple[str, str | None]]) -> list:
        name_field = self._person_key(item)
        if not name_field:
            return [self._node(item, seg, [seg_head], []) for seg, seg_head in segments]

        # tablica osób: jeden element na każde znalezione imię i nazwisko
        capture = re.compile(self._capture(item["fields"][name_field]), re.MULTILINE)
        items, seen = [], set()
        for seg, _ in segments:
            matches = list(capture.finditer(seg))
            for i, match in enumerate(matches):
                person = " ".join(_first_group(match).split())
                if person.lower() in seen:
                    continue
                seen.add(person.lower())
                end = matches[i + 1].start() if i + 1 < len(matches) else len(seg)
                element = self._node(item, seg[match.start():end], [None], [])
                element[name_field] = person
                items.append(element)
        return items

    def _person_key(self, item: dict) -> str | None:
        """Nazwa pola, gdy element tablicy to osoba (jedyne wymagane pole typu person_name)."""
        if item.get("type") != "object":
            return None
        required = [n for n, f in item["fields"].items() if not _is_container(f) and not f.get("nullable")]
        if len(required) == 1:
            f = item["fields"][required[0]]
            if f.get("type") == "person_name" and not f.get("hints") and self._capture(f):
                return required[0]
        return None

    def _segmentations(self, node: dict, scope: str, siblings: list[str], window: tuple[int, int]) -> list[list]:
        """Możliwe podziały tekstu na fragmenty [(tekst, pierwsza linia)] dla obiektu/tablicy.

        Kandydaci: sekcje od etykiety (np. 'Zamawiający:') i okno kilku linii wokół frazy z `hints`
        (szukanej też w całym dokumencie, gdy brak jej we fragmencie); bez nich – brak kandydatów
        (wywołujący decyduje o fallbacku). Wywołujący wybiera kandydata, który wypełnia najwięcej pól.
        """
        hints = node.get("hints", [])
        lines = scope.split("\n")
        candidates: list[list] = []

        anchored = [_label_re(h) for h in hints]
        starts = [
            i for i, line in enumerate(lines)
            if any(r.match(line) for r in anchored) and not _is_signature_block(lines, i)
        ]
        if starts:
            stops = [_label_re(h) for h in siblings] + [_label_re(h) for h in node.get("stops", [])]
            segments = []
            for i in starts:
                end = next(
                    (j for j in range(i + 1, len(lines))
                     if j in starts or any(r.match(lines[j]) for r in stops) or _TITLE_LINE_RE.fullmatch(lines[j].strip())),
                    len(lines),
                )
                segment = "\n".join(lines[i:end])
                segments.append((segment, _segment_head(segment)))
            candidates.append(segments)

        for text in (scope, self.document) if scope is not self.document else (scope,):
            match = _first_content_hint(hints, text)
            if match:
                i = text.count("\n", 0, match.start())
                text_lines = text.split("\n")
                lo, hi = max(0, i + window[0]), i + window[1] + text.count("\n", match.start(), match.end())
                segment = "\n".join(text_lines[lo:hi])
                # nazwa liczona od trafienia, nie od początku okna (okno może obejmować tytuł pisma);
                # bez zgadywania z dalszych linii – w oknie wpada proza ('NIP …', '… w postępowaniu …')
                line_end = text.find("\n", match.end())
                head = _clean_head(text[match.end(): line_end if line_end != -1 else None])
                candidates.append([(segment, head)])
                break

        return candidates

    # pola-liście

    def _capture(self, node: dict) -> str | None:
        return node.get("capture") or self.defs.format_of(node).get("capture")

    def _leaf(self, node: dict, scope: str, head: list, focused: bool = False) -> Any:
        capture = self._capture(node)
        regex = re.compile(capture, re.MULTILINE) if capture else None
        hints = node.get("hints", [])
        is_text = self.defs.format_of(node).get("json_type", "string") == "string" and not node.get("values")
        hint_seen = False
        for hint in hints:
            for m in _hint_re(hint).finditer(scope):
                hint_seen = True
                if regex:
                    value = self._near(regex, scope, m)
                elif is_text:
                    value = self._text_after(scope, m)
                else:
                    value = None
                if value:
                    return value
        # bez etykiety: wzorzec szukany w całym fragmencie – tylko gdy nie grozi to przypadkowym trafieniem
        # (pole wymagane, własne `hints`/`capture` albo fragment zawężony etykietami obiektu-rodzica)
        if regex and not hint_seen and (hints or node.get("capture") or not node.get("nullable") or focused):
            if m := regex.search(scope):
                return _first_group(m)
        if not hints and not regex and head[0] and node.get("type") == "text":
            value, head[0] = head[0], None  # pierwsza linia sekcji, np. nazwa pod 'Odwołujący:'
            return value
        return None

    @staticmethod
    def _near(regex: re.Pattern, scope: str, hint: re.Match) -> str | None:
        """Pierwsza wartość pasująca do `capture` za etykietą (lub obejmująca etykietę, np. 2026/BZP...)."""
        lo = max(0, hint.start() - 40)
        for m in regex.finditer(scope, lo, hint.end() + 160):
            if m.end() > hint.start():
                return _first_group(m)
        return None

    def _text_after(self, scope: str, hint: re.Match) -> str | None:
        """Tekst za etykietą: reszta linii (albo następna linia), cytat „…” i linie kontynuacji."""
        line_start = scope.rfind("\n", 0, hint.start()) + 1
        line_end = scope.find("\n", hint.end())
        line_end = len(scope) if line_end == -1 else line_end
        rest = re.sub(r"^[^\w„\"“«]*", "", scope[hint.end():line_end])
        if tail := re.match(r"^[^\d:„\"“«]{0,25}:\s*", rest):  # 'PRZELEWU:' w 'TYTUŁ PRZELEWU:'
            rest = rest[tail.end():]
        elif not scope[line_start:hint.start()].strip() and re.fullmatch(r"[^\W\d]+(\s+[^\W\d]+)?\.?\s*", rest):
            rest = ""  # cała linia to etykieta, np. 'Tytuł przelewu' – wartość w następnej linii
        following = [line.strip() for line in scope[line_end + 1:].split("\n")]
        if not rest.strip():
            following = [line for line in following if line]
            if not following:
                return None
            rest, following = following[0], following[1:]

        rest = rest.strip()
        if rest[:1] in _QUOTES:
            closing = _QUOTES[rest[0]]
            text = rest[1:]
            for line in following:
                if closing in text:
                    break
                text += " " + line
            return " ".join(text.split(closing)[0].split()) or None

        parts = [rest]
        for line in following:  # zawinięta linia: poprzednia urywa się na słowie, a ta nie jest etykietą
            if (not re.search(r"[^\W\d],?$", parts[-1]) or not line or "...." in line or re.match(r"[^:]{0,30}:", line)
                    or _TITLE_LINE_RE.fullmatch(line) or any(r.match(line) for r in self.labels)):
                break
            parts.append(line)
        return " ".join(" ".join(parts).split()) or None


# --- exporter ------------------------------------------------------------------------

def _load_classifier() -> Callable[[str], str | None]:
    spec = importlib.util.spec_from_file_location("document_classifier", CLASSIFIER_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.classify


def _default_extractor(path: Path) -> dict:
    from pipeline.data_extraction import extract_pdf

    return extract_pdf(path)


class JsonExporter:
    """Buduje JSON dokumentu na podstawie definicji, wyniku data_extraction i klasyfikatora."""

    def __init__(
        self,
        definitions_dir: str | Path = DEFINITIONS_DIR,
        extractor: Callable[[Path], dict] | None = None,
        classifier: Callable[[str], str | None] | None = None,
    ):
        self.defs = Definitions.load(definitions_dir)
        self.rules = RuleFiller(self.defs)
        self._extract = extractor or _default_extractor
        self._classifier = classifier

    # potok

    def classify(self, text: str) -> str | None:
        if self._classifier is None:
            self._classifier = _load_classifier()
        return self._classifier(text)

    def process(self, pdf_path: str | Path) -> tuple[str | None, Any, Report]:
        """PDF → (klasa, dane JSON, raport)."""
        extracted = self._extract(Path(pdf_path))
        text = extracted.get("text") or extracted.get("markdown") or ""
        label = self.classify(text)
        data, report = self.build(text, label)
        meta = extracted.get("metadata") or {}
        report.extraction = {k: meta[k] for k in ("filename", "status", "total_pages", "ocr_applied", "warning") if k in meta}
        return label, data, report

    def build(self, text: str, label: str | None) -> tuple[Any, Report]:
        """Tekst dokumentu + klasa → (dane JSON, raport)."""
        report = Report(document_class=label)
        if label is None or label not in self.defs.fields:
            if label is not None:
                report.add("", f"Brak definicji pól dla klasy {label!r}", "error")
            report.source = "negative"
            return copy.deepcopy(self.defs.negative_output), report

        schema = self.defs.fields[label]
        report.source = "rules"
        data = self._conform(schema, self.rules.fill(label, text), "", report)
        self._apply_derivations(label, data, report)
        self._check_required(schema, data, "", report)
        return data, report

    # zapis

    def export_pdf(self, pdf_path: str | Path, out_path: str | Path, report_dir: str | Path | None = None) -> Path:
        pdf_path, out_path = Path(pdf_path), Path(out_path)
        try:
            label, data, report = self.process(pdf_path)
        except Exception as exc:
            logger.exception("Nie udało się przetworzyć %s", pdf_path)
            label, data, report = None, copy.deepcopy(self.defs.negative_output), Report(source="error")
            report.add("", f"Błąd przetwarzania: {exc}", "error")
        _write_json(out_path, data)
        if report_dir is not None:
            _write_json(Path(report_dir) / f"{out_path.stem}.report.json", {"file": pdf_path.name, **report.to_dict()})
        problems = sum(1 for i in report.issues if i["level"] != "info")
        logger.info("%s → %s [%s, %s, uwagi: %d]", pdf_path.name, out_path, label or "negatywny", report.source, problems)
        return out_path

    def export_tree(self, input_dir: str | Path, output_dir: str | Path, report_dir: str | Path | None = None) -> list[Path]:
        """input/<sprawa>/<plik>.pdf → output/<sprawa>/<plik>.json (nazwa jak plik wejściowy)."""
        input_dir, output_dir = Path(input_dir), Path(output_dir)
        written = []
        for pdf in sorted(p for p in input_dir.rglob("*") if p.suffix.lower() == ".pdf"):
            rel = pdf.relative_to(input_dir).with_suffix(".json")
            reports = Path(report_dir) / rel.parent if report_dir is not None else None
            written.append(self.export_pdf(pdf, output_dir / rel, reports))
        return written

    # budowa drzewa wynikowego

    def _conform(self, node: dict, raw: Any, path: str, report: Report) -> Any:
        """Surowe wartości → drzewo o kluczach i kolejności z definicji, z wartościami po normalizacji."""
        kind = node.get("type")
        if kind == "object":
            raw = raw if isinstance(raw, dict) else {}
            return {
                name: self._conform(child, raw.get(name), f"{path}.{name}" if path else name, report)
                for name, child in node["fields"].items()
            }
        if kind == "array":
            items = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
            result = [self._conform(node["items"], item, f"{path}[{i}]", report) for i, item in enumerate(items)]
            result = [item for item in result if not _is_empty(item)]
            while len(result) < int(node.get("min_items", 0)):
                result.append(self._conform(node["items"], None, f"{path}[{len(result)}]", report))
            return result
        return self._normalize(node, raw, path, report)

    def _normalize(self, node: dict, value: Any, path: str, report: Report) -> Any:
        if value is None:
            return None
        if isinstance(value, (list, dict)):
            report.add(path, "oczekiwano pojedynczej wartości", value=value)
            return None
        fmt = self.defs.format_of(node)
        json_type = fmt.get("json_type", "string")
        if isinstance(value, bool):
            value = int(value)

        if isinstance(value, (int, float)) and json_type in ("integer", "number"):
            result: Any = value
        else:
            text = str(value)
            for step in fmt.get("normalize", []):
                text = _apply_step(text, step)
            if text.strip().lower() in _EMPTY_MARKERS:
                return None
            if json_type == "string":
                pattern = fmt.get("pattern")
                if pattern and not re.fullmatch(pattern, text):
                    report.add(path, "wartość niezgodna z formatem – pominięta", value=text)
                    return None
                checksum = fmt.get("checksum")
                if checksum and not CHECKSUMS[checksum["algorithm"]](text):
                    level = "warning" if checksum.get("mode", "warn") == "warn" else "error"
                    report.add(path, f"niepoprawna suma kontrolna ({checksum['algorithm']})", level, value=text)
                    if level == "error":
                        return None
                result = text
            else:
                try:
                    result = float(text.replace(",", ".").replace(" ", ""))
                except ValueError:
                    report.add(path, f"oczekiwano liczby ({json_type})", value=text)
                    return None

        if json_type == "integer":
            if float(result) != int(float(result)):
                report.add(path, "oczekiwano liczby całkowitej", value=result)
                return None
            result = int(float(result))
        elif json_type == "number":
            result = float(result)

        allowed = node.get("values") or fmt.get("values")
        if allowed is not None and result not in allowed:
            match = next((a for a in allowed if _canonical(a) == _canonical(result)), None)
            if match is None:
                report.add(path, f"wartość spoza dozwolonych {allowed}", value=result)
                return None
            result = match
        return result

    def _check_required(self, node: dict, value: Any, path: str, report: Report) -> None:
        kind = node.get("type")
        if kind == "object":
            for name, child in node["fields"].items():
                self._check_required(child, (value or {}).get(name), f"{path}.{name}" if path else name, report)
        elif kind == "array":
            for i, item in enumerate(value or []):
                self._check_required(node["items"], item, f"{path}[{i}]", report)
        elif value is None and not node.get("nullable"):
            report.add(path, "brak wartości pola wymaganego")

    # pola wyliczane (derivations.yaml)

    def _apply_derivations(self, label: str, data: Any, report: Report) -> None:
        for spec, rule in (self.defs.derivations.get(label) or {}).items():
            *parents, name = spec.split(".")
            for chain in _containers(data, parents):
                container = chain[-1]
                if not isinstance(container, dict):
                    continue
                if rule.get("override") and container.get(name) is not None:
                    continue  # wartość z dokumentu ma pierwszeństwo
                found, value = self._derive(rule, chain, spec, report)
                if found:
                    container[name] = value

    def _derive(self, rule: dict, chain: list, spec: str, report: Report) -> tuple[bool, Any]:
        if "lookup" in rule:
            key = _resolve(rule["lookup"]["from"], chain)
            if key is None:
                return True, None
            table = self.defs.lookups.get(rule["lookup"]["table"], {})
            for candidate in (key, str(key), _as_int(key)):
                if candidate in table:
                    return True, table[candidate]
            report.add(spec, f"brak klucza {key!r} w słowniku {rule['lookup']['table']}")
            return True, None
        if "present" in rule:
            return True, 0 if _is_empty(_resolve(rule["present"], chain)) else 1
        if "when" in rule:
            for case in rule["when"]:
                if "else" in case:
                    return True, case["else"]
                if _condition(str(case["if"]), chain):
                    return True, case.get("then")
            return False, None
        report.add(spec, f"nieznana reguła w derivations.yaml: {sorted(rule)}", "error")
        return False, None


# --- pomocnicze ----------------------------------------------------------------------

def _containers(data: Any, tokens: list[str]) -> list[list]:
    """Łańcuchy przodków [root, ..., obiekt zawierający pole] dla ścieżki z `[]`."""
    chains = [[data]]
    for token in tokens:
        is_array, name = token.endswith("[]"), token.removesuffix("[]")
        next_chains = []
        for chain in chains:
            node = chain[-1].get(name) if isinstance(chain[-1], dict) else None
            if is_array and isinstance(node, list):
                next_chains += [chain + [node, item] for item in node]
            elif not is_array and node is not None:
                next_chains.append(chain + [node])
        chains = next_chains
    return chains


def _resolve(path: str, chain: list) -> Any:
    path, up = path.strip(), 0
    while path.startswith("../") or path == "..":
        path, up = path[3:], up + 1
    if up >= len(chain):
        return None
    node = chain[-1 - up]
    for part in filter(None, path.split(".")):
        node = node.get(part) if isinstance(node, dict) else None
    return node


def _operand(token: str, chain: list) -> Any:
    token = token.strip()
    if m := re.fullmatch(r"count\((.*)\)", token):
        value = _resolve(m.group(1), chain)
        return len(value) if isinstance(value, (list, dict)) else (0 if value is None else 1)
    if token in ("null", "None"):
        return None
    if token in ("true", "false"):
        return token == "true"
    if re.fullmatch(r"-?\d+(\.\d+)?", token):
        return float(token) if "." in token else int(token)
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1]
    return _resolve(token, chain)


def _condition(expr: str, chain: list) -> bool:
    """Warunek z derivations.yaml: porównania (==, !=, <, >, <=, >=) łączone `and` / `or`, count(), null."""
    def compare(term: str) -> bool:
        m = re.fullmatch(r"\s*(.+?)\s*(==|!=|>=|<=|>|<)\s*(.+?)\s*", term)
        if not m:
            return not _is_empty(_operand(term, chain))
        left, op, right = _operand(m.group(1), chain), m.group(2), _operand(m.group(3), chain)
        if op == "==":
            return left == right
        if op == "!=":
            return left != right
        if left is None or right is None:
            return False
        return {"<": left < right, ">": left > right, "<=": left <= right, ">=": left >= right}[op]

    return any(all(compare(t) for t in re.split(r"\s+and\s+", d)) for d in re.split(r"\s+or\s+", expr))


def _as_int(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _is_empty(value: Any) -> bool:
    if isinstance(value, dict):
        return all(_is_empty(v) for v in value.values())
    if isinstance(value, list):
        return all(_is_empty(v) for v in value)
    return value is None or value == ""


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --- CLI -------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PDF (odwołanie / pełnomocnictwo / przelew) → JSON wg definitions/.")
    parser.add_argument("input", nargs="?", default=str(ROOT / "input"), help="katalog z PDF-ami albo pojedynczy PDF")
    parser.add_argument("output", nargs="?", default=str(ROOT / "output"), help="katalog wynikowy")
    parser.add_argument("--definitions", default=str(DEFINITIONS_DIR), help="katalog z definicjami pól")
    parser.add_argument("--report-dir", help="zapisz raporty ekstrakcji (<plik>.report.json) w tym katalogu")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    exporter = JsonExporter(args.definitions)

    source, output = Path(args.input), Path(args.output)
    if source.is_file():
        exporter.export_pdf(source, output / f"{source.stem}.json", args.report_dir)
    elif source.is_dir():
        exporter.export_tree(source, output, args.report_dir)
    else:
        parser.error(f"Nie znaleziono: {source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
