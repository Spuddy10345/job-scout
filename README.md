# Job Scout

[![CI](https://github.com/Spuddy10345/job-scout/actions/workflows/ci.yml/badge.svg)](https://github.com/Spuddy10345/job-scout/actions/workflows/ci.yml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)

A self-hosted job finder that runs around the clock. It collects vacancies from job-board APIs,
employers' own careers systems, university job sites, scraped boards and web search. Duplicates
are merged, jobs outside your commute or seniority range are filtered out, and Claude scores what
remains against your profile and CV. Everything lands in a web UI with sorting, per-advert
application steps, contact details, drafted application notes, an application tracker and stats.
Strong matches get pushed to your phone.

![Jobs list](docs/screenshots/jobs.png)

| Job detail | Tracker | Stats |
|---|---|---|
| ![Job detail](docs/screenshots/drawer.png) | ![Tracker](docs/screenshots/tracker.png) | ![Stats](docs/screenshots/stats.png) |
| **First-run setup** | **Sources** | **Mobile** |
| ![Setup](docs/screenshots/setup.png) | ![Sources](docs/screenshots/runs.png) | ![Mobile](docs/screenshots/mobile.png) |

*Screenshots use made-up demo data (`scripts/seed_demo.py`).*

## What it does

- **Collects** from Adzuna and Reed (official APIs), employers' applicant-tracking systems
  (Greenhouse, Lever, Ashby, SmartRecruiters, Workday), any careers page you point it at,
  jobs.ac.uk, optional scraped boards (Find a Job, NHS Jobs, Totaljobs, CWJobs, Gumtree, Indeed,
  LinkedIn) and Brave web search.
- **Merges and filters.** The same role seen on three sites becomes one row that lists all three.
  Jobs are dropped for distance, seniority words, years of experience asked for, salary, age or
  excluded keywords. That's free, and it happens before any AI is used.
- **Scores with Claude.** Every job gets a 0–100 fit score, a category, a two-line "why", a
  concrete CV bullet you could earn there, red flags, how to apply for *that* advert, and any
  contacts it names.
- **Helps you apply.** One click drafts a cover email or supporting statement in the format the
  application route expects. Notes, status history, and a drag-and-drop tracker that flags
  applications that have gone quiet.
- **Alerts you** through [Apprise](https://github.com/caronc/apprise/wiki) (ntfy, Discord,
  Telegram, Slack, email, Pushover and 100+ more) and/or Home Assistant, instantly or as a daily
  digest.
- **Keeps costs visible.** Daily call and dollar caps, spend tracked from real token usage, prompt
  caching, and half-price batch re-scoring.

## Why I built this

I built it for my own search: a Software Engineering graduate (post-quantum cryptography
dissertation) looking for a first role around Cardiff, South Wales, Bristol or remote. It runs 24/7
on a Raspberry Pi 5. Everything that was specific to me (the profile, search area, scoring rubric
and watchlist) is now a setting, so it works for anyone's search.

## How it works

```mermaid
flowchart LR
  subgraph Collect
    A[Adzuna / Reed APIs] & W[Employer ATS APIs<br/>Greenhouse · Lever · Ashby · Workday] & U[jobs.ac.uk]
    F[Boards & careers pages<br/>via Firecrawl + Claude extraction] & B[Brave web search]
  end
  Collect --> N[Normalise + dedupe<br/>clean URLs, company/title fingerprint]
  N --> G[Geocode<br/>postcodes.io → OSM Nominatim, cached]
  G --> H[Hard filters<br/>radius · seniority · years · salary · age]
  H --> S[Claude scoring<br/>structured output, cached prompt]
  S --> DB[(SQLite)]
  DB --> UI[FastAPI + HTMX UI]
  DB --> AL[Alerts<br/>Apprise · Home Assistant]
  UI -- on demand --> C[Claude<br/>application notes]
  UI -- Re-score all --> BA[Message Batches API<br/>half price]
```

**It's a pipeline, not a free-running agent.** Collecting, deduplicating and filtering are
deterministic, cheap and testable. The model is only used for judgement: fit, category, the CV
angle, how to apply for this particular advert, and pulling listings out of pages that have no
API. That keeps costs low and results explainable.

Design points:

- **APIs before scraping.** Many employers expose public applicant-tracking endpoints that are
  exact and free. Firecrawl is only used for boards without an API. It renders the page to
  markdown for 1 credit, and Claude extracts the listings.
- **Change detection.** Careers pages on the watchlist are hashed, so extraction only runs when a
  page actually changes.
- **Deduplication.** A fingerprint of the normalised title, company (minus Ltd/PLC/…) and town,
  plus a cleaned URL, merges the same job across sources.
- **Geocoding.** postcodes.io covers Great Britain (Welsh places by their Welsh names, which is
  handled). Everything else goes to OpenStreetMap Nominatim at its required 1 request/second.
  Results are cached.
- **Structured outputs.** `messages.parse()` with Pydantic models means the pipeline never repairs
  JSON. Allowed categories are built from your settings, so the model can only pick one of yours.
  Web-search hits often have jumbled titles, so the scoring call also returns the advert's real
  title, company and location, and the location filter re-runs on them.
- **Cost control.** The rubric + profile + CV prefix is identical for every job, so it's
  prompt-cached. "Re-score all" goes through the Message Batches API at half price. Every call's
  tokens are priced and capped per day.
- **Concurrency.** APScheduler runs collectors in threads over SQLite in WAL mode. Network work
  (geocoding, batch results) happens before write transactions open, so a long ingest never
  deadlocks against cache writes.

## Quick start

You need Docker, or Python 3.12+ with [uv](https://docs.astral.sh/uv/). Only an Anthropic key is
needed for scoring. Adzuna and Reed keys are free, and jobs.ac.uk plus the employer watchlist work
with no keys at all.

### Docker (recommended)

```bash
git clone https://github.com/Spuddy10345/job-scout && cd job-scout
cp .env.example .env        # add the keys you have
docker compose up -d        # pulls ghcr.io/spuddy10345/job-scout (amd64 + arm64)
open http://localhost:8080  # the setup page walks you through the rest
```

Use `docker compose up -d --build` to build from your checkout instead. Data (the SQLite
database, your uploaded CV and the session key) lives in `./data`.

By default the port is only bound to `127.0.0.1`. To use it from your phone or another machine,
set `JOBSCOUT_BIND=0.0.0.0` **and** `JOBSCOUT_PASSWORD` in `.env`, or put it behind Tailscale or
an authenticating reverse proxy. See [Security](#security).

### Without Docker

```bash
cp .env.example .env
uv sync
uv run python -m app serve        # http://127.0.0.1:8080
```

`docs/deploy/job-scout.service` is a hardened systemd unit for running it this way on a server or
Raspberry Pi.

### Raspberry Pi

The published image is multi-arch, so `docker compose up -d` works on a Pi 4 or 5 with a 64-bit
OS. Compose caps the container at 512 MB of RAM.

## Configuration

There are three layers, each overriding the one before:

1. **`config/defaults.yaml`**: neutral starting settings, shipped with the app.
2. **`config/local.yaml`** (optional, gitignored): your own starting settings. Copy the
   defaults file and edit the sections you care about. Point `JOBSCOUT_CONFIG` elsewhere if you
   like. In Docker, uncomment the `local.yaml` volume line in `docker-compose.yml`.
3. **The Settings page**: once you save there, the database copy wins. "Reset all settings"
   reseeds from layers 1 and 2.

Everything is editable in the UI:

- profile and CV (PDF, DOCX or Markdown), and whether scoring uses one or both
- search centres and radii, remote on/off, minimum salary, maximum advert age, country and currency
- search terms per source type
- seniority words, excluded keywords, maximum years asked for, and categories with score adjustments
- the **scoring rubric** in plain words, and who application notes are written for, in which language
- sources on/off with run intervals, and the company watchlist
- alerts (instant or digest, quiet hours, threshold)
- models, daily call/US$ caps, per-model prices, batch re-scoring

### Environment variables

Secrets only ever come from the environment. Any of them can be given as `NAME_FILE=/path` instead
(Docker secrets). The full annotated list is in [`.env.example`](.env.example).

| Variable | Used for |
|---|---|
| `ANTHROPIC_API_KEY` | scoring, careers-page extraction, application notes |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | Adzuna search |
| `REED_API_KEY` | Reed search (UK) |
| `BRAVE_API_KEY` | web discovery and company lookups (`BRAVE_RPS` sets the rate) |
| `FIRECRAWL_API_KEY` | boards without APIs, JavaScript careers pages |
| `APPRISE_URLS` | alert destinations, e.g. `ntfy://ntfy.sh/your-long-random-topic` |
| `HA_TOKEN` | Home Assistant long-lived token for companion-app alerts |
| `JOBSCOUT_PASSWORD` | turns on the login page |
| `JOBSCOUT_SECRET_KEY` | signs the login cookie (auto-generated into `data/` if unset) |
| `JOBSCOUT_BIND`, `JOBSCOUT_HOST_PORT`, `TZ` | Docker Compose: bind address, host port, time zone |
| `JOBSCOUT_HOST`, `JOBSCOUT_PORT`, `JOBSCOUT_DATA`, `JOBSCOUT_CONFIG` | running without Docker |
| `JOBSCOUT_CONTACT` | contact sent to OpenStreetMap Nominatim, as its usage policy asks |

### API keys and what they cost

Prices checked September 2026. Settings → Integrations shows which keys are set, and the Stats page
shows what you've actually spent.

| Service | Get a key | Cost |
|---|---|---|
| Anthropic | [console.anthropic.com](https://console.anthropic.com/settings/keys) | Haiku 4.5 is $1 / $5 per million input/output tokens. Scoring a job costs about half a US cent, less once a CV makes the cached prefix long enough. Application notes use Sonnet 5 ($2 / $10). |
| Adzuna | [developer.adzuna.com](https://developer.adzuna.com/signup) | free |
| Reed | [reed.co.uk/developers](https://www.reed.co.uk/developers/jobseeker) | free |
| Brave Search | [api-dashboard.search.brave.com](https://api-dashboard.search.brave.com) | $5 of free credit a month (about 1,000 searches), then pay as you go. The defaults use roughly 150 a month. |
| Firecrawl | [firecrawl.dev](https://www.firecrawl.dev) | 1,000 free credits a month. 1 credit per page, capped per day in Settings. |
| Apprise targets | e.g. [ntfy.sh](https://ntfy.sh) | ntfy is free; others vary |

### Rotating keys

1. Create the new key at the provider (links above), and revoke the old one once the new one works.
2. Put it in `.env`, or in the file a `NAME_FILE` variable points to.
3. Restart so it's picked up (keys are read at start-up):
   - Docker: `docker compose up -d --force-recreate`
   - systemd: `sudo systemctl restart job-scout`
   - manual: stop and rerun `uv run python -m app serve`
4. Check Settings → Integrations shows it as **set**, then press **Run now** on a source (or
   **Send test** for alerts).

If a key ever leaks (pasted somewhere, committed by mistake), revoke it at the provider first.
Removing it from git history doesn't un-leak it.

### Alerts

Set `APPRISE_URLS` to one or more [Apprise URLs](https://github.com/caronc/apprise/wiki), separated
by spaces or commas, then use **Settings → Alerts → Send test**. Examples:

```bash
APPRISE_URLS="ntfy://ntfy.sh/pick-a-long-random-topic"       # free push to the ntfy app
APPRISE_URLS="tgram://BOT_TOKEN/CHAT_ID discord://WEBHOOK_ID/TOKEN"
APPRISE_URLS="mailtos://user:app-password@gmail.com"
```

For Home Assistant, set `HA_TOKEN` and fill in the URL and `notify` service (e.g.
`mobile_app_your_phone`) in Settings.

## Security

- **No login by default.** Set `JOBSCOUT_PASSWORD` whenever anyone else can reach the app. A
  banner warns you when it's being viewed from somewhere other than localhost without one.
- **Cross-site request protection.** State-changing requests must come from the app's own pages,
  so a malicious website can't change your settings or trigger actions through your browser.
- **Secrets stay in the environment.** They're never stored in the database or shown in the UI,
  and error messages and logs are redacted.
- **Scraped content is untrusted.** Only `http(s)` links are rendered, a strict
  Content-Security-Policy blocks inline script, CSV exports defuse spreadsheet formulas, and prompts
  tell the model to treat adverts as data rather than instructions.
- **Hardened container.** Non-root, read-only filesystem, no Linux capabilities.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## Sources and terms of use

Official APIs (Adzuna, Reed, ATS endpoints) are used wherever they exist. Indeed and LinkedIn don't
allow automated access in their terms, so those sources are **off by default** and are meant only
for occasional personal use at your own risk. Scraped boards are rate-limited by the daily
Firecrawl budget. Coverage is UK-first (postcodes.io, Reed, jobs.ac.uk, UK boards). Adzuna,
employer ATS feeds and Nominatim geocoding work in other countries too: set the country code and
currency in Settings → Search area.

## Command line

```bash
uv run python -m app serve              # web UI + scheduler
uv run python -m app sources            # which sources are on / missing keys
uv run python -m app run reed watchlist # run sources once, now
uv run python -m app rescore            # re-score every active job
```

In Docker, prefix these with `docker compose exec job-scout python -m app …`.

## Development

```bash
uv sync
uv run pytest            # uses a throwaway database; never reads your .env or local.yaml
uv run ruff check .
pre-commit install       # gitleaks + ruff before every commit
```

To work on the UI with realistic data and no keys:

```bash
export JOBSCOUT_DATA=/tmp/jobscout-demo JOBSCOUT_DISABLE_SCHEDULER=1
uv run python scripts/seed_demo.py && uv run python -m app serve --port 8081
```

The tests cover:

- dedupe and merging, the radius/remote/seniority/experience/age filters, salary and URL parsing
- source parsers against saved fixtures
- the LLM layer with a mocked client (schema, rubric, categories, cost accounting, budgets,
  batches, outage handling)
- notifications, schema migrations, CSRF, login, redaction, output sanitising
- the web pages and actions

CI runs lint, tests, a full-history gitleaks scan and a Docker build. Tagging `vX.Y.Z` publishes a
multi-arch image to GHCR.

## Roadmap

- htmx 4 migration (2.x is supported indefinitely, so no rush)
- More non-UK sources and a geocoder that isn't GB-first
- Import applications from email

## Stack

Python 3.13 · FastAPI · HTMX 2 · SQLModel/SQLite · APScheduler · httpx · selectolax · Anthropic SDK ·
Apprise · Docker

## Licence

[MIT](LICENSE)
