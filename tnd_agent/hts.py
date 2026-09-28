"""USITC Harmonized Tariff Schedule: base (column 1 "general") rates.

Federal Register documents announce CHANGES; a duty figure also needs the base
rate each code pays before any of them. That comes from the USITC's public
HTS API and is parsed without an LLM: "2.5%", "Free", "4.4¢/kg",
"2.5% + 4.4¢/kg".

Loaded for the codes the customer actually imports (TND_HTS_CODES), not the
whole schedule. Free-trade-agreement ("special") rates are not loaded.
"""
from __future__ import annotations

import re
from decimal import Decimal

import httpx

SEARCH = "https://hts.usitc.gov/reststop/search"

_PCT = re.compile(r"^(\d+(?:\.\d+)?)%$")
_SPECIFIC = re.compile(r"^(\$)?(\d+(?:\.\d+)?)(¢)?\s*/\s*([a-z]+)$", re.IGNORECASE)


def digits(code: str) -> str:
    return "".join(ch for ch in code if ch.isdigit())


def parse_rate(text: str) -> dict | None:
    """A general-column rate as tnd_measures rate fields, or None if it is
    something this parser does not handle (quotas, 'See 9903...', ranges)."""
    parts = [p.strip() for p in (text or "").strip().split("+")]
    if parts == ["Free"]:
        return {"rate_type": "ad_valorem", "ad_valorem_rate": Decimal("0"),
                "specific_rate": None, "specific_unit": None}
    ad_valorem = specific = unit = None
    for part in parts:
        if m := _PCT.match(part):
            if ad_valorem is not None:
                return None
            ad_valorem = Decimal(m.group(1)) / 100
        elif m := _SPECIFIC.match(part):
            if specific is not None or (m.group(1) and m.group(3)):
                return None
            specific = Decimal(m.group(2)) / (100 if m.group(3) else 1)
            unit = m.group(4).lower()
        else:
            return None
    if ad_valorem is not None and specific is not None:
        rate_type = "compound"
    elif ad_valorem is not None:
        rate_type = "ad_valorem"
    elif specific is not None:
        rate_type = "specific"
    else:
        return None
    return {"rate_type": rate_type, "ad_valorem_rate": ad_valorem,
            "specific_rate": specific, "specific_unit": unit}


def _clean(text: str | None) -> str:
    """The API leaves markup in some fields, e.g. '2.5% <u></u>'."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


def pick_base_rate(code: str, rows: list[dict]) -> dict | None:
    """The line whose general rate applies to `code`:

    1. the longest listed code that `code` starts with: a 10-digit statistical
       line usually has no rate of its own and inherits its 8-digit parent's;
    2. otherwise a longer line under `code`, when the rate sits one level down
       (8544.30.00 is rated on 8544.30.00.00). Only if every such line carries
       the same rate; if they differ the code is too broad to answer.
    """
    want = digits(code)
    rated = [{**r, "general": _clean(r.get("general")),
              "description": _clean(r.get("description"))}
             for r in rows if r.get("htsno") and _clean(r.get("general"))]
    parents = [r for r in rated if want.startswith(digits(r["htsno"]))]
    if parents:
        best = max(parents, key=lambda r: len(digits(r["htsno"])))
    else:
        children = [r for r in rated if digits(r["htsno"]).startswith(want)]
        if not children or len({r["general"] for r in children}) != 1:
            return None
        best = min(children, key=lambda r: len(digits(r["htsno"])))
    return {"htsno": best["htsno"], "description": best["description"],
            "general": best["general"]}


def fetch_base_rate(http: httpx.Client, code: str) -> dict | None:
    """The HTS line for `code`: {htsno, description, general}, or None when
    the schedule has no such code."""
    resp = http.get(SEARCH, params={"keyword": code})
    resp.raise_for_status()
    return pick_base_rate(code, resp.json())
