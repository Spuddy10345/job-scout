# Contributing

Issues and pull requests are welcome.

```bash
uv sync                      # Python 3.12+, uv 0.12+
uv run pytest                # tests use a throwaway database and never read your .env
uv run ruff check .
pre-commit install           # optional: gitleaks + ruff before each commit
```

To work on the UI with realistic data and no API keys:

```bash
export JOBSCOUT_DATA=/tmp/jobscout-demo JOBSCOUT_DISABLE_SCHEDULER=1
uv run python scripts/seed_demo.py
uv run python -m app serve --port 8081
```

Guidelines:

- Keep changes focused, and add a test for any bug you fix.
- Prefer official APIs over scraping. New scrapers should respect each site's terms.
- Never commit real keys, personal settings (`config/local.yaml`) or data from `data/`.
- New settings go in `app/config.py` with a sensible default, plus `config/defaults.yaml` if users should see them.
