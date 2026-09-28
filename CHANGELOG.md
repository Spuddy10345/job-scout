# Changelog

## 0.2.0 - 2026-09-28

First public release.

- **Security:** optional password login, CSRF protection, Content-Security-Policy, secrets from
  `NAME_FILE`, redaction of keys in stored errors and logs (Adzuna errors used to include the key),
  `http(s)`-only links from scraped data, CV upload limits, hardened container.
- **AI costs:** prompt caching for scoring, half-price Batches API for "Re-score all", per-model
  token pricing with daily/monthly spend, a daily US$ cap, model checks on save.
- **Alerts:** Apprise (ntfy, Discord, Telegram, email and 100+ more) alongside Home Assistant,
  plus a daily digest mode.
- **Web UI:** first-run setup, integrations status, stats page, bulk actions, saved views, CSV
  export, keyboard shortcuts, drag-and-drop tracker, paging, error toasts.
- **Configurable for anyone:** scoring rubric, note persona and language, categories, country and
  currency; personal settings in a gitignored `config/local.yaml`.
- **Fixes:** remote filter ignored "remote off", "£30-35k" salaries, API outages marking jobs
  as score 0, schema migrations for existing databases, Workday posting dates, and more.
- **Running it:** multi-arch image on GHCR, `python -m app` CLI, systemd unit, CI with tests,
  lint, gitleaks and Docker builds.

## 0.1.0

Private version.
