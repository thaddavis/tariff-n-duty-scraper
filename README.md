# Duty & tariff research agent

A scheduled job that finds U.S. duty and tariff changes and writes them to the
cmdlabs database for review. The tables (`tnd_research_runs`,
`tnd_source_documents`, `tnd_measures`) come from cmdlabs-api migration
`c7a1d2e3f4b5`. People review what it finds on **Tariff Updates** in the app,
where approved rates also drive the duty calculator (`/api/tariffs/duty`).

## What one run does

1. Lists Federal Register documents published since the last successful run:
   presidential documents and rules from the White House, CBP, USTR and ITA, plus
   USTR notices. Each document is recorded once.
2. A keyword filter marks the irrelevant ones, such as visa rules and holidays.
3. The LLM turns each relevant document into structured rate records using
   OpenAI structured outputs. The model is `TND_MODEL`: `gpt-5.4` (default) or
   `gpt-4o`. Code then checks each record: tariff codes are 2 to 10 digits,
   the origin is two letters, the rate is plausible, and the quoted sentence
   really appears in the document (`quote_verified`).
4. Loads each `TND_HTS_CODES` code's base rate from the USITC HTS API, parsed
   without an LLM. A base rate is only proposed again when it changes.

Everything is written as `proposed`. Nothing counts until a person approves it.

- **Failed documents:** a document that fails is marked `failed` with the reason.
- **LLM service errors:** an invalid key, rate limit or outage fails the whole run
  (exit code 1) and leaves documents pending for the next run.

## Run it

```bash
cp .env.example .env      # fill in POSTGRES_URL, TND_ORG_ID, OPENAI_API_KEY
uv run python -m tnd_agent.run
uv run pytest -q          # set TND_TEST_POSTGRES_URL (a disposable, migrated DB) for the DB test
```

## Schedule it

Daily is enough, because the Federal Register publishes once each business day.
Build the Dockerfile, deploy it as a Cloud Run **Job** with the `.env` values as
secrets, and trigger it from Cloud Scheduler:

```bash
gcloud run jobs deploy tnd-research --image IMAGE --region us-central1 \
  --set-secrets POSTGRES_URL=...,OPENAI_API_KEY=... --set-env-vars TND_ORG_ID=...
gcloud scheduler jobs create http tnd-research-daily --schedule "0 7 * * *" \
  --uri "https://run.googleapis.com/v2/projects/PROJECT/locations/us-central1/jobs/tnd-research:run" \
  --http-method POST --oauth-service-account-email SA@PROJECT.iam.gserviceaccount.com
```

## Not covered yet

- CBP CSMS messages, which have no API
- Free-trade-agreement rates
- In-transit exemptions
- Non-stacking rules between programs

The reviewer handles these through the measure's note.
