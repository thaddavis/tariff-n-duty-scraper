"""Writes to the tnd_* tables (schema: cmdlabs-api migration c7a1d2e3f4b5).

Plain SQL over psycopg, autocommit: each document is committed as it is
processed, so a crash half-way keeps what was done and the next run picks up
documents still marked 'new'.
"""
from __future__ import annotations

import datetime as dt
import json

import psycopg
from psycopg.rows import dict_row


def connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=True, row_factory=dict_row)


def last_window_end(conn, org_id: int) -> dt.date | None:
    row = conn.execute(
        "SELECT max(window_end) AS d FROM tnd_research_runs "
        "WHERE org_id = %s AND status = 'succeeded'", (org_id,)).fetchone()
    return row["d"]


def start_run(conn, org_id: int, window_start: dt.date, window_end: dt.date) -> int:
    return conn.execute(
        "INSERT INTO tnd_research_runs (org_id, window_start, window_end) "
        "VALUES (%s, %s, %s) RETURNING id", (org_id, window_start, window_end)).fetchone()["id"]


def finish_run(conn, run_id: int, status: str, stats: dict, error: str | None = None) -> None:
    conn.execute(
        "UPDATE tnd_research_runs SET status = %s, finished_at = now(), error = %s, "
        "docs_fetched = %s, docs_relevant = %s, measures_proposed = %s WHERE id = %s",
        (status, error, stats.get("docs_fetched", 0), stats.get("docs_relevant", 0),
         stats.get("measures_proposed", 0), run_id))


def insert_document(conn, org_id: int, run_id: int, *, source: str, external_id: str,
                    title: str, url: str, doc_type: str | None = None,
                    agencies: list[str] | None = None, published_on=None, effective_on=None,
                    raw_text_url: str | None = None, status: str = "new",
                    note: str | None = None) -> int | None:
    """The new row's id, or None when this document was already recorded."""
    row = conn.execute(
        """INSERT INTO tnd_source_documents
               (org_id, run_id, source, external_id, title, doc_type, agencies,
                published_on, effective_on, url, raw_text_url, status, note)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (org_id, source, external_id) DO NOTHING
           RETURNING id""",
        (org_id, run_id, source, external_id, title, doc_type, json.dumps(agencies or []),
         published_on, effective_on, url, raw_text_url, status, note)).fetchone()
    return row["id"] if row else None


def pending_documents(conn, org_id: int, source: str, limit: int) -> list[dict]:
    """Relevant documents not yet extracted, from this run or an earlier one."""
    return conn.execute(
        "SELECT * FROM tnd_source_documents WHERE org_id = %s AND source = %s "
        "AND status = 'new' ORDER BY published_on DESC NULLS LAST, id LIMIT %s",
        (org_id, source, limit)).fetchall()


def set_document_status(conn, doc_id: int, status: str, note: str | None = None,
                        content_hash: str | None = None) -> None:
    conn.execute(
        "UPDATE tnd_source_documents SET status = %s, note = %s, "
        "content_hash = coalesce(%s, content_hash) WHERE id = %s",
        (status, note, content_hash, doc_id))


def insert_measure(conn, org_id: int, run_id: int, doc_id: int, row: dict,
                   extracted_by: str) -> int:
    return conn.execute(
        """INSERT INTO tnd_measures
               (org_id, run_id, source_document_id, hts_code, origin_country, program,
                rate_type, ad_valorem_rate, specific_rate, specific_unit, effective_from,
                effective_to, excluded_hts, description, source_quote, quote_verified,
                confidence, extracted_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (org_id, run_id, doc_id, row["hts_code"], row["origin_country"], row["program"],
         row["rate_type"], row["ad_valorem_rate"], row["specific_rate"], row["specific_unit"],
         row["effective_from"], row["effective_to"], json.dumps(row["excluded_hts"]),
         row["description"], row["source_quote"], row["quote_verified"], row["confidence"],
         extracted_by)).fetchone()["id"]
