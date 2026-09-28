"""One research run:  uv run python -m tnd_agent.run

  1. Window: from the last successful run's window_end (inclusive, so documents
     published later that same day are caught; repeats are ignored) to today.
     First run: TND_LOOKBACK_DAYS back.
  2. List Federal Register documents in the window. Record each one once;
     the keyword filter marks the irrelevant ones straight away.
  3. Extract measures from up to TND_MAX_DOCS relevant documents with the LLM,
     including ones an earlier run left pending. Each becomes 'proposed' rows.
  4. Load base rates for TND_HTS_CODES from the USITC. A code's rate only
     produces a new proposed row when it differs from the last one recorded.

Exit code 1 when the run failed, so a scheduler sees it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import sys

import httpx
from dotenv import load_dotenv

from . import db, extract, federal_register, hts
from .config import Config

log = logging.getLogger("tnd_agent")
USER_AGENT = "cmdlabs-tnd-research/0.1 (+https://cmdlabs.io)"


def collect_federal_register(conn, http, cfg: Config, run_id: int, since, until, stats) -> None:
    for doc in federal_register.fetch_documents(http, since, until):
        relevant = federal_register.is_relevant(doc)
        doc_id = db.insert_document(
            conn, cfg.org_id, run_id,
            source="federal_register", external_id=doc["document_number"],
            title=doc.get("title") or doc["document_number"], url=doc["html_url"],
            doc_type=doc.get("type"),
            agencies=[a.get("slug") or a.get("name") for a in doc.get("agencies") or []],
            published_on=doc.get("publication_date"), effective_on=doc.get("effective_on"),
            raw_text_url=doc.get("raw_text_url"),
            status="new" if relevant else "irrelevant",
            note=None if relevant else "no duty/tariff keywords in title or abstract",
        )
        if doc_id is not None:
            stats["docs_fetched"] += 1
            stats["docs_relevant"] += relevant


# Errors about the LLM service rather than the document: retrying the next
# document cannot help, so the run fails and every document stays 'new' for
# the next run instead of being marked failed for good.
def _service_errors():
    import openai
    return (openai.AuthenticationError, openai.PermissionDeniedError,
            openai.RateLimitError, openai.APIConnectionError,
            openai.InternalServerError, openai.NotFoundError)  # NotFound: unknown model


def extract_pending(conn, http, llm, cfg: Config, run_id: int, stats) -> None:
    service_errors = _service_errors()
    for doc in db.pending_documents(conn, cfg.org_id, "federal_register", cfg.max_docs):
        try:
            text, cut = extract.truncate(federal_register.fetch_text(http, doc["raw_text_url"]))
            measures = extract.extract_measures(llm, cfg.model, {
                "title": doc["title"], "type": doc["doc_type"],
                "publication_date": doc["published_on"], "effective_on": doc["effective_on"],
                "document_number": doc["external_id"]}, text)
            kept, dropped = 0, []
            for m in measures:
                row, why = extract.to_row(m, text)
                if row is None:
                    dropped.append(why)
                    continue
                db.insert_measure(conn, cfg.org_id, run_id, doc["id"], row, cfg.model)
                kept += 1
            note = f"{kept} measure(s) proposed"
            if dropped:
                note += f"; {len(dropped)} dropped: " + "; ".join(dropped)[:500]
            if cut:
                note += f"; text cut at {extract.MAX_CHARS:,} characters"
            db.set_document_status(conn, doc["id"], "extracted", note,
                                   hashlib.sha256(text.encode()).hexdigest())
            stats["measures_proposed"] += kept
            log.info("%s: %s", doc["external_id"], note)
        except service_errors:
            raise
        except Exception as exc:  # one bad document must not sink the run
            log.exception("extraction failed for %s", doc["external_id"])
            db.set_document_status(conn, doc["id"], "failed", f"{type(exc).__name__}: {exc}"[:1000])


def load_base_rates(conn, http, cfg: Config, run_id: int, today: dt.date, stats) -> None:
    for code in cfg.hts_codes:
        line = hts.fetch_base_rate(http, code)
        if line is None:
            log.warning("HTS %s: not found in the schedule", code)
            continue
        # The rate is part of the id: an unchanged rate is the same document
        # and is skipped; a changed one is new and proposes a new base rate.
        doc_id = db.insert_document(
            conn, cfg.org_id, run_id, source="hts",
            external_id=f"{line['htsno']}|{line['general']}",
            title=f"HTS {line['htsno']}: {line['description']}"[:500],
            url=f"https://hts.usitc.gov/search?query={line['htsno']}",
            published_on=today)
        if doc_id is None:
            continue
        stats["docs_fetched"] += 1
        stats["docs_relevant"] += 1
        rate = hts.parse_rate(line["general"])
        if rate is None:
            db.set_document_status(conn, doc_id, "failed",
                                   f"unparsed general rate {line['general']!r}")
            continue
        # The shorter code: a parent's rate covers everything under it, and a
        # rate found one level down (pick_base_rate rule 2) covers the whole
        # requested code, since every line under it carries that same rate.
        applies_to = min(hts.digits(line["htsno"]), hts.digits(code), key=len)
        db.insert_measure(conn, cfg.org_id, run_id, doc_id, {
            "hts_code": applies_to, "origin_country": None, "program": "mfn",
            **rate, "effective_from": today, "effective_to": None, "excluded_hts": [],
            "description": f"Base (general) rate for {line['htsno']} "
                           f"({line['description']}): {line['general']}.",
            "source_quote": line["general"], "quote_verified": True,
            "confidence": 1}, extracted_by="hts_import")
        db.set_document_status(conn, doc_id, "extracted", "base rate proposed")
        stats["measures_proposed"] += 1


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    today = dt.date.today()

    with db.connect(cfg.postgres_url) as conn, \
            httpx.Client(timeout=60, headers={"User-Agent": USER_AGENT},
                         follow_redirects=True) as http:
        since = db.last_window_end(conn, cfg.org_id) or today - dt.timedelta(days=cfg.lookback_days)
        run_id = db.start_run(conn, cfg.org_id, since, today)
        stats = {"docs_fetched": 0, "docs_relevant": 0, "measures_proposed": 0}
        log.info("run %s: org %s, %s .. %s", run_id, cfg.org_id, since, today)
        try:
            collect_federal_register(conn, http, cfg, run_id, since, today, stats)
            if cfg.openai_api_key:
                import openai
                llm = openai.OpenAI(api_key=cfg.openai_api_key, max_retries=3)
                extract_pending(conn, http, llm, cfg, run_id, stats)
            else:
                log.warning("OPENAI_API_KEY not set: documents recorded, none extracted")
            load_base_rates(conn, http, cfg, run_id, today, stats)
        except Exception as exc:
            log.exception("run %s failed", run_id)
            db.finish_run(conn, run_id, "failed", stats, f"{type(exc).__name__}: {exc}"[:2000])
            return 1
        db.finish_run(conn, run_id, "succeeded", stats)
        log.info("run %s succeeded: %s", run_id, stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
