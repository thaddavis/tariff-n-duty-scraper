"""Offline tests: rate parsing, the relevance filter, and extraction checks."""
import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

from tnd_agent import extract, federal_register, hts


def test_parse_general_rates():
    assert hts.parse_rate("2.5%")["ad_valorem_rate"] == Decimal("0.025")
    assert hts.parse_rate("Free")["ad_valorem_rate"] == Decimal("0")
    specific = hts.parse_rate("4.4¢/kg")
    assert (specific["rate_type"], specific["specific_rate"], specific["specific_unit"]) == \
        ("specific", Decimal("0.044"), "kg")
    compound = hts.parse_rate("2.5% + 4.4¢/kg")
    assert compound["rate_type"] == "compound"
    assert hts.parse_rate("$1.50/each")["specific_rate"] == Decimal("1.50")
    assert hts.parse_rate("See 9903.90.08") is None


def test_pick_base_rate():
    rows = [
        {"htsno": "8703.23.01", "general": "2.5%", "description": "Cars"},
        {"htsno": "8703.23.01.90", "general": "", "description": "Other"},
        {"htsno": "8708.30.50", "general": "2.5% <u></u>", "description": " <u></u> Other"},
        {"htsno": "8544.30.00.00", "general": "5%", "description": "Wiring sets"},
        {"htsno": "8544.42.20.00", "general": "2.6%", "description": "A"},
        {"htsno": "8544.42.90.00", "general": "2.6%", "description": "B"},
        {"htsno": "8544.49.10.00", "general": "Free", "description": "C"},
        {"htsno": "8544.49.20.00", "general": "3.5%", "description": "D"},
    ]
    assert hts.pick_base_rate("8703.23.01.90", rows)["general"] == "2.5%"   # inherits parent
    assert hts.pick_base_rate("8708.30.50", rows) == {                        # markup removed
        "htsno": "8708.30.50", "description": "Other", "general": "2.5%"}
    assert hts.pick_base_rate("8544.30.00", rows)["htsno"] == "8544.30.00.00"  # rated one level down
    assert hts.pick_base_rate("8544.42", rows)["general"] == "2.6%"            # children agree
    assert hts.pick_base_rate("8544.49", rows) is None                         # children differ
    assert hts.pick_base_rate("8708.29.50", rows) is None                      # no such code


def test_relevance_filter():
    assert federal_register.is_relevant(
        {"title": "Modifying the Scope of Products of Canada Subject to the Additional Duties"})
    assert federal_register.is_relevant({"title": "Notice of Action", "abstract": "Section 301"})
    assert not federal_register.is_relevant({"title": "Labor Day, 2026"})
    assert not federal_register.is_relevant({"title": "Restriction on Entry of Certain Nonimmigrant Workers"})


def test_html_to_text():
    assert federal_register.html_to_text("<pre>a &amp; b <a href='x'>c</a></pre>") == "a & b c"


TEXT = """I imposed under section 338 of the Tariff Act of 1930 additional ad valorem
          duties of 25 percent, effective August 19, 2026, on certain products of Canada."""


def _m(**kw):
    base = dict(hts_code="8703.23", origin_country="ca", program="Section 338",
                rate_type="ad_valorem", ad_valorem_rate=0.25, specific_rate=None,
                specific_unit=None, effective_from=dt.date(2026, 8, 19), effective_to=None,
                excluded_hts=["8703.23.01.10"], description="25% on Canadian cars.",
                source_quote="additional ad valorem duties of 25 percent, effective August 19, 2026",
                confidence=0.9)
    return extract.ExtractedMeasure(**{**base, **kw})


def test_to_row_normalizes_and_verifies_quote():
    row, why = extract.to_row(_m(), TEXT)
    assert why is None
    assert row["hts_code"] == "870323"
    assert row["origin_country"] == "CA"
    assert row["program"] == "section_338"
    assert row["ad_valorem_rate"] == Decimal("0.2500")
    assert row["excluded_hts"] == ["8703230110"]
    assert row["quote_verified"] is True        # found despite the line break


def test_to_row_flags_an_unfound_quote_and_drops_bad_values():
    row, _ = extract.to_row(_m(source_quote="duties of 50 percent"), TEXT)
    assert row["quote_verified"] is False
    assert extract.to_row(_m(hts_code="8"), TEXT)[0] is None
    assert extract.to_row(_m(origin_country="Canada"), TEXT)[0] is None
    assert extract.to_row(_m(ad_valorem_rate=25), TEXT)[0] is None      # 2500%, not 25%
    assert extract.to_row(_m(effective_to=dt.date(2026, 8, 1)), TEXT)[0] is None
    assert extract.to_row(_m(ad_valorem_rate=None), TEXT)[0] is None
    assert extract.to_row(_m(confidence=1.5), TEXT)[0] is None


def _fake_openai(message, captured):
    def parse(**kw):
        captured.update(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(parse=parse)))


def test_extract_measures_uses_structured_outputs():
    captured = {}
    parsed = extract.ExtractionResult(measures=[_m()])
    llm = _fake_openai(SimpleNamespace(parsed=parsed, refusal=None), captured)
    out = extract.extract_measures(llm, "gpt-5.4", {"title": "T"}, TEXT)
    assert out[0].ad_valorem_rate == 0.25
    assert captured["response_format"] is extract.ExtractionResult
    assert captured["model"] == "gpt-5.4"
    assert captured["messages"][0]["role"] == "system"


def test_a_refusal_or_empty_output_is_an_error():
    import pytest
    for message in (SimpleNamespace(parsed=None, refusal="no"),
                    SimpleNamespace(parsed=None, refusal=None)):
        with pytest.raises(ValueError):
            extract.extract_measures(_fake_openai(message, {}), "gpt-4o", {}, TEXT)


def test_the_schema_is_accepted_by_strict_structured_outputs():
    # The openai SDK's own strict-schema conversion raises on shapes the API
    # would reject, so this catches a schema change before a live run does.
    from openai.lib._pydantic import to_strict_json_schema
    schema = to_strict_json_schema(extract.ExtractionResult)
    measure = schema["$defs"]["ExtractedMeasure"]
    assert set(measure["required"]) == set(measure["properties"])
    assert measure["additionalProperties"] is False
