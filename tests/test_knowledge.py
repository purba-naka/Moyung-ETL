import pytest
from pydantic import ValidationError

from excel_etl.knowledge import QA, Example, Knowledge, lookup_knowledge


def test_empty_knowledge_overview_and_miss():
    k = Knowledge()
    assert "(none)" in lookup_knowledge(k)
    assert "nothing about" in lookup_knowledge(k, "ton")
    assert "stored: nothing" in lookup_knowledge(k, "ton")


@pytest.mark.parametrize("q", ["", "   ", "?!", "a", None, 5])
def test_unusable_query_gives_overview(q):
    k = Knowledge(brief="Monthly fertilizer report.", glossary={"PKG": "Petrokimia Gresik"})
    out = lookup_knowledge(k, q)
    assert "glossary: 1 terms (PKG)" in out and "lookup_knowledge" in out


def test_glossary_rules_qa_matched_by_word_case_blind():
    k = Knowledge(
        brief="Produksi bulanan. Angka dalam ton.",
        glossary={"PKG": "Petrokimia Gresik", "NPK": "pupuk majemuk"},
        rules=["Abaikan sheet Grafik", "Total baris tidak diambil"],
        qa_log=[QA_("Apa satuan kolom amt?", "ribu rupiah")],
    )
    out = lookup_knowledge(k, "apa arti pkg?")
    assert "PKG = Petrokimia Gresik" in out and "NPK" not in out
    assert "Abaikan sheet Grafik" in lookup_knowledge(k, "sheet grafik")
    assert "ribu rupiah" in lookup_knowledge(k, "satuan amt")
    assert "Angka dalam ton" in lookup_knowledge(k, "ton")
    assert "Produksi bulanan" not in lookup_knowledge(k, "ton")  # only the matching sentence


def QA_(q, a):
    return QA(question=q, answer=a)


def test_best_match_first_and_capped():
    k = Knowledge(
        rules=[f"rule about stock {i}" + (" and ton" if i == 7 else "") for i in range(12)]
    )
    out = lookup_knowledge(k, "stock ton")
    assert out.splitlines()[1].endswith("7 and ton")  # two shared words beat one
    assert out.count("\n  - ") == 5  # capped per section


def test_target_schema_and_examples():
    k = Knowledge(
        target_schema={"produk": "VARCHAR", "tonase": "DOUBLE"},
        examples=[Example(title="sales monthly", spec='{"sources": {}}')],
    )
    assert "tonase DOUBLE" in lookup_knowledge(k, "tonase")
    out = lookup_knowledge(k, "sales")
    assert "sales monthly" in out and '"sources"' in out


def test_long_entries_and_total_are_cut():
    k = Knowledge(
        rules=["ton " + "x" * 5000] * 3, examples=[Example(title="ton", spec="y" * 9000)] * 2
    )
    out = lookup_knowledge(k, "ton")
    assert len(out) <= 4000 and "..." in out


def test_miss_lists_stored_sections():
    k = Knowledge(rules=["only this"], glossary={"A1": "x"})
    out = lookup_knowledge(k, "zebra")
    assert "stored: glossary, rules" in out


def test_json_round_trip_and_strict_keys():
    k = Knowledge(brief="b", glossary={"x": "y"}, qa_log=[QA_("q", "a")])
    assert Knowledge.model_validate_json(k.model_dump_json()) == k
    with pytest.raises(ValidationError):
        Knowledge.model_validate({"brief": "b", "glosary": {}})


def test_unicode_and_regex_chars_in_query():
    k = Knowledge(glossary={"Kg/Ton": "satuan (berat)"})
    assert "satuan" in lookup_knowledge(k, "kg/ton [(*")


def test_examples_capped_at_two():
    k = Knowledge(examples=[Example(title=f"sales {i}", spec="{}") for i in range(4)])
    assert lookup_knowledge(k, "sales").count("sales ") == 2


def test_total_cap_applies():
    k = Knowledge(
        rules=["ton " + "x" * 5000] * 5,
        qa_log=[QA(question="ton " + "q" * 500, answer="a" * 500)] * 5,
        examples=[Example(title="ton", spec="y" * 9000)] * 2,
    )
    out = lookup_knowledge(k, "ton")
    assert len(out) == 4000 and out.endswith("...")
