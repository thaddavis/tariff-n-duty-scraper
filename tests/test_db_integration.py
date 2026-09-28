"""The run's database writes against a real, migrated schema, with the LLM and
the HTTP sources stubbed.

Needs TND_TEST_POSTGRES_URL pointing at a DISPOSABLE database migrated to
cmdlabs-api head (c7a1d2e3f4b5 or later); skipped otherwise.
"""
import datetime as dt
import os
from types import SimpleNamespace

import pytest

from tnd_agent import db, extract, run
from tnd_agent.config import Config

URL = os.getenv("TND_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TND_TEST_POSTGRES_URL not set")

TEXT = ("I imposed under section 338 additional ad valorem duties of 25 percent, "
        "effective August 19, 2026, on certain products of Canada.")


class FakeHttp:
    def get(self, url, params=None):
        if "hts.usitc.gov" in url and params["keyword"] == "8544.30.00":
            body = [{"htsno": "8544.30.00.00", "description": "Wiring sets", "general": "5%"}]
        elif "hts.usitc.gov" in url:
            body = [{"htsno": "8703.23.01", "description": "Cars", "general": "2.5%"}]
        elif "documents.json" in url:
            body = {"results": [
                {"document_number": "T-1", "title": "Additional Duties on Canada",
                 "type": "Presidential Document", "publication_date": "2026-09-14",
                 "html_url": "https://fr/T-1", "raw_text_url": "https://fr/T-1.txt"},
                {"document_number": "T-2", "title": "Labor Day, 2026",
                 "type": "Presidential Document", "publication_date": "2026-09-10",
                 "html_url": "https://fr/T-2", "raw_text_url": "https://fr/T-2.txt"}]}
        else:
            body = None
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body,
                               text=f"<pre>{TEXT}</pre>")


class FakeLlm:
    """Stands in for openai.OpenAI: chat.completions.parse returns the
    parsed ExtractionResult the way structured outputs do."""
    class chat:
        class completions:
            @staticmethod
            def parse(**kw):
                parsed = extract.ExtractionResult.model_validate({"measures": [{
                    "hts_code": "8703", "origin_country": "CA", "program": "section_338",
                    "rate_type": "ad_valorem", "ad_valorem_rate": 0.25, "specific_rate": None,
                    "specific_unit": None, "effective_from": "2026-08-19", "effective_to": None,
                    "excluded_hts": [], "description": "25% on Canadian vehicles.",
                    "source_quote": "additional ad valorem duties of 25 percent",
                    "confidence": 0.9}]})
                return SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(parsed=parsed, refusal=None))])


@pytest.fixture()
def conn():
    with db.connect(URL) as c:
        org = c.execute("INSERT INTO organizations (name) VALUES ('tnd test') RETURNING id").fetchone()["id"]
        yield c, org
        c.execute("DELETE FROM organizations WHERE id = %s", (org,))


def test_a_run_records_documents_and_proposes_measures_once(conn):
    c, org = conn
    cfg = Config(postgres_url=URL, org_id=org, openai_api_key="x", hts_codes=["8703.23.01", "8544.30.00"])
    today = dt.date(2026, 9, 28)

    for _ in range(2):   # the second run must add nothing
        run_id = db.start_run(c, org, dt.date(2026, 9, 1), today)
        stats = {"docs_fetched": 0, "docs_relevant": 0, "measures_proposed": 0}
        run.collect_federal_register(c, FakeHttp(), cfg, run_id, dt.date(2026, 9, 1), today, stats)
        run.extract_pending(c, FakeHttp(), FakeLlm, cfg, run_id, stats)
        run.load_base_rates(c, FakeHttp(), cfg, run_id, today, stats)
        db.finish_run(c, run_id, "succeeded", stats)

    docs = {r["external_id"]: r for r in c.execute(
        "SELECT external_id, status FROM tnd_source_documents WHERE org_id = %s", (org,))}
    assert docs["T-1"]["status"] == "extracted"
    assert docs["T-2"]["status"] == "irrelevant"
    assert docs["8703.23.01|2.5%"]["status"] == "extracted"
    assert docs["8544.30.00.00|5%"]["status"] == "extracted"

    measures = c.execute(
        "SELECT program, hts_code, origin_country, ad_valorem_rate, quote_verified, status "
        "FROM tnd_measures WHERE org_id = %s ORDER BY program, hts_code", (org,)).fetchall()
    assert [(m["program"], m["hts_code"], m["origin_country"], float(m["ad_valorem_rate"]),
             m["quote_verified"], m["status"]) for m in measures] == [
        ("mfn", "85443000", None, 0.05, True, "proposed"),   # rated on .00.00, saved as asked
        ("mfn", "87032301", None, 0.025, True, "proposed"),
        ("section_338", "8703", "CA", 0.25, True, "proposed"),
    ]
    runs = c.execute("SELECT measures_proposed FROM tnd_research_runs WHERE org_id = %s "
                     "ORDER BY id", (org,)).fetchall()
    assert [r["measures_proposed"] for r in runs] == [3, 0]
    assert db.last_window_end(c, org) == today
