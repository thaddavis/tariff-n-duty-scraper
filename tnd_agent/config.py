"""Settings, all from the environment (a local .env is loaded by run.py)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    postgres_url: str
    org_id: int                       # the org whose tnd_* rows this run writes
    openai_api_key: str | None
    model: str = "gpt-5.4"           # or gpt-4o; any model with structured outputs
    lookback_days: int = 14           # first run only; later runs start where the last ended
    max_docs: int = 25                # LLM extractions per run; the rest wait for the next run
    hts_codes: list[str] = field(default_factory=list)  # codes to load base rates for

    @classmethod
    def from_env(cls) -> "Config":
        url = os.getenv("POSTGRES_URL")
        org = os.getenv("TND_ORG_ID")
        if not url or not org:
            raise SystemExit("POSTGRES_URL and TND_ORG_ID are required")
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        url = url.replace("postgresql+psycopg://", "postgresql://")
        codes = [c.strip() for c in os.getenv("TND_HTS_CODES", "").split(",") if c.strip()]
        # `or`, not a getenv default: a blank line in .env (TND_MODEL=) means unset.
        return cls(
            postgres_url=url,
            org_id=int(org),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            model=os.getenv("TND_MODEL") or cls.model,
            lookback_days=int(os.getenv("TND_LOOKBACK_DAYS") or cls.lookback_days),
            max_docs=int(os.getenv("TND_MAX_DOCS") or cls.max_docs),
            hts_codes=codes,
        )
