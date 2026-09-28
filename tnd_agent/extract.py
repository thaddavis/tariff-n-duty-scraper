"""Turn one Federal Register document into tnd_measures rows with an LLM.

The model fills a fixed schema through OpenAI structured outputs (strict
JSON schema), so the output is data rather than prose. Everything it returns is then checked in code:

  - tariff codes are digits only, 2-10 of them; an origin is two letters;
  - a rate is between 0 and 500%;
  - the source quote is searched for in the document text. quote_verified
    tells the reviewer whether the sentence the rate came from really exists.

Rows are written as 'proposed'. A person approves them; nothing the model
says reaches a duty calculation on its own.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

MAX_CHARS = 150_000   # longer documents are cut, and the document note says so


class ExtractedMeasure(BaseModel):
    hts_code: str | None = Field(
        description="Tariff code or code prefix exactly as written, e.g. '8703.23' or "
                    "'9903.88.03'. null when the measure covers every product (for "
                    "example 'all products of Canada').")
    origin_country: str | None = Field(
        description="ISO 3166 alpha-2 country of origin, e.g. 'CN'. null = every origin.")
    program: str = Field(
        description="Legal authority in snake_case: section_301, section_232, section_338, "
                    "section_201, ieepa, ad, cvd, mfn or other.")
    rate_type: Literal["ad_valorem", "specific", "compound"]
    ad_valorem_rate: float | None = Field(
        description="Fraction of customs value: 25% is 0.25. 0 when a duty is suspended or ended.")
    specific_rate: float | None = Field(description="U.S. dollars per unit, e.g. 0.044.")
    specific_unit: str | None = Field(description="Unit for specific_rate, e.g. 'kg'.")
    effective_from: dt.date = Field(
        description="First date goods entered pay this rate.")
    effective_to: dt.date | None = Field(
        description="First date they no longer do; null if open-ended.")
    excluded_hts: list[str] = Field(
        description="Codes or prefixes explicitly carved out; empty list if none.")
    description: str = Field(
        description="One or two plain sentences an operations person understands.")
    source_quote: str = Field(
        description="A sentence copied VERBATIM from the document that states this rate "
                    "or change. Under 300 characters.")
    # No ge/le bounds: strict structured outputs keep the schema minimal, and
    # to_row checks the range instead.
    confidence: float = Field(description="0 to 1: how sure you are this record is right.")


class ExtractionResult(BaseModel):
    measures: list[ExtractedMeasure]


SYSTEM = """You extract U.S. import duty rate changes from Federal Register documents
into structured records for a customs compliance team, who will review every record
before it is used to calculate duty.

Rules:
- Record only duty rates that this document puts in force, changes, suspends or ends.
  Ignore proposals, requests for comment, and anything that is not a duty rate.
- One record per combination of tariff code (or prefix), origin, rate and date range.
- Never invent a tariff code. If the document names none, use null for hts_code only
  when it clearly covers every product (from that origin); otherwise skip the change.
- A suspended or ended duty is a record with rate 0 from the date it stops applying
  (and effective_to set if it resumes).
- effective_from: the date stated for goods entered or withdrawn from warehouse. If the
  document states none, use its effective date, else its publication date, and lower
  confidence.
- If the document changes no duty rates, return an empty list. That is common and fine."""


def truncate(text: str) -> tuple[str, bool]:
    return (text[:MAX_CHARS], True) if len(text) > MAX_CHARS else (text, False)


def extract_measures(llm, model: str, doc: dict, text: str) -> list[ExtractedMeasure]:
    """`llm` is an openai.OpenAI client. Works with gpt-5.4 and gpt-4o (any
    model with structured outputs)."""
    prompt = (
        f"Title: {doc.get('title')}\n"
        f"Type: {doc.get('type')}\n"
        f"Published: {doc.get('publication_date')}\n"
        f"Effective: {doc.get('effective_on') or 'not stated'}\n"
        f"Document number: {doc.get('document_number')}\n\n"
        f"<document>\n{text}\n</document>"
    )
    resp = llm.chat.completions.parse(
        model=model,
        # Covers reasoning tokens too on gpt-5.x, hence the headroom.
        max_completion_tokens=16000,
        messages=[{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": prompt}],
        response_format=ExtractionResult,
    )
    message = resp.choices[0].message
    if message.refusal:
        raise ValueError(f"model refused: {message.refusal}")
    if message.parsed is None:
        raise ValueError("model returned no structured output")
    return message.parsed.measures


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_found(quote: str | None, text: str) -> bool:
    return bool(quote) and _squash(quote) in _squash(text)


def to_row(m: ExtractedMeasure, text: str) -> tuple[dict | None, str | None]:
    """A validated tnd_measures row, or (None, why it was dropped)."""
    hts = None
    if m.hts_code:
        hts = "".join(ch for ch in m.hts_code if ch.isdigit())
        if not 2 <= len(hts) <= 10:
            return None, f"bad tariff code {m.hts_code!r}"
    origin = (m.origin_country or "").strip().upper() or None
    if origin and not re.fullmatch(r"[A-Z]{2}", origin):
        return None, f"bad origin {m.origin_country!r}"
    if m.ad_valorem_rate is not None and not 0 <= m.ad_valorem_rate <= 5:
        return None, f"implausible rate {m.ad_valorem_rate}"
    if m.rate_type in ("ad_valorem", "compound") and m.ad_valorem_rate is None:
        return None, "ad valorem rate missing"
    if m.rate_type in ("specific", "compound") and m.specific_rate is None:
        return None, "specific rate missing"
    if not 0 <= m.confidence <= 1:
        return None, f"confidence {m.confidence} is outside 0-1"
    if m.effective_to and m.effective_to <= m.effective_from:
        return None, "effective_to is not after effective_from"
    return {
        "hts_code": hts,
        "origin_country": origin,
        "program": re.sub(r"[^a-z0-9_]+", "_", m.program.lower()).strip("_")[:40] or "other",
        "rate_type": m.rate_type,
        "ad_valorem_rate": None if m.ad_valorem_rate is None
        else Decimal(str(m.ad_valorem_rate)).quantize(Decimal("0.0001")),
        "specific_rate": None if m.specific_rate is None else Decimal(str(m.specific_rate)),
        "specific_unit": m.specific_unit,
        "effective_from": m.effective_from,
        "effective_to": m.effective_to,
        "excluded_hts": ["".join(ch for ch in x if ch.isdigit()) for x in m.excluded_hts
                         if any(ch.isdigit() for ch in x)],
        "description": m.description.strip(),
        "source_quote": m.source_quote.strip()[:1000],
        "quote_verified": quote_found(m.source_quote, text),
        "confidence": Decimal(str(round(m.confidence, 2))),
    }, None
