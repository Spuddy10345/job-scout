# syntax=docker/dockerfile:1
# Job Scout - multi-arch (amd64 / arm64, e.g. Raspberry Pi 4/5) image.

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
# dependencies first, so code changes don't reinstall them
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

FROM python:3.13-slim
ARG VERSION=dev
LABEL org.opencontainers.image.title="Job Scout" \
      org.opencontainers.image.description="Self-hosted job finder with AI scoring and an application tracker" \
      org.opencontainers.image.source="https://github.com/Spuddy10345/job-scout" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH" \
    JOBSCOUT_DATA=/data JOBSCOUT_HOST=0.0.0.0 JOBSCOUT_PORT=8080 TZ=Europe/London
RUN useradd --uid 1000 --create-home scout && mkdir -p /data && chown scout /data
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY app ./app
COPY config/defaults.yaml ./config/defaults.yaml
USER scout
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
CMD ["python", "-m", "app", "serve"]
