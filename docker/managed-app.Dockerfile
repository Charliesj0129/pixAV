ARG PIXAV_MANAGED_PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3
FROM ${PIXAV_MANAGED_PYTHON_IMAGE} AS dependencies

ENV UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH=/app/.venv/bin:$PATH
WORKDIR /app
RUN pip install --no-cache-dir --index-url https://pypi.org/simple uv==0.9.11 \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-group dev --no-group embeddings --no-install-project

FROM dependencies AS application
COPY src/ /app/src/
COPY config/ /app/config/
COPY migrations/ /app/migrations/
COPY scripts/ /app/scripts/
USER 1000:1000
# A deployment must select an explicit managed role.
CMD ["python", "-c", "raise SystemExit('select an explicit managed service command')"]
