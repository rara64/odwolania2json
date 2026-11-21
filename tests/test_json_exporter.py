import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "dev" / "dane"


def _load_exporter():
    spec = importlib.util.spec_from_file_location("json_exporter", ROOT / "pipeline" / "json-exporter" / "exporter.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ex = _load_exporter()


@pytest.fixture(scope="module")
def exporter():
    return ex.JsonExporter()


def _finish(exporter, label, raw):
    """Surowe wartości → normalizacja + pola wyliczane (jak w JsonExporter.build)."""
    report = ex.Report()
    data = exporter._conform(exporter.defs.fields[label], raw, "", report)
    exporter._apply_derivations(label, data, report)
    return data


@pytest.mark.parametrize(
    ("fmt", "raw", "expected"),
    [
        ("nip", "PL 999-928-40-65", "9999284065"),
        ("amount", "1 200,50 zł", 1200.5),
        ("amount", "200,00 PLN", 200.0),
        ("date", "21 marca 2026 r.", "2026-03-21"),
        ("date", "11-10-2026", "2026-10-11"),
        ("numer_ted", "2026/S 112-999137", "999137-2026"),
        ("numer_bzp", "2026/bzp 00999778", "2026/BZP00999778"),
        ("krs", "12345", "0000012345"),
        ("person_name", "r. pr. Aleksandra Marciniak", "Aleksandra Marciniak"),
        ("bank_account", "PL05 4706 8028 7581 5397 2145 5024", "05470680287581539721455024"),
        ("flag", True, 1),
        ("regon", "12345", None),  # niezgodne z wzorcem → null
    ],
)
def test_normalize(exporter, fmt, raw, expected):
    assert exporter._normalize({"type": fmt}, raw, "x", ex.Report()) == expected


def test_negative_document_gives_empty_object(exporter):
    data, report = exporter.build("cokolwiek", None)
    assert data == {} and report.source == "negative"


def test_odwolanie_structure_and_derivations(exporter):
    data = _finish(exporter, "odwolanie", {
        "OdwolanieKIO": {"czynnosc_dzien": "20.03.2026", "czynnosc_powod_id": "9", "czynnosc_opis": "cokolwiek"},
        "Odwolujacy": [{"nip": "PL9999284065", "pelnomocnik": {"imie_nazwisko": "adw. Jan Kowalski", "mail": "BIURO@X.PL"}}],
        "Zamawiajacy": [{"nazwa": "Gmina X"}],
    })
    assert list(data) == ["OdwolanieKIO", "Odwolujacy", "Zamawiajacy", "ZamowieniePubliczne"]
    assert data["OdwolanieKIO"]["czynnosc_dzien"] == "2026-03-20"
    assert data["OdwolanieKIO"]["czynnosc_opis"] == "Decyzja o unieważnieniu postępowania"
    odw = data["Odwolujacy"][0]
    assert odw["nip"] == "9999284065" and odw["przez-pelnomocnika"] == 1
    assert odw["pelnomocnik"] == {"imie_nazwisko": "Jan Kowalski", "nazwa": None, "mail": "biuro@x.pl"}
    assert data["Zamawiajacy"][0]["prowadzacy_postepowanie"] is None  # jeden zamawiający → null
    assert data["ZamowieniePubliczne"] == {"numer_bzp": None, "numer_postepowania": None, "numer_ted": None, "tytul": None}


def test_pelnomocnictwo_typ_derivation(exporter):
    data = _finish(exporter, "pelnomocnictwo", {"mocodawca": [
        {"data": {"imie_nazwisko": "Jan Nowak", "krs": None, "nazwa": "Jan Nowak Usługi"}},
        {"data": {"imie_nazwisko": None, "krs": "9999405188", "nazwa": "Spółka S.A."}},
    ]})
    assert [m["data"]["typ"] for m in data["mocodawca"]] == ["osoba_fizyczna", "osoba_prawna"]


def test_build_from_text_with_rules(exporter):
    text = "Data transakcji\n2026-03-21\nKwota operacji\n200,00 PLN\nNr ref.\nCEN2604016344389\n"
    data, report = exporter.build(text, "przelew")
    assert data["przelew_bankowy"]["data_operacji"] == "2026-03-21"
    assert data["przelew_bankowy"]["kwota"] == 200.0
    assert data["przelew_bankowy"]["numer_transakcji"] == "CEN2604016344389"
    assert report.source == "rules"
    assert any(i["path"] == "przelew_bankowy.na_rachunek" for i in report.issues)  # brak pola wymaganego


@pytest.mark.parametrize("case", ["przyklad_01", "przyklad_17"])
def test_export_pdf_end_to_end(tmp_path, exporter, case):
    out = exporter.export_pdf(DATA / case / "potwierdzenie_przelewu.pdf", tmp_path / case / "potwierdzenie_przelewu.json")
    expected = json.loads((DATA / case / "przelew.json").read_text(encoding="utf-8"))
    assert json.loads(out.read_text(encoding="utf-8")) == expected


def test_export_tree_mirrors_input_structure(tmp_path, exporter):
    src = tmp_path / "input" / "sprawa1"
    src.mkdir(parents=True)
    (src / "dok.pdf").write_bytes((DATA / "przyklad_01" / "pelnomocnictwo.pdf").read_bytes())
    (src / "inny.pdf").write_bytes((DATA / "negatywne" / "KIO_KIO 2617-21.pdf").read_bytes())
    written = exporter.export_tree(tmp_path / "input", tmp_path / "output", tmp_path / "reports")
    assert sorted(p.relative_to(tmp_path / "output").as_posix() for p in written) == ["sprawa1/dok.json", "sprawa1/inny.json"]
    assert json.loads((tmp_path / "output" / "sprawa1" / "inny.json").read_text(encoding="utf-8")) == {}
    pel = json.loads((tmp_path / "output" / "sprawa1" / "dok.json").read_text(encoding="utf-8"))
    assert list(pel) == ["mocodawca", "pelnomocnicy"]
    report = json.loads((tmp_path / "reports" / "sprawa1" / "dok.report.json").read_text(encoding="utf-8"))
    assert report["document_class"] == "pelnomocnictwo"
