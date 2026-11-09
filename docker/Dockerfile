# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /dist

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ALYA_APP__DATA_DIR=/app/runtime \
    ALYA_STORAGE__DB_PATH=/app/runtime/alya.sqlite3 \
    ALYA_TELEGRAM__SESSION_DIR=/app/runtime/td_database
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10001 alya \
 && useradd --system --uid 10001 --gid alya --home-dir /app --shell /usr/sbin/nologin alya
WORKDIR /app
COPY --from=build /dist/*.whl /tmp/
COPY character /app/character
RUN pip install --no-cache-dir "$(ls /tmp/*.whl)[tg]" && rm -f /tmp/*.whl \
 && mkdir -p /app/runtime && chown alya:alya /app/runtime
USER alya
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["alya", "healthcheck"]
ENTRYPOINT ["alya"]
CMD ["run"]
