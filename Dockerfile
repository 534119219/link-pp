FROM node:24-bookworm-slim AS node-runtime

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY --from=node-runtime /usr/local/ /usr/local/

RUN apt-get update \
    && apt-get install --no-install-recommends --yes ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app \
    && install -d -o app -g app -m 0700 /data/diagnostics

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY package.json package-lock.json ./
RUN npm ci --omit=dev \
    && npm cache clean --force

COPY --chown=app:app handoff ./handoff
COPY --chown=app:app run.py wsgi.py ./

USER app

EXPOSE 5572

HEALTHCHECK --interval=20s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5572/api/meta', timeout=5).read()"

CMD ["gunicorn", "--workers", "1", "--worker-class", "gthread", "--threads", "8", "--bind", "0.0.0.0:5572", "--timeout", "900", "--graceful-timeout", "30", "--keep-alive", "30", "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
