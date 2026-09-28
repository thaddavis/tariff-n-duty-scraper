"""Federal Register: where U.S. duty changes are published.

Uses the public JSON API (no key). Proclamations and executive orders
(Section 232/338/IEEPA tariffs), CBP and USTR rules, and USTR notices
(Section 301 actions and exclusions) all appear here with a publication date,
and usually an effective date.

A cheap keyword filter on title + abstract decides which documents are worth
an LLM call: the same agencies publish far more that has nothing to do with
duty (visa rules, commemorations).
"""
from __future__ import annotations

import datetime as dt
import html
import re

import httpx

API = "https://www.federalregister.gov/api/v1/documents.json"

QUERIES = [
    {   # Rules and presidential documents from the agencies that set duty rates.
        "agencies": [
            "executive-office-of-the-president",
            "u-s-customs-and-border-protection",
            "trade-representative-office-of-united-states",
            "international-trade-administration",
        ],
        "types": ["RULE", "PRESDOCU"],
    },
    {   # USTR publishes Section 301 modifications and exclusions as notices.
        "agencies": ["trade-representative-office-of-united-states"],
        "types": ["NOTICE"],
    },
]

FIELDS = ["document_number", "title", "type", "abstract", "publication_date",
          "effective_on", "agencies", "html_url", "raw_text_url"]

RELEVANT = re.compile(
    r"\b(tariffs?|dut(y|ies)|harmonized tariff|htsus?|section\s+(201|232|301|338)|"
    r"anti-?dumping|countervailing|imports?|importation|customs)\b",
    re.IGNORECASE,
)


def is_relevant(doc: dict) -> bool:
    return bool(RELEVANT.search(f"{doc.get('title') or ''} {doc.get('abstract') or ''}"))


def fetch_documents(http: httpx.Client, since: dt.date, until: dt.date) -> list[dict]:
    """Every document in QUERIES published in [since, until], newest first, once each."""
    seen: dict[str, dict] = {}
    for q in QUERIES:
        params = [
            ("conditions[publication_date][gte]", since.isoformat()),
            ("conditions[publication_date][lte]", until.isoformat()),
            ("order", "newest"),
            ("per_page", "100"),
            *[("conditions[agencies][]", a) for a in q["agencies"]],
            *[("conditions[type][]", t) for t in q["types"]],
            *[("fields[]", f) for f in FIELDS],
        ]
        url: str | None = API
        while url:
            resp = http.get(url, params=params if url == API else None)
            resp.raise_for_status()
            body = resp.json()
            for doc in body.get("results") or []:
                seen.setdefault(doc["document_number"], doc)
            url = body.get("next_page_url")
    return sorted(seen.values(), key=lambda d: d.get("publication_date") or "", reverse=True)


def fetch_text(http: httpx.Client, raw_text_url: str) -> str:
    """The document's plain text. raw_text_url serves HTML around a <pre> block."""
    resp = http.get(raw_text_url)
    resp.raise_for_status()
    return html_to_text(resp.text)


def html_to_text(raw: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
