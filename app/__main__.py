"""Command line: `python -m app <command>` (or `uv run python -m app <command>`).

    serve              run the web UI and scheduler (JOBSCOUT_HOST / JOBSCOUT_PORT, default 127.0.0.1:8080)
    sources            list sources and whether their keys are set
    run <source>...    run sources once now and score what they find
    rescore            re-score every active job (uses the Batches API for big runs if enabled)
"""

from __future__ import annotations

import argparse
import logging
import sys

from .credentials import env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app", description="Job Scout")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the web UI and scheduler")
    serve.add_argument("--host", default=env("JOBSCOUT_HOST") or "127.0.0.1")
    serve.add_argument("--port", type=int, default=int(env("JOBSCOUT_PORT") or 8080))
    sub.add_parser("sources", help="list sources and whether their keys are set")
    run = sub.add_parser("run", help="run sources once now")
    run.add_argument("source", nargs="+")
    sub.add_parser("rescore", help="re-score every active job")
    args = parser.parse_args(argv)

    if args.command == "serve":
        import uvicorn

        uvicorn.run("app.main:app", host=args.host, port=args.port, proxy_headers=True)
        return 0

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from .credentials import install_log_redaction

    install_log_redaction()
    from . import pipeline
    from .config import get_settings, missing_keys
    from .db import init_db
    from .sources import SOURCES

    init_db()
    if args.command == "sources":
        settings = get_settings()
        for name, src in SOURCES.items():
            cfg = settings.sources.get(name)
            missing = missing_keys(name)
            state = f"needs {', '.join(missing)}" if missing else ("on" if cfg and cfg.enabled else "off")
            print(f"{name:12} {src.label:22} {state}")
        return 0
    if args.command == "run":
        unknown = [s for s in args.source if s not in SOURCES]
        if unknown:
            print(f"unknown source(s): {', '.join(unknown)} - see `python -m app sources`", file=sys.stderr)
            return 2
        failed = False
        for name in args.source:
            run = pipeline.run_source(name)
            print(f"{name}: found {run.found}, new {run.new}, merged {run.merged}, filtered {run.filtered}, "
                  f"scored {run.scored}" + (f" - error: {run.error}" if run.error else ""))
            failed |= bool(run.error)
        return 1 if failed else 0
    if args.command == "rescore":
        print(f"{pipeline.rescore_all()} jobs scored or queued")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
