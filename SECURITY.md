# Security

## Reporting a vulnerability

Please report security problems privately through GitHub:
**Security → Report a vulnerability** on this repository. Don't open a public issue.
I'll acknowledge reports within a week.

## How Job Scout handles secrets and access

- **Secrets live in the environment only** (`.env`, or `NAME_FILE` pointing at a Docker secret).
  They are never stored in the database, rendered in the UI or written to logs: errors and log
  lines pass through a redactor that masks every configured secret and key-like URL parameters.
- **The web UI has no login unless you set `JOBSCOUT_PASSWORD`.** Docker Compose binds to
  `127.0.0.1` by default. If you expose the app to a network (LAN, Tailscale, reverse proxy), set a
  password - otherwise anyone who can reach it can read your CV and profile, change settings and
  spend your API budget.
- **Cross-site requests are rejected** (`Sec-Fetch-Site` / `Origin` checks on every state-changing
  request), and pages ship a Content-Security-Policy without inline script.
- **Scraped adverts and model output are untrusted.** Only `http(s)` links are rendered, CSV
  exports neutralise spreadsheet formulas, and prompts tell the model to treat advert text as data.
- The container runs as a non-root user with a read-only root filesystem and no capabilities.

## Keeping secrets out of git

`.env`, `config/local.yaml`, `data/` and `secrets/` are gitignored. The repo runs
[gitleaks](https://github.com/gitleaks/gitleaks) in CI over the full history, and you can run it
before every commit with `pre-commit install`. GitHub secret scanning and push protection are on.
